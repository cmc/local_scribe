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

import io
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
              extra_form: dict | None = None):
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
        )

    def test_default_format_is_json_with_text_only(self):
        # Char's standard model whisper-1 sends response_format=json
        resp = self._post(response_format=None, model="whisper-1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"text": CANNED_TRANSCRIPT})

    def test_chars_default_diarized_json_shape(self):
        # The exact shape Char sends when the user clicks Generate with the
        # default OpenAI batch model gpt-4o-transcribe-diarize.
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
