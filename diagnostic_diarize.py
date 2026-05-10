"""One-off diagnostic: probe auto-K vs. forced-K results on a single
audio file.

Goal: when a user reports "this call had N speakers but auto-K picked
M < N", we need a concrete answer to:

  * Are the missing speakers even DETECTABLE in the audio? (i.e. does
    sherpa surface a stable cluster for them?)
  * What's the per-cluster total speech time? (a 30-second-only quiet
    speaker will hover near our noise filter's cutoff)
  * What do the eigenvalues look like — is there a clear K=N gap that
    we're missing, or is the affinity matrix genuinely indistinct?
  * Does forcing K=3, 4, 5 produce a sensible split (centroids well
    separated, each speaker carries meaningful airtime), or does it
    just split one real speaker in half?

This is a one-shot diagnostic, not part of the runtime pipeline. It
intentionally does NOT write transcript.json — we only want to inspect.
"""

from __future__ import annotations

import argparse
import collections
import logging
import sys
from pathlib import Path

import numpy as np

import diarization_backend as dz


def _summarise_clusters(segments: list[dict]) -> list[tuple[str, float, int]]:
    """Return [(speaker_label, total_seconds, num_segments)] sorted by
    total_seconds desc."""
    by = collections.defaultdict(lambda: [0.0, 0])
    for s in segments:
        by[s["speaker"]][0] += s["end"] - s["start"]
        by[s["speaker"]][1] += 1
    out = [(spk, t, n) for spk, (t, n) in by.items()]
    out.sort(key=lambda x: -x[1])
    return out


def _silhouette_proxy(affinity: np.ndarray, labels: np.ndarray) -> float:
    """Quick silhouette proxy on a precomputed affinity matrix.
    Returns mean (a − b) / max(a, b) across points where:
      a = mean similarity to other points in the same cluster
      b = max mean similarity to points in any OTHER cluster
    Higher → cleaner clustering. ~0 means barely-separated, < 0 means
    points sit closer to a different cluster on average than their own.
    """
    n = affinity.shape[0]
    unique = np.unique(labels)
    if len(unique) < 2:
        return 0.0
    s = np.zeros(n)
    for i in range(n):
        own = labels[i]
        sims = {}
        for u in unique:
            mask = labels == u
            if u == own:
                mask = mask.copy()
                mask[i] = False
            if not mask.any():
                continue
            sims[u] = float(np.mean(affinity[i, mask]))
        if own not in sims:
            continue
        a = sims[own]
        b = max(v for u, v in sims.items() if u != own)
        denom = max(abs(a), abs(b)) or 1.0
        s[i] = (a - b) / denom
    return float(np.mean(s))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("audio_path")
    p.add_argument("--k-max", type=int, default=10)
    p.add_argument("--min-total-seconds", type=float, default=3.0)
    p.add_argument("--also-force", type=int, nargs="*", default=[3, 4, 5],
                   help="Additional K values to force AHC at, for comparison")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("diagnostic")

    audio_path = Path(args.audio_path).expanduser().resolve()
    if not audio_path.is_file():
        log.error("audio not found: %s", audio_path)
        return 1

    seg_path, emb_path = dz.ensure_models()

    print(f"\n=== diagnostic: {audio_path.name} ===")
    print(f"loading audio ...")
    samples, sr = dz._load_wav_16k_mono(audio_path)
    print(f"  duration = {len(samples)/sr/60:.1f} min @ {sr} Hz\n")

    # ------------------------------------------------------------------
    # 1) Tight-threshold sherpa pass (raw micro-clusters)
    # ------------------------------------------------------------------
    print("1) sherpa-onnx pass (tight threshold = 0.45, K auto) ...")
    raw = dz._diarize_samples(samples, sr, cluster_threshold=0.45)
    by_raw = _summarise_clusters(raw)
    print(f"   sherpa returned {len(raw)} segments across "
          f"{len(by_raw)} micro-clusters.")
    print(f"   top 15 micro-clusters by total speech time:")
    for spk, t, n in by_raw[:15]:
        print(f"     {spk}  total={t:6.1f}s  segs={n:>4}")
    print()

    # ------------------------------------------------------------------
    # 2) Centroid filter + embedding extraction
    # ------------------------------------------------------------------
    print(f"2) extracting centroids (filter < {args.min_total_seconds}s total speech) ...")
    centroids = dz._extract_centroid_embeddings(
        samples, sr, raw,
        embedding_path=emb_path, num_threads=4,
        min_total_seconds=args.min_total_seconds,
    )
    keys = list(centroids.keys())
    print(f"   {len(keys)} centroids survived the filter.")
    if len(keys) <= 1:
        print("   (degenerate — auto-K can't do anything here)")
        return 0
    emb_mat = np.stack([centroids[k] for k in keys])
    affinity = emb_mat @ emb_mat.T  # cosine sim (already unit-normed)
    iu = np.triu_indices(len(keys), k=1)
    print(f"   mean off-diagonal cosine sim = {float(np.mean(affinity[iu])):.3f}")
    print(f"   stddev                        = {float(np.std(affinity[iu])):.3f}")
    print(f"   min                           = {float(np.min(affinity[iu])):.3f}")
    print(f"   max                           = {float(np.max(affinity[iu])):.3f}")
    print()

    # ------------------------------------------------------------------
    # 3) Eigengap + spectral clustering at different K values
    # ------------------------------------------------------------------
    print("3) eigengap analysis (with k_min=2, monologue gate at 0.80) ...")
    k_auto, eigvals = dz._pick_k_eigengap(
        affinity, k_max=args.k_max, k_min=2,
        monologue_affinity=0.80,
    )
    print(f"   eigvals[:{args.k_max+1}] = {[f'{v:.3f}' for v in eigvals[:args.k_max+1]]}")
    gaps = np.diff(eigvals[:args.k_max+1])
    print(f"   gaps                = {[f'{v:.3f}' for v in gaps]}")
    print(f"   auto-K chose K = {k_auto}")
    print()

    # Score every plausible K with the silhouette proxy.
    print("4) silhouette proxy across K ∈ [2, k_max]:")
    print(f"   {'K':>3}  {'silhouette':>10}  {'eigengap':>10}  cluster sizes (top {min(8, len(keys))} by airtime)")
    for k in range(2, min(args.k_max + 1, len(keys) + 1)):
        labels = dz._spectral_cluster(affinity, k)
        sil = _silhouette_proxy(affinity, labels)
        # Map labels back through raw segments to compute per-final-cluster airtime.
        centroid_to_label = {keys[i]: int(labels[i]) for i in range(len(keys))}
        remapped = dz._remap_segments(raw, centroid_to_label)
        final_summary = _summarise_clusters(remapped)
        sizes = ",".join(f"{t:.0f}s" for _, t, _ in final_summary[:8])
        eig_gap = float(eigvals[k] - eigvals[k - 1]) if k < len(eigvals) else 0.0
        print(f"   {k:>3}  {sil:>+10.3f}  {eig_gap:>10.3f}  {sizes}")

    # ------------------------------------------------------------------
    # 5) Forced AHC paths (sanity check: do we even have enough signal?)
    # ------------------------------------------------------------------
    print()
    print("5) forced AHC at sherpa-onnx with num_clusters=N (sanity check):")
    for k in args.also_force:
        try:
            forced = dz._diarize_samples(samples, sr, num_clusters=k)
        except Exception as exc:
            print(f"   K={k}: ERROR {exc}")
            continue
        forced_summary = _summarise_clusters(forced)
        sizes = ",".join(f"{t:.0f}s" for _, t, _ in forced_summary)
        print(f"   K={k}: {sizes}")

    print()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
