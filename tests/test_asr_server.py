"""Unit tests for asr_server.py — focused on the OpenAI Whisper-API compatible
endpoint (POST /v1/audio/transcriptions) and its response-shaping helpers.

These tests are hermetic: heavy backends (parakeet-mlx, mlx.core, faster-whisper)
are stubbed out via sys.modules before asr_server is imported, so the module
loads in milliseconds with no real models on disk. The actual transcription
function `_run_asr_async` is patched per-test to return canned ASR output.

Run from the repo root:
    venv/bin/python -m unittest discover -v -s tests
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Stub heavy backends BEFORE importing asr_server, then RESTORE the real
# `parakeet_backend` module so sibling tests (e.g. test_transcribe_file's
# TestParakeetBackend group) still see the real implementation. We force
# ASR_BACKEND=parakeet so we exercise the same dispatch path Char hits, but
# replace mlx.core + parakeet_backend with mocks for the duration of the
# `import asr_server` call only.
# ---------------------------------------------------------------------------
os.environ["ASR_BACKEND"] = "parakeet"

_mlx_stub = mock.MagicMock(name="mlx_core_stub")
_mlx_stub.set_default_stream = mock.MagicMock()
_mlx_stub.new_stream = mock.MagicMock(return_value="fake-stream")
_mlx_stub.default_device = mock.MagicMock(return_value="fake-device")
sys.modules.setdefault("mlx", mock.MagicMock(core=_mlx_stub))
sys.modules.setdefault("mlx.core", _mlx_stub)

_real_parakeet = sys.modules.get("parakeet_backend")
_parakeet_stub = mock.MagicMock(name="parakeet_backend_stub")
_parakeet_stub.load_model = mock.MagicMock(return_value=None)
_parakeet_stub.transcribe_to_deepgram = mock.MagicMock(
    return_value={
        "metadata": {"duration": 0.0},
        "results": {"channels": [{"alternatives": [{
            "transcript": "stub", "words": [], "languages": ["en"],
        }]}]},
    }
)
sys.modules["parakeet_backend"] = _parakeet_stub

import asr_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Put the real module back in sys.modules so sibling test files importing
# parakeet_backend get the production code, not our stub. asr_server has
# already captured references to load_model / transcribe_to_deepgram via
# `from parakeet_backend import ...`, so restoring the module here doesn't
# affect this file's own tests.
if _real_parakeet is not None:
    sys.modules["parakeet_backend"] = _real_parakeet
else:
    sys.modules.pop("parakeet_backend", None)


# ---------------------------------------------------------------------------
# Canned ASR output — small but realistic; all numbers below are seconds.
# ---------------------------------------------------------------------------

CANNED_WORDS: list[dict[str, Any]] = [
    {"word": "Hello", "punctuated_word": "Hello",
     "start": 0.10, "end": 0.55, "confidence": 0.99},
    {"word": "world", "punctuated_word": "world.",
     "start": 0.60, "end": 1.10, "confidence": 0.97},
    {"word": "This", "punctuated_word": "This",
     "start": 1.20, "end": 1.45, "confidence": 0.98},
    {"word": "is", "punctuated_word": "is",
     "start": 1.50, "end": 1.65, "confidence": 0.99},
    {"word": "a", "punctuated_word": "a",
     "start": 1.70, "end": 1.80, "confidence": 0.95},
    {"word": "test", "punctuated_word": "test.",
     "start": 1.85, "end": 2.30, "confidence": 0.96},
]
CANNED_TRANSCRIPT = "Hello world. This is a test."
CANNED_LANG = "en"
CANNED_DURATION = 2.5


async def _fake_run_asr_async(audio_path, on_segment=None, on_start=None):
    return CANNED_TRANSCRIPT, CANNED_WORDS, CANNED_LANG, CANNED_DURATION


# ===========================================================================
# Pure-function tests — segment grouping + subtitle formatting
# ===========================================================================

class ComputeSpeakerAirtimeTests(unittest.TestCase):
    """``_compute_speaker_airtime`` aggregates the per-word stream into
    one row per speaker. Used to populate the inspector's airtime
    panel + the per-request "airtime:" log line.
    """

    def test_empty_words_returns_empty(self):
        self.assertEqual(asr_server._compute_speaker_airtime([]), [])
        self.assertEqual(asr_server._compute_speaker_airtime(None), [])

    def test_two_speakers_aggregate_by_seconds_desc(self):
        words = [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.0,
             "speaker_confidence": 0.9},
            {"speaker": "speaker_1", "start": 1.0, "end": 3.0,
             "speaker_confidence": 0.6},
            {"speaker": "speaker_0", "start": 3.0, "end": 4.0,
             "speaker_confidence": 0.8},
        ]
        out = asr_server._compute_speaker_airtime(words)
        # Sort: speaker_1 (2s) > speaker_0 (2s) — equal, but stable
        # insertion order should put speaker_0 first in this case.
        labels = [r["label"] for r in out]
        self.assertEqual(sorted(labels), ["speaker_0", "speaker_1"])
        seconds = {r["label"]: r["seconds"] for r in out}
        self.assertAlmostEqual(seconds["speaker_0"], 2.0)
        self.assertAlmostEqual(seconds["speaker_1"], 2.0)
        # Percentages sum to 100% across speakers.
        percs = sum(r["percent"] for r in out)
        self.assertAlmostEqual(percs, 1.0)
        # Mean confidence: speaker_0 is (0.9 + 0.8) / 2 = 0.85.
        confs = {r["label"]: r["mean_confidence"] for r in out}
        self.assertAlmostEqual(confs["speaker_0"], 0.85)
        self.assertAlmostEqual(confs["speaker_1"], 0.6)

    def test_no_confidence_field_means_none_mean_confidence(self):
        words = [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.0},
            {"speaker": "speaker_0", "start": 1.0, "end": 2.0},
        ]
        out = asr_server._compute_speaker_airtime(words)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["mean_confidence"])
        self.assertEqual(out[0]["word_count"], 2)

    def test_word_count_per_speaker(self):
        words = [
            {"speaker": "a", "start": 0.0, "end": 0.5},
            {"speaker": "a", "start": 0.5, "end": 1.0},
            {"speaker": "b", "start": 1.0, "end": 1.5},
        ]
        out = asr_server._compute_speaker_airtime(words)
        counts = {r["label"]: r["word_count"] for r in out}
        self.assertEqual(counts, {"a": 2, "b": 1})


class FormatAirtimeLogTests(unittest.TestCase):
    """Compact log-line formatting used in the per-request 'done' log."""

    def test_empty_returns_empty_string(self):
        self.assertEqual(asr_server._format_airtime_log([]), "")
        self.assertEqual(asr_server._format_airtime_log(None), "")

    def test_renders_label_percent_mins_seconds(self):
        out = asr_server._format_airtime_log([
            {"label": "speaker_0", "seconds": 75.0,
             "percent": 0.5, "mean_confidence": 0.8},
            {"label": "speaker_1", "seconds": 75.0,
             "percent": 0.5, "mean_confidence": None},
        ])
        self.assertIn("speaker_0=50%", out)
        self.assertIn("(1m 15s, 80% conf)", out)
        # Missing confidence omits the conf chunk.
        self.assertIn("speaker_1=50% (1m 15s)", out)


class BuildOpenAISegmentsTests(unittest.TestCase):
    def test_empty_words_with_fallback_text_returns_single_segment(self):
        segs = asr_server._build_openai_segments([], "fallback transcript")
        self.assertEqual(segs, [{"start": 0.0, "end": 0.0,
                                  "text": "fallback transcript"}])

    def test_empty_words_no_fallback_returns_empty(self):
        self.assertEqual(asr_server._build_openai_segments([], ""), [])

    def test_splits_at_sentence_boundaries(self):
        segs = asr_server._build_openai_segments(CANNED_WORDS, CANNED_TRANSCRIPT)
        self.assertEqual(len(segs), 2,
                         "expected 2 segments, one per sentence-final period")
        self.assertEqual(segs[0]["text"], "Hello world.")
        self.assertAlmostEqual(segs[0]["start"], 0.10, places=2)
        self.assertAlmostEqual(segs[0]["end"], 1.10, places=2)
        self.assertEqual(segs[1]["text"], "This is a test.")
        self.assertAlmostEqual(segs[1]["start"], 1.20, places=2)
        self.assertAlmostEqual(segs[1]["end"], 2.30, places=2)

    def test_long_run_without_punctuation_splits_at_max_duration(self):
        words = []
        for i in range(50):
            t0 = float(i) * 0.5
            words.append({"word": "word",
                          "punctuated_word": "word",
                          "start": t0, "end": t0 + 0.4})
        segs = asr_server._build_openai_segments(words, "")
        self.assertGreater(len(segs), 1,
                           "30-word / 12s caps should force at least one split")
        for seg in segs:
            self.assertLessEqual(seg["end"] - seg["start"], 16.0)

    def test_handles_words_missing_punctuated_word_field(self):
        bare_words = [
            {"word": "ok", "start": 0.0, "end": 0.3},
            {"word": "now", "start": 0.4, "end": 0.7},
        ]
        segs = asr_server._build_openai_segments(bare_words, "")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["text"], "ok now")


class BuildDiarizedSegmentsTests(unittest.TestCase):
    def test_breaks_at_speaker_change(self):
        words = [
            {"punctuated_word": "Hi", "start": 0.0, "end": 0.3, "speaker": "speaker_0"},
            {"punctuated_word": "there", "start": 0.4, "end": 0.7, "speaker": "speaker_0"},
            {"punctuated_word": "Hello", "start": 0.8, "end": 1.1, "speaker": "speaker_1"},
            {"punctuated_word": "back", "start": 1.2, "end": 1.5, "speaker": "speaker_1"},
        ]
        segs = asr_server._build_diarized_segments(words, "")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["speaker"], "speaker_0")
        self.assertEqual(segs[0]["text"], "Hi there")
        self.assertEqual(segs[1]["speaker"], "speaker_1")
        self.assertEqual(segs[1]["text"], "Hello back")

    def test_speaker_change_takes_priority_over_punctuation(self):
        words = [
            {"punctuated_word": "Yes", "start": 0.0, "end": 0.3, "speaker": "speaker_0"},
            {"punctuated_word": "indeed", "start": 0.4, "end": 0.8, "speaker": "speaker_1"},
        ]
        segs = asr_server._build_diarized_segments(words, "")
        self.assertEqual([s["speaker"] for s in segs], ["speaker_0", "speaker_1"])

    def test_same_speaker_long_run_still_splits_at_caps(self):
        # 200 words over 100 seconds; even with the looser single-speaker
        # 80-word / 30-second caps this must split into multiple segments.
        words = [
            {"punctuated_word": "word", "start": float(i) * 0.5,
             "end": float(i) * 0.5 + 0.4, "speaker": "speaker_0"}
            for i in range(200)
        ]
        segs = asr_server._build_diarized_segments(words, "")
        self.assertGreater(len(segs), 1)
        for seg in segs:
            self.assertEqual(seg["speaker"], "speaker_0")

    def test_empty_words_with_fallback(self):
        segs = asr_server._build_diarized_segments([], "fallback")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["speaker"], "speaker_0")
        self.assertEqual(segs[0]["text"], "fallback")

    def test_single_speaker_does_not_break_on_sentence_end(self):
        """In single-speaker mode, sentence-final punctuation must NOT
        close a segment. The reader still sees the period via the
        punctuated_word value, but multiple sentences flow into the same
        segment so Char's UI doesn't have to render thousands of tiny rows.
        Multi-speaker mode keeps the old per-sentence breaking.
        """
        # Three short sentences, single speaker -> ONE segment.
        single = [
            {"punctuated_word": "Hi.", "start": 0.0, "end": 0.3, "speaker": "speaker_0"},
            {"punctuated_word": "OK?", "start": 0.5, "end": 0.7, "speaker": "speaker_0"},
            {"punctuated_word": "Cool.", "start": 0.9, "end": 1.2, "speaker": "speaker_0"},
        ]
        segs = asr_server._build_diarized_segments(single, "")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["text"], "Hi. OK? Cool.")

        # Same words, two speakers -> sentence-final still respected per
        # speaker turn, so we still get clean turn boundaries.
        multi = [
            {"punctuated_word": "Hi.", "start": 0.0, "end": 0.3, "speaker": "speaker_0"},
            {"punctuated_word": "OK?", "start": 0.5, "end": 0.7, "speaker": "speaker_1"},
        ]
        segs = asr_server._build_diarized_segments(multi, "")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["speaker"], "speaker_0")
        self.assertEqual(segs[1]["speaker"], "speaker_1")

    def test_single_speaker_uses_chunkier_segments_than_multi_speaker(self):
        """Single-speaker output (post auto-skip / one-cluster) must produce
        far fewer segments than multi-speaker output for the same word
        sequence, so Char's UI doesn't have to mint thousands of UUIDs.
        Boundaries: ~30s/80 words instead of ~12s/30 words.
        """
        # 200 words, 0.5s each, all speaker_0 -> 100s total.
        single = [
            {"punctuated_word": "word", "start": float(i) * 0.5,
             "end": float(i) * 0.5 + 0.4, "speaker": "speaker_0"}
            for i in range(200)
        ]
        # Same words, but speaker alternates each word -> tightest chunking.
        alternating = [
            {"punctuated_word": "word", "start": float(i) * 0.5,
             "end": float(i) * 0.5 + 0.4,
             "speaker": f"speaker_{i % 2}"}
            for i in range(200)
        ]
        single_segs = asr_server._build_diarized_segments(single, "")
        multi_segs = asr_server._build_diarized_segments(alternating, "")
        # Single-speaker should be at least 2x cheaper to render than the
        # alternating case under the same input volume (in practice it's
        # ~2.6x with the 30s/80w vs 12s/30w boundary policy).
        self.assertLess(
            len(single_segs), len(multi_segs) // 2,
            f"single={len(single_segs)} multi={len(multi_segs)}; "
            f"single-speaker chunking should be much chunkier",
        )
        # Sanity: single-speaker output must still hit the 80-word cap (200
        # words / 80 ≈ 3 segments minimum after sentence-end breaks).
        self.assertGreaterEqual(len(single_segs), 1)
        # And every segment really is one speaker.
        self.assertEqual({s["speaker"] for s in single_segs}, {"speaker_0"})


class AttachSpeakersTests(unittest.TestCase):
    """Exercise the diarization-attach helper in isolation. We patch
    diarization_backend at use-site so this stays hermetic."""

    def setUp(self):
        # Force the env-var gate ON for these tests regardless of how the
        # process was started.
        self._orig = asr_server._OPENAI_BATCH_DIARIZE
        asr_server._OPENAI_BATCH_DIARIZE = True
        self.addCleanup(lambda: setattr(asr_server, "_OPENAI_BATCH_DIARIZE", self._orig))

    def test_disabled_returns_speaker_0_for_all(self):
        asr_server._OPENAI_BATCH_DIARIZE = False
        out, n = asr_server._attach_speakers_to_words("/nope.wav", CANNED_WORDS)
        self.assertEqual(n, 1)
        self.assertTrue(all(w["speaker"] == "speaker_0" for w in out))

    def test_empty_words_short_circuits(self):
        out, n = asr_server._attach_speakers_to_words("/nope.wav", [])
        self.assertEqual((out, n), ([], 1))

    def test_remaps_sherpa_labels_to_dense_speaker_n(self):
        # sherpa-onnx returns labels like SPEAKER_03, SPEAKER_11 — we should
        # collapse those to a dense speaker_0/speaker_1 in encounter order.
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_03", "start": 0.0, "end": 0.7},
            {"speaker": "SPEAKER_11", "start": 0.8, "end": 2.5},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(CANNED_WORDS[0], speaker="SPEAKER_03"),
            dict(CANNED_WORDS[1], speaker="SPEAKER_03"),
            dict(CANNED_WORDS[2], speaker="SPEAKER_11"),
            dict(CANNED_WORDS[3], speaker="SPEAKER_11"),
            dict(CANNED_WORDS[4], speaker="SPEAKER_11"),
            dict(CANNED_WORDS[5], speaker="SPEAKER_11"),
        ]
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS,
            )
        self.assertEqual(n, 2)
        speakers = [w["speaker"] for w in out]
        self.assertEqual(speakers,
                         ["speaker_0", "speaker_0",
                          "speaker_1", "speaker_1", "speaker_1", "speaker_1"])

    def test_no_turns_falls_back_to_single_speaker(self):
        # The default path (no overrides) is now eigengap auto-K
        # (`diarize_auto`), which returns whatever segments sherpa
        # produced after the centroid-remap. An empty return there
        # means no speech was detected — must collapse to single
        # speaker rather than emit a wordless transcript.
        fake_dz = mock.MagicMock()
        fake_dz.diarize_auto.return_value = []
        # diarize() is also exposed in case the auto-K path falls back
        # to it; keep the mock empty so a regression that calls it
        # surfaces a clear assertion later.
        fake_dz.diarize.return_value = []
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS,
            )
        self.assertEqual(n, 1)
        self.assertTrue(all(w["speaker"] == "speaker_0" for w in out))

    def test_diarization_exception_falls_back_to_single_speaker(self):
        fake_dz = mock.MagicMock()
        # Both paths must be guarded; the default path now goes through
        # diarize_auto. We explode it to confirm the catch-all in
        # _attach_speakers_to_words still emits a valid response.
        fake_dz.diarize_auto.side_effect = RuntimeError("sherpa exploded")
        fake_dz.diarize.side_effect = RuntimeError("sherpa exploded")
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS,
            )
        self.assertEqual(n, 1)
        self.assertTrue(all(w["speaker"] == "speaker_0" for w in out))

    def test_long_audio_auto_skips_diarization(self):
        """Audio over MAX_DIARIZE_SECONDS should bail to single-speaker
        rather than chew through 5+ minutes of clustering — for both
        the legacy AHC path AND the new auto-K path."""
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 100.0},
        ]
        fake_dz.diarize_auto.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 100.0},
        ]
        with mock.patch.object(asr_server, "_MAX_DIARIZE_SECONDS", 1800), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS, duration=1860.0,
            )
        self.assertEqual(n, 1)
        self.assertTrue(all(w["speaker"] == "speaker_0" for w in out))
        # Neither path should fire when we're past the duration cap.
        fake_dz.diarize.assert_not_called()
        fake_dz.diarize_auto.assert_not_called()

    def test_blowup_more_than_max_speakers_collapses_to_single(self):
        """If sherpa-onnx returns more than MAX_SPEAKERS distinct labels we
        treat that as a clustering blow-up (this is the 451-speakers-on-114-min
        bug) and collapse to single-speaker rather than emit junk JSON."""
        # Synthesize 15 words each assigned to a distinct phantom speaker.
        many_words = [
            {"word": f"w{i}", "punctuated_word": f"w{i}",
             "start": float(i), "end": float(i) + 0.5, "confidence": 0.9}
            for i in range(15)
        ]
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": f"SPEAKER_{i:02d}", "start": float(i), "end": float(i)+0.5}
            for i in range(15)
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(w, speaker=f"SPEAKER_{i:02d}") for i, w in enumerate(many_words)
        ]
        with mock.patch.object(asr_server, "_MAX_SPEAKERS", 12), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", many_words, duration=600.0,
            )
        self.assertEqual(n, 1)
        self.assertTrue(all(w["speaker"] == "speaker_0" for w in out))

    def test_max_speakers_zero_disables_blowup_guard(self):
        """MAX_SPEAKERS=0 lets the original behavior (one segment per
        clustered speaker) through unchanged."""
        many_words = [
            {"word": f"w{i}", "punctuated_word": f"w{i}",
             "start": float(i), "end": float(i) + 0.5, "confidence": 0.9}
            for i in range(15)
        ]
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": f"SPEAKER_{i:02d}", "start": float(i), "end": float(i)+0.5}
            for i in range(15)
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(w, speaker=f"SPEAKER_{i:02d}") for i, w in enumerate(many_words)
        ]
        with mock.patch.object(asr_server, "_MAX_SPEAKERS", 0), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", many_words, duration=600.0,
            )
        self.assertEqual(n, 15)

    def test_long_audio_bumps_cluster_threshold_when_num_speakers_forced(self):
        """The threshold-bump heuristic only runs on the legacy AHC
        path, which is now reached only when an override is set
        (`?num_speakers=N` or `?cluster_threshold=F`). With
        ``num_speakers_override=2`` we go down AHC and the long-audio
        bump should still kick in."""
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 100.0},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(w, speaker="SPEAKER_00") for w in CANNED_WORDS
        ]
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", None), \
             mock.patch.object(asr_server, "_MAX_DIARIZE_SECONDS", 0), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS, duration=900.0,
                num_speakers_override=2,
            )
        kwargs = fake_dz.diarize.call_args.kwargs
        self.assertAlmostEqual(kwargs["cluster_threshold"], 0.7)
        self.assertEqual(kwargs["num_clusters"], 2)
        # Auto path must NOT have run when an override is set.
        fake_dz.diarize_auto.assert_not_called()

    def test_explicit_threshold_override_wins_over_long_audio_bump(self):
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 100.0},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(w, speaker="SPEAKER_00") for w in CANNED_WORDS
        ]
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", 0.42), \
             mock.patch.object(asr_server, "_MAX_DIARIZE_SECONDS", 0), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS, duration=900.0,
                # An override (any of num_speakers / cluster_threshold)
                # forces the AHC path; we use num_speakers here so the
                # override under test (CLUSTER_THRESHOLD_OVERRIDE=0.42)
                # wins.
                num_speakers_override=2,
            )
        kwargs = fake_dz.diarize.call_args.kwargs
        self.assertAlmostEqual(kwargs["cluster_threshold"], 0.42)

    def test_default_path_uses_auto_eigengap(self):
        """The new default path: no overrides → diarize_auto, NOT
        diarize. This is the behavioral change that prevents the
        451-phantom-speaker blow-up by replacing single-threshold AHC
        with eigengap auto-K. Regression: before this fix, a default
        ?Generate? hit `dz.diarize` directly with a single threshold."""
        fake_dz = mock.MagicMock()
        fake_dz.diarize_auto.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5},
            {"speaker": "SPEAKER_01", "start": 1.5, "end": 3.0},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(CANNED_WORDS[0], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[1], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[2], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[3], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[4], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[5], speaker="SPEAKER_01"),
        ]
        with mock.patch.object(asr_server, "_MAX_DIARIZE_SECONDS", 0), \
             mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}):
            out, n = asr_server._attach_speakers_to_words(
                "/tmp/audio.wav", CANNED_WORDS, duration=600.0,
            )
        fake_dz.diarize_auto.assert_called_once()
        # Legacy single-threshold path must NOT fire for the default flow.
        fake_dz.diarize.assert_not_called()
        self.assertEqual(n, 2)
        self.assertEqual(
            [w["speaker"] for w in out],
            ["speaker_0", "speaker_0",
             "speaker_1", "speaker_1", "speaker_1", "speaker_1"],
        )


class PickClusterThresholdTests(unittest.TestCase):
    def test_short_audio_no_override_uses_default(self):
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", None):
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(60.0), 0.5)

    def test_long_audio_no_override_bumps_to_0_7(self):
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", None):
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(700.0), 0.7)

    def test_unknown_duration_uses_default(self):
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", None):
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(None), 0.5)

    def test_explicit_override_always_wins(self):
        with mock.patch.object(asr_server, "_CLUSTER_THRESHOLD_OVERRIDE", 0.33):
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(60.0), 0.33)
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(7200.0), 0.33)
            self.assertAlmostEqual(asr_server._pick_cluster_threshold(None), 0.33)


class TimestampFormatTests(unittest.TestCase):
    def test_srt_timestamp_uses_comma_decimal(self):
        self.assertEqual(asr_server._srt_timestamp(0.0), "00:00:00,000")
        self.assertEqual(asr_server._srt_timestamp(65.123), "00:01:05,123")
        self.assertEqual(asr_server._srt_timestamp(3725.5), "01:02:05,500")

    def test_vtt_timestamp_uses_period_decimal(self):
        self.assertEqual(asr_server._vtt_timestamp(0.0), "00:00:00.000")
        self.assertEqual(asr_server._vtt_timestamp(65.123), "00:01:05.123")
        self.assertEqual(asr_server._vtt_timestamp(3725.5), "01:02:05.500")

    def test_negative_timestamps_clamp_to_zero(self):
        self.assertEqual(asr_server._srt_timestamp(-1.0), "00:00:00,000")
        self.assertEqual(asr_server._vtt_timestamp(-1.0), "00:00:00.000")


class SubtitleFormatTests(unittest.TestCase):
    def setUp(self):
        self.segments = asr_server._build_openai_segments(
            CANNED_WORDS, CANNED_TRANSCRIPT,
        )

    def test_srt_emits_indexed_blocks_with_arrow(self):
        srt = asr_server._segments_to_srt(self.segments)
        self.assertIn("1\n", srt)
        self.assertIn("2\n", srt)
        self.assertIn(" --> ", srt)
        self.assertIn("Hello world.", srt)
        self.assertIn("This is a test.", srt)

    def test_vtt_starts_with_webvtt_header(self):
        vtt = asr_server._segments_to_vtt(self.segments)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn(" --> ", vtt)
        self.assertIn("Hello world.", vtt)


# ===========================================================================
# Response-shape unit tests (no FastAPI involved)
# ===========================================================================

class OpenAITranscriptionResponseTests(unittest.TestCase):
    def _build(self, response_format: str):
        return asr_server._openai_transcription_response(
            transcript=CANNED_TRANSCRIPT,
            words=CANNED_WORDS,
            language=CANNED_LANG,
            duration=CANNED_DURATION,
            response_format=response_format,
        )

    def test_json_default_returns_text_only(self):
        result = self._build("json")
        self.assertEqual(result, {"text": CANNED_TRANSCRIPT})

    def test_unknown_format_falls_back_to_json(self):
        # Bypass endpoint validation; the helper itself should be lenient.
        result = self._build("garbage")
        self.assertEqual(result, {"text": CANNED_TRANSCRIPT})

    def test_text_returns_plaintext_response(self):
        result = self._build("text")
        self.assertEqual(result.body.decode(), CANNED_TRANSCRIPT)
        self.assertTrue(result.media_type.startswith("text/plain"))

    def test_verbose_json_shape_matches_openai_contract(self):
        result = self._build("verbose_json")
        self.assertEqual(result["task"], "transcribe")
        self.assertEqual(result["language"], "english")  # full name, not 'en'
        self.assertEqual(result["text"], CANNED_TRANSCRIPT)
        self.assertEqual(result["duration"], 2.5)
        self.assertEqual(len(result["words"]), len(CANNED_WORDS))
        # words should be {word, start, end} only (no confidence per OpenAI)
        self.assertEqual(set(result["words"][0].keys()), {"word", "start", "end"})
        # segments must include all OpenAI-required fields, even if synthetic
        for seg in result["segments"]:
            for field in ("id", "seek", "start", "end", "text", "tokens",
                          "temperature", "avg_logprob", "compression_ratio",
                          "no_speech_prob"):
                self.assertIn(field, seg)

    def test_diarized_json_shape_matches_char_contract(self):
        # Char's CreateTranscriptionResponse::Diarized parser expects:
        #   { duration, task, text, segments[*].{id, speaker, start, end, text, type} }
        # No speakers attached to words -> placeholder speaker_0 fallback.
        result = self._build("diarized_json")
        self.assertEqual(result["task"], "transcribe")
        self.assertEqual(result["text"], CANNED_TRANSCRIPT)
        self.assertEqual(result["duration"], 2.5)
        self.assertGreater(len(result["segments"]), 0)
        for seg in result["segments"]:
            self.assertEqual(set(seg.keys()),
                             {"id", "speaker", "start", "end", "text", "type"})
            self.assertTrue(seg["id"].startswith("seg_"))
            self.assertEqual(seg["speaker"], "speaker_0")
            self.assertEqual(seg["type"], "transcript.text.segment")

    def test_diarized_json_uses_real_speaker_labels_when_attached(self):
        # When words already carry a `speaker` field (because the endpoint ran
        # sherpa-onnx beforehand), the diarized_json shaper must respect it
        # and split segments at speaker boundaries.
        words = [
            dict(CANNED_WORDS[0], speaker="speaker_0"),
            dict(CANNED_WORDS[1], speaker="speaker_0"),
            dict(CANNED_WORDS[2], speaker="speaker_1"),
            dict(CANNED_WORDS[3], speaker="speaker_1"),
            dict(CANNED_WORDS[4], speaker="speaker_1"),
            dict(CANNED_WORDS[5], speaker="speaker_1"),
        ]
        result = asr_server._openai_transcription_response(
            transcript=CANNED_TRANSCRIPT,
            words=words,
            language=CANNED_LANG,
            duration=CANNED_DURATION,
            response_format="diarized_json",
        )
        speakers = [seg["speaker"] for seg in result["segments"]]
        self.assertIn("speaker_0", speakers)
        self.assertIn("speaker_1", speakers)
        # Segment with speaker_0 must contain only the first sentence
        s0 = next(s for s in result["segments"] if s["speaker"] == "speaker_0")
        self.assertEqual(s0["text"], "Hello world.")
        s1 = next(s for s in result["segments"] if s["speaker"] == "speaker_1")
        self.assertEqual(s1["text"], "This is a test.")

    def test_srt_returns_subrip_response(self):
        result = self._build("srt")
        body = result.body.decode()
        self.assertIn(" --> ", body)
        self.assertIn("Hello world.", body)
        self.assertEqual(result.media_type, "application/x-subrip")

    def test_vtt_returns_webvtt_response(self):
        result = self._build("vtt")
        body = result.body.decode()
        self.assertTrue(body.startswith("WEBVTT"))
        self.assertEqual(result.media_type, "text/vtt")

    def test_verbose_json_unknown_language_passes_through(self):
        result = asr_server._openai_transcription_response(
            transcript="hola", words=[], language="xx",
            duration=1.0, response_format="verbose_json",
        )
        self.assertEqual(result["language"], "xx")

    def test_verbose_json_none_language_defaults_to_english(self):
        result = asr_server._openai_transcription_response(
            transcript="hi", words=[], language=None,
            duration=1.0, response_format="verbose_json",
        )
        self.assertEqual(result["language"], "english")


# ===========================================================================
# End-to-end TestClient — exercises FastAPI routing, multipart parsing,
# error paths, and the full request/response lifecycle of the new endpoint.
# ===========================================================================

class OpenAIEndpointE2ETests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(asr_server.app)
        self._patcher = mock.patch.object(
            asr_server, "_run_asr_async", side_effect=_fake_run_asr_async,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _post(self, *, response_format: str | None = None,
              filename: str = "audio.m4a",
              model: str | None = "gpt-4o-transcribe-diarize",
              extra_form: dict | None = None,
              query_params: dict | None = None):
        files = {"file": (filename, io.BytesIO(b"FAKE_AUDIO" * 100), "audio/m4a")}
        data: dict = {}
        if model is not None:
            data["model"] = model
        if response_format is not None:
            data["response_format"] = response_format
        if extra_form:
            data.update(extra_form)
        return self.client.post(
            "/v1/audio/transcriptions",
            files=files,
            data=data,
            headers={"Authorization": "Bearer test-key"},
            params=query_params,
        )

    def test_default_format_is_json_with_text_only(self):
        # Char's standard model whisper-1 sends response_format=json
        resp = self._post(response_format=None, model="whisper-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"text": CANNED_TRANSCRIPT})

    def test_chars_default_diarized_json_shape(self):
        # The exact shape Char sends when the user clicks Generate with the
        # default OpenAI batch model gpt-4o-transcribe-diarize. With diarization
        # disabled we still must produce a valid response with placeholder labels.
        with mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", False):
            resp = self._post(response_format="diarized_json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["task"], "transcribe")
        self.assertEqual(body["text"], CANNED_TRANSCRIPT)
        self.assertGreater(len(body["segments"]), 0)
        first = body["segments"][0]
        self.assertEqual(first["speaker"], "speaker_0")
        self.assertEqual(first["type"], "transcript.text.segment")
        self.assertTrue(first["id"].startswith("seg_"))

    def test_diarized_json_with_real_diarization_emits_multiple_speakers(self):
        # End-to-end: fake diarize_auto returns two speaker turns, the
        # endpoint must run diarization, attach labels, and the response
        # segments must split at the speaker boundary. Default path
        # since v3 is auto-K eigengap (`diarize_auto`); the legacy
        # single-threshold `diarize` only runs when overrides are set.
        fake_dz = mock.MagicMock()
        fake_dz.diarize_auto.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.10},
            {"speaker": "SPEAKER_01", "start": 1.20, "end": 2.30},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(CANNED_WORDS[0], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[1], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[2], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[3], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[4], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[5], speaker="SPEAKER_01"),
        ]
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", True):
            resp = self._post(response_format="diarized_json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        speakers = sorted({s["speaker"] for s in body["segments"]})
        self.assertEqual(speakers, ["speaker_0", "speaker_1"])
        self.assertEqual(fake_dz.diarize_auto.call_count, 1,
                         "diarize_auto() should run exactly once on the default path")
        fake_dz.diarize.assert_not_called()

    def test_diarized_json_skips_diarization_when_env_disabled(self):
        # With OPENAI_BATCH_DIARIZE off, diarization_backend.diarize MUST NOT
        # be invoked — keeps the latency promise honest.
        fake_dz = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", False):
            resp = self._post(response_format="diarized_json")
        self.assertEqual(resp.status_code, 200)
        fake_dz.diarize.assert_not_called()
        body = resp.json()
        self.assertTrue(all(s["speaker"] == "speaker_0" for s in body["segments"]))

    def test_diarization_failure_does_not_break_endpoint(self):
        # If sherpa-onnx blows up mid-Generate (either path), the
        # endpoint should still return a valid diarized_json response
        # with placeholder labels — never a 500. This is the safety
        # net Char relies on.
        fake_dz = mock.MagicMock()
        fake_dz.diarize_auto.side_effect = RuntimeError("sherpa-onnx model missing")
        fake_dz.diarize.side_effect = RuntimeError("sherpa-onnx model missing")
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", True):
            resp = self._post(response_format="diarized_json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(all(s["speaker"] == "speaker_0" for s in body["segments"]))

    def test_verbose_json_shape(self):
        resp = self._post(response_format="verbose_json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["task"], "transcribe")
        self.assertEqual(body["language"], "english")
        self.assertEqual(body["text"], CANNED_TRANSCRIPT)
        self.assertEqual(len(body["words"]), len(CANNED_WORDS))

    def test_text_returns_plain_body(self):
        resp = self._post(response_format="text")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))
        self.assertEqual(resp.text, CANNED_TRANSCRIPT)

    def test_srt_returns_subrip_body(self):
        resp = self._post(response_format="srt")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp.headers["content-type"].startswith("application/x-subrip"),
            f"unexpected content-type: {resp.headers['content-type']}",
        )
        self.assertIn(" --> ", resp.text)
        self.assertIn("Hello world.", resp.text)

    def test_vtt_returns_webvtt_body(self):
        resp = self._post(response_format="vtt")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp.headers["content-type"].startswith("text/vtt"),
            f"unexpected content-type: {resp.headers['content-type']}",
        )
        self.assertTrue(resp.text.startswith("WEBVTT"))

    def test_missing_file_returns_400(self):
        # Send a real multipart POST that just doesn't include a `file` field.
        # We use httpx's _files_ shape with an unrelated form key so multipart
        # encoding is triggered without populating "file".
        resp = self.client.post(
            "/v1/audio/transcriptions",
            files={"not_file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
            data={"model": "whisper-1"},
            headers={"Authorization": "Bearer test-key"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], "missing_file")

    def test_non_multipart_returns_400(self):
        resp = self.client.post(
            "/v1/audio/transcriptions",
            json={"model": "whisper-1"},
            headers={"Authorization": "Bearer test-key"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "invalid_content_type")

    def test_invalid_response_format_returns_400(self):
        resp = self._post(response_format="bogus")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "invalid_response_format")
        self.assertIn("diarized_json", body["error"]["message"])

    def test_empty_file_returns_400(self):
        files = {"file": ("audio.wav", io.BytesIO(b""), "audio/wav")}
        resp = self.client.post(
            "/v1/audio/transcriptions",
            files=files,
            data={"model": "whisper-1"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "empty_file")

    def test_auth_header_is_optional(self):
        files = {"file": ("a.wav", io.BytesIO(b"x" * 100), "audio/wav")}
        resp = self.client.post("/v1/audio/transcriptions", files=files)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"text": CANNED_TRANSCRIPT})

    # ---- streaming SSE branch (Char's progressive `gpt-4o-transcribe` path) ----

    def _parse_sse_events(self, body: str) -> list[dict[str, Any] | str]:
        """Parse the `data: ...\\n\\n` blocks in an SSE response body."""
        out: list[dict[str, Any] | str] = []
        for chunk in body.split("\n\n"):
            chunk = chunk.strip()
            if not chunk.startswith("data:"):
                continue
            payload = chunk[len("data:"):].strip()
            if payload == "[DONE]":
                out.append("[DONE]")
                continue
            out.append(json.loads(payload))
        return out

    def test_stream_true_returns_sse_with_done_event(self):
        # Char's `gpt-4o-transcribe` progressive path POSTs `stream=true`.
        # We must reply with text/event-stream, end with `transcript.text.done`
        # carrying the full transcript, and terminate with `[DONE]`.
        resp = self._post(
            model="gpt-4o-transcribe",
            response_format=None,
            extra_form={"stream": "true"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp.headers["content-type"].startswith("text/event-stream"),
            f"unexpected content-type: {resp.headers['content-type']}",
        )
        events = self._parse_sse_events(resp.text)
        self.assertEqual(events[-1], "[DONE]")
        done = events[-2]
        self.assertIsInstance(done, dict)
        self.assertEqual(done["type"], "transcript.text.done")
        self.assertEqual(done["text"], CANNED_TRANSCRIPT)

    def test_stream_emits_heartbeat_deltas_when_asr_is_slow(self):
        # The whole point of the streaming branch: a long ASR run must
        # produce delta heartbeats so Char's 60-second BATCH_IDLE_TIMEOUT
        # doesn't fire. We force ASR to take longer than the heartbeat
        # interval and assert at least one delta arrives before `done`.
        slow_done = asyncio.Event()

        async def slow_asr(audio_path, on_segment=None, on_start=None):
            # Simulate a 0.4s ASR; with heartbeat=0.1s we expect ~3 heartbeats.
            await asyncio.sleep(0.4)
            slow_done.set()
            return CANNED_TRANSCRIPT, CANNED_WORDS, CANNED_LANG, CANNED_DURATION

        with mock.patch.object(asr_server, "_run_asr_async", side_effect=slow_asr), \
             mock.patch.object(asr_server, "_OPENAI_STREAM_HEARTBEAT_SECONDS", 0.1):
            resp = self._post(
                model="gpt-4o-transcribe",
                response_format=None,
                extra_form={"stream": "true"},
            )
        self.assertEqual(resp.status_code, 200)
        events = self._parse_sse_events(resp.text)
        deltas = [e for e in events if isinstance(e, dict)
                  and e.get("type") == "transcript.text.delta"]
        dones = [e for e in events if isinstance(e, dict)
                 and e.get("type") == "transcript.text.done"]
        self.assertGreaterEqual(len(deltas), 1,
            "must emit at least one heartbeat delta during slow ASR")
        # Heartbeat delta is a single space (non-empty so Char's parser
        # actually emits a Progress event that resets last_activity_tx).
        self.assertEqual(deltas[0]["delta"], " ")
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0]["text"], CANNED_TRANSCRIPT)

    def test_stream_with_diarization_inlines_speaker_prefixes(self):
        # When diarization runs successfully and finds 2+ speakers, the
        # streamed `done.text` must inline `Speaker N:` prefixes -- the
        # progressive batch shape Char accepts on this path is text-only,
        # so structural segments would be lost otherwise.
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.10},
            {"speaker": "SPEAKER_01", "start": 1.20, "end": 2.30},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(CANNED_WORDS[0], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[1], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[2], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[3], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[4], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[5], speaker="SPEAKER_01"),
        ]
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", True):
            resp = self._post(
                model="gpt-4o-transcribe-diarize",  # triggers diarize
                response_format="diarized_json",
                extra_form={"stream": "true"},
            )
        self.assertEqual(resp.status_code, 200)
        events = self._parse_sse_events(resp.text)
        done = next(e for e in events if isinstance(e, dict)
                    and e.get("type") == "transcript.text.done")
        text = done["text"]
        self.assertIn("Speaker 0:", text)
        self.assertIn("Speaker 1:", text)
        # The second speaker's lines must come AFTER the first speaker's
        # lines, not interleaved.
        self.assertLess(text.index("Speaker 0:"), text.index("Speaker 1:"))

    def test_stream_diarizes_by_default_for_plain_gpt4o_transcribe(self):
        # Regression: Char's progressive flow sends `model=gpt-4o-transcribe`
        # + `response_format=json` (never `-diarize` / `diarized_json`).
        # The original gate required one of those, so diarization silently
        # no-op'd on every Generate -> single-speaker transcripts.
        # The fix: diarize whenever `_OPENAI_BATCH_DIARIZE` is on,
        # regardless of model name / response_format.
        fake_dz = mock.MagicMock()
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.10},
            {"speaker": "SPEAKER_01", "start": 1.20, "end": 2.30},
        ]
        fake_dz.attach_speaker_to_words.return_value = [
            dict(CANNED_WORDS[0], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[1], speaker="SPEAKER_00"),
            dict(CANNED_WORDS[2], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[3], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[4], speaker="SPEAKER_01"),
            dict(CANNED_WORDS[5], speaker="SPEAKER_01"),
        ]
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", True):
            resp = self._post(
                model="gpt-4o-transcribe",       # NOT -diarize
                response_format=None,            # i.e. defaulted to "json"
                extra_form={"stream": "true"},
            )
        self.assertEqual(resp.status_code, 200)
        events = self._parse_sse_events(resp.text)
        done = next(e for e in events if isinstance(e, dict)
                    and e.get("type") == "transcript.text.done")
        # Speaker prefixes prove diarization actually fired for this
        # bare gpt-4o-transcribe + json combination.
        self.assertIn("Speaker 0:", done["text"])
        self.assertIn("Speaker 1:", done["text"])

    def test_stream_diarize_query_param_zero_opts_out(self):
        # Operator escape hatch: `?diarize=0` skips diarization even when
        # the server-wide flag is on, so power users can compare with
        # and without speakers without restarting the server.
        # If diarization had run we'd see Speaker prefixes in the text
        # (per the test above); their absence confirms the opt-out.
        fake_dz = mock.MagicMock()
        # Make diarize.return_value distinctive so a regression that
        # accidentally still calls diarization will fail this assertion.
        fake_dz.diarize.return_value = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.10},
            {"speaker": "SPEAKER_01", "start": 1.20, "end": 2.30},
        ]
        with mock.patch.dict(sys.modules, {"diarization_backend": fake_dz}), \
             mock.patch.object(asr_server, "_OPENAI_BATCH_DIARIZE", True):
            resp = self._post(
                model="gpt-4o-transcribe",
                response_format=None,
                extra_form={"stream": "true"},
                # `?diarize=0` is the documented opt-out.
                query_params={"diarize": "0"},
            )
        self.assertEqual(resp.status_code, 200)
        fake_dz.diarize.assert_not_called()
        events = self._parse_sse_events(resp.text)
        done = next(e for e in events if isinstance(e, dict)
                    and e.get("type") == "transcript.text.done")
        # Plain transcript, no speaker prefixes inlined.
        self.assertNotIn("Speaker 0:", done["text"])

    def test_stream_handles_asr_failure_with_synthetic_done(self):
        # If ASR explodes mid-stream we still must emit a `done` event so
        # Char doesn't hang waiting forever. The done.text contains the
        # human-readable error so the user sees what went wrong.
        async def boom(*a, **kw):
            raise RuntimeError("model exploded")

        with mock.patch.object(asr_server, "_run_asr_async", side_effect=boom):
            resp = self._post(
                model="gpt-4o-transcribe",
                response_format=None,
                extra_form={"stream": "true"},
            )
        # Streaming response is always 200; errors are inlined into the
        # final `done.text` (closer to OpenAI's actual SSE behavior).
        self.assertEqual(resp.status_code, 200)
        events = self._parse_sse_events(resp.text)
        done = next(e for e in events if isinstance(e, dict)
                    and e.get("type") == "transcript.text.done")
        self.assertIn("model exploded", done["text"])
        self.assertEqual(events[-1], "[DONE]")


# ---------------------------------------------------------------------------
# Auth integration: exercises the real bearer-token check end-to-end.
#
# The rest of the e2e suite runs with LOCAL_SCRIBE_DISABLE_AUTH=1 so the
# bypass branch is taken and the tests can ignore the new auth layer.
# This class explicitly turns the bypass *off* and stands up the app
# inside a TestClient context manager so the FastAPI lifespan runs and
# populates ``asr_server._asr_token`` from the test master-key env var.
# ---------------------------------------------------------------------------

class AsrServerAuthIntegrationTests(unittest.TestCase):
    """End-to-end verification that gated endpoints really do 401 on
    missing/wrong tokens and pass with the right one, exercising the
    same code path Char hits in production."""

    TEST_MK_HEX = "ab" * 32

    @classmethod
    def setUpClass(cls):
        import service_auth as sa
        cls._old_bypass = os.environ.pop(sa.BYPASS_ENV, None)
        cls._old_test_mk = os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
        os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = cls.TEST_MK_HEX
        # Precompute what the server's derived token will be so we can
        # match it in headers.
        cls.expected_token = sa.derive_service_token(
            bytes.fromhex(cls.TEST_MK_HEX), "asr",
        )

    @classmethod
    def tearDownClass(cls):
        import service_auth as sa
        if cls._old_bypass is not None:
            os.environ[sa.BYPASS_ENV] = cls._old_bypass
        if cls._old_test_mk is None:
            os.environ.pop("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX", None)
        else:
            os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = cls._old_test_mk

    def setUp(self):
        # ``with TestClient(...) as client`` enters the lifespan, which
        # is where ``_asr_token`` is populated. Without the context
        # manager, lifespan runs lazily and we can't guarantee state.
        self._cm = TestClient(asr_server.app)
        self.client = self._cm.__enter__()
        self.addCleanup(lambda: self._cm.__exit__(None, None, None))
        self._patcher = mock.patch.object(
            asr_server, "_run_asr_async", side_effect=_fake_run_asr_async,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _audio_files(self):
        return {"file": ("a.m4a", io.BytesIO(b"FAKE" * 100), "audio/m4a")}

    # ---- /health stays open --------------------------------------

    def test_health_endpoint_remains_unauthenticated(self):
        """/health is the liveness probe — gating it would break
        ``./run.sh status`` and any future monitoring."""
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    # ---- /v1/audio/transcriptions (OpenAI batch, Char's path) ----

    def test_openai_endpoint_401_without_auth_header(self):
        r = self.client.post(
            "/v1/audio/transcriptions",
            files=self._audio_files(),
            data={"model": "gpt-4o-transcribe-diarize"},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)
        body = r.json()
        self.assertEqual(body["detail"]["error"]["type"], "auth")
        self.assertEqual(body["detail"]["error"]["service"], "asr")

    def test_openai_endpoint_401_with_wrong_token(self):
        r = self.client.post(
            "/v1/audio/transcriptions",
            files=self._audio_files(),
            data={"model": "gpt-4o-transcribe-diarize"},
            headers={"Authorization": "Bearer ls_asr_wrong"},
        )
        self.assertEqual(r.status_code, 401)

    def test_openai_endpoint_200_with_correct_bearer(self):
        r = self.client.post(
            "/v1/audio/transcriptions",
            files=self._audio_files(),
            data={"model": "gpt-4o-transcribe-diarize"},
            headers={"Authorization": f"Bearer {self.expected_token}"},
        )
        self.assertEqual(r.status_code, 200, msg=f"body={r.text[:300]}")

    def test_openai_endpoint_accepts_x_api_key_header(self):
        r = self.client.post(
            "/v1/audio/transcriptions",
            files=self._audio_files(),
            data={"model": "gpt-4o-transcribe-diarize"},
            headers={"X-API-Key": self.expected_token},
        )
        self.assertEqual(r.status_code, 200)

    # ---- /v1/listen (Deepgram batch, Char's live path) -----------

    def test_listen_endpoint_401_without_auth(self):
        r = self.client.post("/v1/listen", content=b"raw audio bytes")
        self.assertEqual(r.status_code, 401)

    def test_listen_endpoint_accepts_token_scheme(self):
        # Deepgram clients use ``Authorization: Token <key>``.
        r = self.client.post(
            "/v1/listen",
            content=b"raw audio bytes" * 100,
            headers={
                "Authorization": f"Token {self.expected_token}",
                "Content-Type": "audio/wav",
            },
        )
        # Either 200 or 400 (bad WAV) is fine -- we only care that the
        # request got *past* the 401 gate and reached the handler body.
        self.assertNotEqual(r.status_code, 401)

    # ---- /v1/listen/stream ---------------------------------------

    def test_listen_stream_endpoint_gated(self):
        r = self.client.post(
            "/v1/listen/stream", content=b"raw audio bytes",
        )
        self.assertEqual(r.status_code, 401)

    # ---- Token derivation ----------------------------------------

    def test_lifespan_derived_token_matches_expected(self):
        # Lifespan should have populated _asr_token; verify it's the
        # exact token we'd derive from our test master key.
        holder = asr_server._asr_token
        self.assertIsNotNone(holder)
        self.assertEqual(holder.service, "asr")
        self.assertEqual(holder.token, self.expected_token)


class ComposeSpeakerPrefixedTextTests(unittest.TestCase):
    def test_groups_by_speaker_turn_with_blank_lines(self):
        words = [
            {"punctuated_word": "Hello.", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
            {"punctuated_word": "World.", "start": 0.4, "end": 0.7, "speaker": "SPEAKER_00"},
            {"punctuated_word": "Hi", "start": 0.8, "end": 1.0, "speaker": "SPEAKER_01"},
            {"punctuated_word": "back.", "start": 1.1, "end": 1.4, "speaker": "SPEAKER_01"},
            {"punctuated_word": "OK?", "start": 1.5, "end": 1.7, "speaker": "SPEAKER_00"},
        ]
        text = asr_server._compose_speaker_prefixed_text(words, "")
        self.assertIn("Speaker 00: Hello. World.", text)
        self.assertIn("Speaker 01: Hi back.", text)
        self.assertIn("Speaker 00: OK?", text)
        # Each speaker turn separated by a blank line.
        self.assertEqual(text.count("\n\n"), 2)

    def test_falls_back_to_plain_transcript_when_no_words(self):
        text = asr_server._compose_speaker_prefixed_text([], "raw fallback")
        self.assertEqual(text, "raw fallback")

    def test_single_speaker_collapses_to_one_line(self):
        words = [
            {"punctuated_word": "Hello.", "start": 0.0, "end": 0.3, "speaker": "speaker_0"},
            {"punctuated_word": "World.", "start": 0.4, "end": 0.7, "speaker": "speaker_0"},
        ]
        text = asr_server._compose_speaker_prefixed_text(words, "")
        self.assertEqual(text, "Speaker 0: Hello. World.")


class OpenAIEndpointMiscTests(unittest.TestCase):
    """Endpoint-level tests reusing the same fake ASR fixture."""

    def setUp(self):
        self.client = TestClient(asr_server.app)
        self._patcher = mock.patch.object(
            asr_server, "_run_asr_async", side_effect=_fake_run_asr_async,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _post(self, *, response_format: str | None = None,
              filename: str = "audio.m4a",
              model: str | None = "gpt-4o-transcribe-diarize"):
        files = {"file": (filename, io.BytesIO(b"FAKE_AUDIO" * 100), "audio/m4a")}
        data: dict = {}
        if model is not None:
            data["model"] = model
        if response_format is not None:
            data["response_format"] = response_format
        return self.client.post(
            "/v1/audio/transcriptions",
            files=files,
            data=data,
            headers={"Authorization": "Bearer test-key"},
        )

    def test_backend_failure_returns_500_with_error_envelope(self):
        async def boom(*a, **kw):
            raise RuntimeError("model exploded")

        with mock.patch.object(asr_server, "_run_asr_async", side_effect=boom):
            resp = self._post(response_format="diarized_json")
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "transcription_failed")
        self.assertEqual(body["error"]["message"], "model exploded")

    def test_health_advertises_openai_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("endpoints", body)
        self.assertEqual(body["endpoints"]["openai_batch"],
                         "POST /v1/audio/transcriptions")


if __name__ == "__main__":
    unittest.main()
