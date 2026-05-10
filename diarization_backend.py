"""
Speaker diarization backend backed by sherpa-onnx (no HF auth required).

Pipeline:
    audio_file
      → sherpa-onnx pyannote segmentation 3.0  (who's talking when)
      → 3D-Speaker / TitaNet embedding + clustering  (which turns are the
                                                      same speaker)
      → list of (speaker_id, start, end) turns

We then cross-reference the turns against the word-level timestamps already
produced by the Parakeet ASR backend, group consecutive same-speaker words
into spoken "lines", and finally hand the line-by-line diarized transcript
off to the LLM so it can rename SPEAKER_00 / SPEAKER_01 to real names by
reading conversational cues (introductions, "thanks for calling", etc.).

The intermediate model files are pulled once from
https://github.com/k2-fsa/sherpa-onnx/releases (public, no auth) and cached
to ~/.cache/local_scribe/diarization/.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Optional

import requests

DEFAULT_CACHE_DIR = Path(
    os.getenv("DIARIZATION_CACHE_DIR")
    or (Path.home() / ".cache" / "local_scribe" / "diarization")
)

SEGMENTATION_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
SEGMENTATION_MODEL_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_MODEL_FILE = "model.onnx"

EMBEDDING_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/nemo_en_titanet_small.onnx"
)
EMBEDDING_MODEL_FILE = "nemo_en_titanet_small.onnx"


def ensure_models(cache_dir: Path = DEFAULT_CACHE_DIR) -> tuple[Path, Path]:
    """Download segmentation + embedding models into `cache_dir` if missing.
    Returns (segmentation_path, embedding_path)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = cache_dir / SEGMENTATION_MODEL_DIR
    seg_path = seg_dir / SEGMENTATION_MODEL_FILE
    if not seg_path.exists():
        archive = cache_dir / "segmentation.tar.bz2"
        urllib.request.urlretrieve(SEGMENTATION_MODEL_URL, archive)
        import tarfile

        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(cache_dir)
        archive.unlink(missing_ok=True)
    emb_path = cache_dir / EMBEDDING_MODEL_FILE
    if not emb_path.exists():
        urllib.request.urlretrieve(EMBEDDING_MODEL_URL, emb_path)
    return seg_path, emb_path


_CACHED_DIARIZER = None
_CACHED_DIARIZER_KEY: tuple | None = None


def load_diarizer(
    *,
    num_clusters: int | None = None,
    cluster_threshold: float = 0.5,
    num_threads: int = 4,
    cache_dir: Path = DEFAULT_CACHE_DIR,
):
    """Build a sherpa-onnx OfflineSpeakerDiarization. Cached across calls
    so the second invocation in the same process skips ~1s of init."""
    global _CACHED_DIARIZER, _CACHED_DIARIZER_KEY
    key = (num_clusters, cluster_threshold, num_threads, str(cache_dir))
    if _CACHED_DIARIZER is not None and _CACHED_DIARIZER_KEY == key:
        return _CACHED_DIARIZER
    import sherpa_onnx

    seg_path, emb_path = ensure_models(cache_dir)

    seg_cfg = sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
            model=str(seg_path)
        ),
        num_threads=num_threads,
    )
    emb_cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(emb_path), num_threads=num_threads
    )
    cluster_cfg = sherpa_onnx.FastClusteringConfig(
        num_clusters=num_clusters if num_clusters else -1,
        threshold=cluster_threshold,
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=seg_cfg,
        embedding=emb_cfg,
        clustering=cluster_cfg,
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("sherpa-onnx diarization config failed to validate")
    _CACHED_DIARIZER = sherpa_onnx.OfflineSpeakerDiarization(config)
    _CACHED_DIARIZER_KEY = key
    return _CACHED_DIARIZER


def _load_wav_16k_mono(audio_path: Path):
    """sherpa-onnx wants float32 samples at the diarizer's native sample
    rate (16kHz). We use librosa to handle whatever container the user
    threw at us (m4a, mp3, wav, ogg, flac)."""
    import numpy as np

    sample_rate_target = 16000
    if audio_path.suffix.lower() == ".wav":
        with wave.open(str(audio_path), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getframerate() == sample_rate_target:
                frames = wf.readframes(wf.getnframes())
                samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                return samples, sample_rate_target

    import librosa

    samples, sr = librosa.load(str(audio_path), sr=sample_rate_target, mono=True)
    return samples.astype("float32"), sr


def diarize(
    audio_path: Path,
    *,
    num_clusters: int | None = None,
    cluster_threshold: float = 0.5,
    num_threads: int = 4,
    on_progress: Optional[Callable[[float], None]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[dict]:
    """Run speaker diarization on `audio_path`. Returns segments in order:
        [{"speaker": "SPEAKER_00", "start": 0.31, "end": 6.86}, ...]

    Pass `num_clusters` if you know the speaker count exactly. Otherwise
    `cluster_threshold` (0.5 default) controls sensitivity - smaller →
    more speakers, larger → fewer speakers."""
    diarizer = load_diarizer(
        num_clusters=num_clusters,
        cluster_threshold=cluster_threshold,
        num_threads=num_threads,
        cache_dir=cache_dir,
    )

    samples, sr = _load_wav_16k_mono(audio_path)
    if sr != diarizer.sample_rate:
        raise RuntimeError(
            f"Audio sample rate {sr} doesn't match diarizer's "
            f"native rate {diarizer.sample_rate}"
        )

    def progress_cb(num_processed: int, num_total: int) -> int:
        if on_progress and num_total > 0:
            try:
                on_progress(num_processed / num_total)
            except Exception:
                pass
        return 0

    result = diarizer.process(samples.tolist(), callback=progress_cb)
    segments = []
    for seg in result.sort_by_start_time():
        segments.append(
            {
                "speaker": f"SPEAKER_{int(seg.speaker):02d}",
                "start": float(seg.start),
                "end": float(seg.end),
            }
        )
    return segments


# ---- auto-K speaker clustering (eigengap on TitaNet centroids) ----
#
# Why this exists
# ---------------
# sherpa-onnx's built-in clustering is plain agglomerative hierarchical
# clustering with a single distance threshold. It has two failure modes
# we hit in production:
#
#   * Threshold too tight  → over-clusters. A 2 hour 1:1 came back as
#     451 "speakers" because every cough, microphone bump, and music
#     interlude produced an embedding far enough from a person's
#     typical centroid to spawn a new micro-cluster.
#   * Threshold too loose  → under-clusters. Two voices that sound
#     similar enough collapse to one.
#
# No single threshold is right for both 5-min calls and 2-hour
# meetings. Production diarizers (pyannote.audio v3.1+, AWS Transcribe)
# solve this by picking K from the *eigengap* of the affinity matrix's
# graph Laplacian rather than from a fixed threshold.
#
# Implementation strategy
# -----------------------
# 1. Run sherpa-onnx diarization with a tight threshold so we get
#    rich per-segment labels. This may produce K_raw clusters where
#    K_raw could be anywhere from 1 to ~hundreds. The segment time
#    boundaries are the real, useful output here.
# 2. Compute one centroid embedding per micro-cluster (only K_raw
#    SpeakerEmbeddingExtractor calls — orders of magnitude fewer than
#    the segment count on long audio, so this is cheap).
# 3. Build cosine-similarity affinity matrix A on those K_raw
#    centroids. Apply normalized graph Laplacian: L = I − D^{-1/2} A D^{-1/2}.
# 4. Eigengap heuristic: the largest gap in the sorted eigenvalues of
#    L picks K_final. This is what pyannote v3.1 and most modern
#    diarizers use for unsupervised K.
# 5. Spectral clustering: take the K_final smallest-eigenvalue
#    eigenvectors as features, row-normalize, k-means → centroid →
#    final-cluster mapping.
# 6. Apply the mapping back to segments. Two segments that sherpa
#    thought were different micro-clusters but whose centroids landed
#    in the same final cluster now share a speaker label.
#
# Costs
# -----
# * Tight-threshold sherpa pass: same wall time as the existing
#   pipeline (we were running it anyway).
# * Centroid extraction: K_raw embeddings × ~10 ms = ~5 s for the
#   pathological 451-cluster Maus case; ~50 ms for typical short calls.
# * Eigendecomposition + k-means: O(K_raw^3) and O(K_raw^2) — sub-second
#   even at K_raw=500.

_AUTO_K_DEFAULT_TIGHT_THRESHOLD = 0.45
_AUTO_K_DEFAULT_K_MAX = 10
_AUTO_K_DEFAULT_K_MIN = 2
_AUTO_K_DEFAULT_MIN_SEG_DURATION = 0.5
# Drop micro-clusters whose total speech time across the entire
# recording is below this threshold. Sub-3-second micro-clusters are
# almost always artefacts (a cough, a music interlude, a brief
# crosstalk) and produce noisy embeddings that swamp the affinity
# matrix's signal. Filtering these out is the single biggest quality
# win for eigengap on long recordings — it's what reduces the Maus
# meeting from 615 noisy centroids to ~10-30 real speaker centroids.
_AUTO_K_DEFAULT_MIN_TOTAL_SECONDS = 3.0
# If the average pairwise cosine similarity between *filtered*
# centroids is ≥ this, treat the recording as a monologue / single
# speaker. The eigengap heuristic alone collapses to K=1 unreliably on
# well-connected affinity graphs, but a high mean affinity is a clear
# domain-specific signal of "actually one person talking".
_AUTO_K_MONOLOGUE_AFFINITY = 0.80


def _extract_centroid_embeddings(
    samples,
    sr: int,
    raw_segments: list[dict],
    *,
    embedding_path: Path,
    num_threads: int = 4,
    min_seg_seconds: float = _AUTO_K_DEFAULT_MIN_SEG_DURATION,
    min_total_seconds: float = _AUTO_K_DEFAULT_MIN_TOTAL_SECONDS,
):
    """For each unique speaker label in ``raw_segments``, find the
    longest segment belonging to that speaker, extract a TitaNet
    embedding for that chunk, and return a mapping
    ``{raw_speaker_label: embedding_vector}``.

    Selection policy:
      * ``min_total_seconds`` (default 3 s): drop micro-clusters whose
        cumulative speech across the recording is below this. These
        are virtually always artefacts (a cough, a music sting, brief
        crosstalk) and their embeddings are noisy enough that
        including them swamps the affinity matrix's signal. Filtering
        them is the biggest quality win for eigengap on long
        recordings.
      * ``min_seg_seconds`` (default 0.5 s): for the surviving
        clusters, prefer chunks ≥ this length when picking the
        representative chunk (longer chunks yield more reliable
        embeddings). Fall back to the longest available chunk if
        nothing meets the bar.

    We pick the longest chunk per cluster (rather than e.g. averaging
    across all chunks) for two reasons:
      * It's much cheaper — K_filtered embeddings instead of N_segments.
      * The longest chunk is the most representative of the cluster's
        true voice (short bumps / coughs / silences carry less signal).
    """
    import numpy as np
    import sherpa_onnx

    by_speaker: dict[str, list[dict]] = {}
    for seg in raw_segments:
        by_speaker.setdefault(seg["speaker"], []).append(seg)

    # Total-duration filter: drop micro-clusters that don't have at
    # least min_total_seconds of speech across the whole recording.
    filtered: dict[str, list[dict]] = {}
    dropped = 0
    for speaker, segs in by_speaker.items():
        total = sum(s["end"] - s["start"] for s in segs)
        if total >= min_total_seconds:
            filtered[speaker] = segs
        else:
            dropped += 1

    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(embedding_path), num_threads=num_threads,
    )
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    centroids: dict[str, "np.ndarray"] = {}
    for speaker, segs in filtered.items():
        long_enough = [s for s in segs if (s["end"] - s["start"]) >= min_seg_seconds]
        pool = long_enough or segs
        rep = max(pool, key=lambda s: s["end"] - s["start"])

        start_sample = max(0, int(rep["start"] * sr))
        end_sample = min(len(samples), int(rep["end"] * sr))
        if end_sample - start_sample < int(0.1 * sr):
            # Audio chunk is too short to extract a stable embedding
            # (TitaNet wants ≥0.1s). Skip — the speaker label will
            # default to the nearest neighbour during remap.
            continue
        chunk = samples[start_sample:end_sample]

        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=sr, waveform=chunk.tolist())
        stream.input_finished()
        emb = np.asarray(extractor.compute(stream), dtype=np.float32)
        # Cosine-similarity assumes unit norm; normalize once here so
        # the affinity matrix builder stays a plain inner product.
        norm = float(np.linalg.norm(emb))
        if norm > 0:
            emb = emb / norm
        centroids[speaker] = emb
    if dropped:
        import logging
        logging.getLogger("local_scribe.diarize_auto").info(
            "_extract_centroid_embeddings: kept %d / %d micro-clusters "
            "(dropped %d below %.1fs total speech)",
            len(centroids), len(by_speaker), dropped, min_total_seconds,
        )
    return centroids


def _pick_k_eigengap(
    affinity: "np.ndarray",
    *,
    k_max: int = _AUTO_K_DEFAULT_K_MAX,
    k_min: int = _AUTO_K_DEFAULT_K_MIN,
    monologue_affinity: float = _AUTO_K_MONOLOGUE_AFFINITY,
) -> tuple[int, list[float]]:
    """Eigengap heuristic on a symmetric, non-negative affinity matrix.

    Returns ``(k_chosen, eigenvalues_sorted)``.

    Algorithm (deviation from textbook eigengap noted below):

      1. **Monologue gate.** If the mean off-diagonal cosine
         similarity between centroids is ≥ ``monologue_affinity``
         (default 0.80), we declare the recording a monologue and
         return K=1. This is a domain-specific signal that textbook
         eigengap can't capture: a graph with all high-similarity
         edges has no near-zero eigenvalues, so eigengap on the
         normalized Laplacian degenerates.

      2. **K_min lower bound** (default 2). For diarization the
         expected case is ≥2 speakers; setting K_min=2 sidesteps the
         trivial λ_0 → λ_1 gap that would otherwise pick K=1 whenever
         the affinity graph is well-connected. (von Luxburg 2007
         eigengap is sound for general spectral clustering but
         conservative for diarization specifically — pyannote.audio,
         AWS Transcribe, etc. all bias toward K≥2.)

      3. **K_max upper bound**: pick K from ``[k_min, k_max]`` (and
         ≤ n − 1) that maximises the gap λ_{k+1} − λ_k.

    For tiny inputs (n ≤ 2 centroids) we short-circuit because
    eigengap is degenerate there.
    """
    import numpy as np

    n = affinity.shape[0]
    if n <= 1:
        return 1, [0.0]
    if n == 2:
        # Two centroids: gap is just (1 − cosine_sim). Treat them as
        # one speaker if very similar (above the monologue threshold),
        # else two.
        sim = float(affinity[0, 1])
        return (1 if sim > monologue_affinity else 2), [0.0, sim]

    # Monologue gate: if the affinity graph is densely high (everyone
    # sounds like everyone else), there's likely just one speaker and
    # the apparent micro-clusters are within-speaker variation.
    iu = np.triu_indices(n, k=1)
    mean_sim = float(np.mean(affinity[iu])) if iu[0].size else 0.0
    if mean_sim >= monologue_affinity:
        return 1, [0.0]

    # Build normalized graph Laplacian: L_sym = I − D^{-1/2} A D^{-1/2}.
    # We clip A to [0, 1] because TitaNet embeddings can produce
    # marginally negative cosine sim between distant speakers and that
    # would make D have negative entries.
    a = np.clip(affinity, 0.0, 1.0)
    d = a.sum(axis=1)
    d_inv_sqrt = 1.0 / np.sqrt(np.maximum(d, 1e-12))
    L = np.eye(n) - (d_inv_sqrt[:, None] * a) * d_inv_sqrt[None, :]

    eigvals = np.sort(np.real(np.linalg.eigvalsh(L)))

    # Compute gaps λ_{k+1} − λ_k for k ∈ [1, k_search].
    k_search = min(k_max, n - 1)
    gaps = np.diff(eigvals[: k_search + 1])
    if len(gaps) == 0:
        return 1, eigvals.tolist()

    # Restrict argmax to k ∈ [k_min, k_max]. gaps[i] is the gap between
    # λ_{i+1} and λ_i, i.e. corresponds to choosing k=i+1, so we slice
    # gaps[k_min-1 : ...] to get gaps for k ∈ [k_min, k_search].
    lo = max(0, k_min - 1)
    hi = len(gaps)
    if lo >= hi:
        # k_min > available k_search → fall back to k_search itself.
        return k_search, eigvals.tolist()
    k_chosen = int(np.argmax(gaps[lo:hi])) + lo + 1
    return k_chosen, eigvals.tolist()


def _spectral_cluster(
    affinity: "np.ndarray", k: int, *, seed: int = 42,
) -> "np.ndarray":
    """Spectral clustering: project rows of A into the K smallest-eigenvalue
    eigenvector space, row-normalize, k-means.

    Returns an integer array of length n with cluster labels in [0, K).
    """
    import numpy as np

    n = affinity.shape[0]
    if k <= 1 or n <= 1:
        return np.zeros(n, dtype=np.int32)
    if k >= n:
        return np.arange(n, dtype=np.int32)

    a = np.clip(affinity, 0.0, 1.0)
    d = a.sum(axis=1)
    d_inv_sqrt = 1.0 / np.sqrt(np.maximum(d, 1e-12))
    L = np.eye(n) - (d_inv_sqrt[:, None] * a) * d_inv_sqrt[None, :]

    eigvals, eigvecs = np.linalg.eigh(L)
    # eigh returns in ascending order — take the k smallest.
    feats = eigvecs[:, :k]
    norms = np.linalg.norm(feats, axis=1, keepdims=True)
    feats = feats / np.maximum(norms, 1e-12)

    # k-means++ on the embedded points. scipy is already a dep (via
    # librosa) so this is free.
    from scipy.cluster.vq import kmeans2
    _, labels = kmeans2(feats, k, minit="++", seed=seed)
    return labels.astype(np.int32)


def _remap_segments(
    raw_segments: list[dict],
    centroid_to_label: dict[str, int],
) -> list[dict]:
    """Apply the {raw_speaker_label: int} mapping to segments and
    re-emit them in the canonical ``SPEAKER_NN`` shape.

    Segments whose raw label has no entry in the mapping (e.g. their
    chunk was too short to embed) inherit the label of their nearest
    neighbour by start time. We never silently drop segments — better
    to ship one mis-attributed word than to leave a hole in the
    transcript.
    """
    sorted_segs = sorted(raw_segments, key=lambda s: s["start"])
    labels: list[int | None] = []
    for seg in sorted_segs:
        labels.append(centroid_to_label.get(seg["speaker"]))

    n = len(labels)
    for i in range(n):
        if labels[i] is not None:
            continue
        # Nearest neighbour fill — search outward.
        for offset in range(1, n):
            j_left = i - offset
            j_right = i + offset
            if j_left >= 0 and labels[j_left] is not None:
                labels[i] = labels[j_left]
                break
            if j_right < n and labels[j_right] is not None:
                labels[i] = labels[j_right]
                break
        else:
            labels[i] = 0

    return [
        {"speaker": f"SPEAKER_{int(lbl):02d}",
         "start": float(seg["start"]),
         "end": float(seg["end"])}
        for seg, lbl in zip(sorted_segs, labels)
    ]


def diarize_auto(
    audio_path: Path,
    *,
    k_max: int = _AUTO_K_DEFAULT_K_MAX,
    k_min: int = _AUTO_K_DEFAULT_K_MIN,
    min_total_seconds: float = _AUTO_K_DEFAULT_MIN_TOTAL_SECONDS,
    monologue_affinity: float = _AUTO_K_MONOLOGUE_AFFINITY,
    tight_threshold: float = _AUTO_K_DEFAULT_TIGHT_THRESHOLD,
    num_threads: int = 4,
    on_progress: Optional[Callable[[float], None]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    request_id: str | None = None,
) -> list[dict]:
    """Diarize ``audio_path`` and pick the speaker count automatically
    via the eigengap heuristic.

    Output shape matches ``diarize()``:
        [{"speaker": "SPEAKER_NN", "start": s, "end": e}, ...]

    Steps:
      1. Run sherpa-onnx diarization with a tight threshold to get
         segments + over-clustered labels.
      2. Centroid embedding per micro-cluster.
      3. Eigengap on the centroid affinity → K.
      4. Spectral re-cluster centroids to K.
      5. Remap segment labels.

    On any pipeline error the function falls back to a single-speaker
    transcript rather than raising — better to ship something than a
    hard error mid-Generate.
    """
    import logging
    import numpy as np

    log = logging.getLogger("local_scribe.diarize_auto")
    log_id = f"[diarize_auto {request_id}] " if request_id else ""

    seg_path, emb_path = ensure_models(cache_dir)

    # 1) Tight-threshold sherpa pass (segmentation + over-clustering).
    #    The over-clustering is the point: we WANT each chunk to land
    #    in its own micro-cluster so we can re-cluster cleanly below.
    raw_segments = diarize(
        audio_path,
        num_clusters=None,
        cluster_threshold=tight_threshold,
        num_threads=num_threads,
        on_progress=on_progress,
        cache_dir=cache_dir,
    )
    if not raw_segments:
        log.warning("%sno segments returned by sherpa; falling back to "
                    "single speaker", log_id)
        return []

    raw_speaker_count = len({s["speaker"] for s in raw_segments})
    log.info(
        "%ssherpa returned %d segments across %d micro-clusters; "
        "running auto-K eigengap to pick final speaker count "
        "(k_max=%d)",
        log_id, len(raw_segments), raw_speaker_count, k_max,
    )

    if raw_speaker_count <= 1:
        # Sherpa already collapsed to one speaker — eigengap can't
        # do better. Return as-is.
        log.info("%sfinal K = 1 (sherpa collapsed)", log_id)
        return raw_segments

    # 2) Centroid embedding per micro-cluster (with the noise filter
    #    that drops sub-3s phantoms — the single biggest quality win
    #    for eigengap on long recordings).
    samples, sr = _load_wav_16k_mono(audio_path)
    centroids = _extract_centroid_embeddings(
        samples, sr, raw_segments,
        embedding_path=emb_path, num_threads=num_threads,
        min_total_seconds=min_total_seconds,
    )
    if len(centroids) <= 1:
        log.info(
            "%sonly %d centroid(s) survived the noise filter; "
            "treating as single speaker",
            log_id, len(centroids),
        )
        # Collapse all raw segments to one speaker — the surviving
        # centroid (if any) is the only "real" voice in the recording.
        return [
            {"speaker": "SPEAKER_00",
             "start": float(s["start"]), "end": float(s["end"])}
            for s in sorted(raw_segments, key=lambda s: s["start"])
        ]

    # 3) Build affinity matrix on centroids and pick K via eigengap.
    speaker_keys = list(centroids.keys())
    emb_matrix = np.stack([centroids[k] for k in speaker_keys])
    affinity = emb_matrix @ emb_matrix.T  # cosine sim (already unit-normed)

    k_final, eigvals = _pick_k_eigengap(
        affinity, k_max=k_max, k_min=k_min,
        monologue_affinity=monologue_affinity,
    )
    log.info(
        "%seigengap chose K=%d (filtered_centroids=%d, eigvals[:%d]=%s)",
        log_id, k_final, len(speaker_keys),
        min(k_max + 1, len(eigvals)),
        [f"{v:.3f}" for v in eigvals[: min(k_max + 1, len(eigvals))]],
    )

    # 4) Spectral cluster the centroids to K.
    centroid_labels = _spectral_cluster(affinity, k_final)

    # 5) Build {raw_speaker → final_label} and remap.
    centroid_to_label: dict[str, int] = {
        speaker_keys[i]: int(centroid_labels[i])
        for i in range(len(speaker_keys))
    }
    return _remap_segments(raw_segments, centroid_to_label)


def attach_speaker_to_words(
    words: list[dict], turns: list[dict]
) -> list[dict]:
    """Given Deepgram-shaped `words` (each with start/end) and diarization
    `turns` (start/end/speaker), return a new list of words with a
    `speaker` field attached. A word is assigned to whichever turn
    contains its midpoint; if no turn matches, the nearest turn wins."""
    if not turns:
        return [dict(w, speaker="SPEAKER_00") for w in words]

    sorted_turns = sorted(turns, key=lambda t: t["start"])

    def find_speaker(word_mid: float) -> str:
        # exact-overlap fast path
        for t in sorted_turns:
            if t["start"] <= word_mid <= t["end"]:
                return t["speaker"]
        # nearest-turn fallback (handles small gaps in diarization)
        nearest = min(
            sorted_turns,
            key=lambda t: min(
                abs(t["start"] - word_mid), abs(t["end"] - word_mid)
            ),
        )
        return nearest["speaker"]

    out = []
    for w in words:
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)
        mid = (start + end) / 2.0
        out.append(dict(w, speaker=find_speaker(mid)))
    return out


def group_words_into_lines(
    diarized_words: list[dict],
    *,
    max_gap_seconds: float = 1.5,
) -> list[dict]:
    """Collapse a flat list of diarized words into spoken "lines" where
    each line is one speaker speaking continuously. A gap of more than
    `max_gap_seconds` between words from the same speaker also starts a
    new line (so the transcript reads as natural turns rather than one
    huge run-on per speaker)."""
    lines: list[dict] = []
    cur: dict | None = None
    for w in diarized_words:
        speaker = w.get("speaker") or "SPEAKER_00"
        word_text = w.get("punctuated_word") or w.get("word") or ""
        if not word_text:
            continue
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)

        if (
            cur is None
            or cur["speaker"] != speaker
            or (start - cur["end"]) > max_gap_seconds
        ):
            cur = {
                "speaker": speaker,
                "start": start,
                "end": end,
                "words": [word_text],
            }
            lines.append(cur)
        else:
            cur["words"].append(word_text)
            cur["end"] = end
    for line in lines:
        line["text"] = " ".join(line["words"]).strip()
        line["text"] = re.sub(r"\s+([,.!?;:])", r"\1", line["text"])
    return lines


def render_diarized_transcript(
    lines: list[dict],
    *,
    speaker_labels: dict[str, str] | None = None,
    include_timestamps: bool = False,
) -> str:
    """Pretty-print diarized lines as 'Name: text' (or 'SPEAKER_NN: text'
    if no mapping). Timestamps optional - we keep them off by default for
    paste-back to Char but they're useful for debugging.

    `speaker_labels` should be a dict like {"SPEAKER_00": "Moss",
    "SPEAKER_01": "Caller"}. Missing keys fall back to the raw id."""
    out_lines: list[str] = []
    for line in lines:
        spk = line["speaker"]
        label = (speaker_labels or {}).get(spk, spk)
        if include_timestamps:
            ts = f"[{_fmt_ts(line['start'])} - {_fmt_ts(line['end'])}] "
        else:
            ts = ""
        out_lines.append(f"{ts}{label}: {line['text']}")
    return "\n".join(out_lines)


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - 3600 * h - 60 * m
    if h:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


SPEAKER_NAMING_SYSTEM_PROMPT = """\
You are an expert at identifying speakers from a call transcript. The user
will give you the first chunk of a diarized transcript that uses placeholder
labels (SPEAKER_00, SPEAKER_01, ...). Read the conversation and infer the
real name (or descriptive role) of each speaker based on:

  - explicit self-introductions ("Hi, this is Alex...")
  - other speakers addressing them by name ("So Alex, what do you think?")
  - role/affiliation context (a support agent, the customer, the host, etc.)

Output ONLY a JSON object that maps every SPEAKER_xx label that appears in
the input to a name or role string. Do NOT add any other prose. Use the
shortest reasonable label - first names if introduced, otherwise a role.
Use "Caller" / "Host" / "Customer" / "Support agent" if no name is given.

Example output:
{"SPEAKER_00": "Moss", "SPEAKER_01": "Caller"}
"""


def infer_speaker_labels(
    diarized_text: str,
    *,
    llm_url: str,
    llm_model: str,
    max_chars: int = 4000,
    timeout: float = 120.0,
) -> dict[str, str]:
    """Ask the local LLM to map SPEAKER_NN labels to real names. Returns
    a dict; on any failure (network error, malformed JSON, missing
    speakers) returns the empty dict, and the caller is expected to fall
    back to the raw labels."""
    speakers_seen = sorted(set(re.findall(r"SPEAKER_\d{2}", diarized_text)))
    if not speakers_seen:
        return {}

    sample = diarized_text[:max_chars]
    user_msg = (
        f"Speakers present in this transcript: {', '.join(speakers_seen)}.\n"
        f"Map each to a short name (preferred) or role.\n\n"
        f"Transcript excerpt:\n\n{sample}\n"
    )
    try:
        resp = requests.post(
            llm_url,
            json={
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": SPEAKER_NAMING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": 256,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return {}

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    match = re.search(r"\{[^{}]*\}", content, flags=re.DOTALL)
    if not match:
        return {}
    try:
        mapping = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(mapping, dict):
        return {}

    cleaned: dict[str, str] = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if not re.fullmatch(r"SPEAKER_\d{2}", k):
            continue
        v = v.strip()
        if v:
            cleaned[k] = v
    return cleaned
