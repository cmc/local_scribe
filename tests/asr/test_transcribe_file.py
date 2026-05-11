"""Unit tests for transcribe_file.py.

Run from the repo root:
    venv/bin/python -m unittest discover -v -s tests

These tests cover the pure-Python logic and use unittest.mock to stub out
the Whisper server and LM Studio HTTP calls. No real audio file or network
traffic is needed.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from local_scribe.asr import transcribe_file as tf


FAKE_AUDIO = b"FAKE_AUDIO_BYTES_FOR_TESTS_" + b"x" * 500


SAMPLE_PAYLOAD: dict = {
    "metadata": {
        "transaction_key": "deprecated",
        "request_id": "test-req-1",
        "sha256": "deadbeef",
        "created": "2026-05-08T16:00:00Z",
        "duration": 125.5,
        "channels": 1,
        "models": ["small"],
    },
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": "  Hello world. This is a test transcript.  ",
                        "confidence": 0.95,
                        "words": [],
                        "languages": ["en"],
                    }
                ],
                "detected_language": "en",
            }
        ]
    },
}


SAMPLE_LLM_RESPONSE = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": (
                    "<think>Let me analyze this transcript...</think>\n\n"
                    "# Test call summary\n\n## TL;DR\nIt was a test call."
                ),
            }
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 30},
}


def make_sse_response(content: str, completion_tokens: int = 30) -> list[bytes]:
    """Build a list of SSE-formatted lines that mimic LM Studio's streaming
    `text/event-stream` response for a chat completion."""
    lines: list[bytes] = []
    for piece in content.split(" "):
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece + " "},
                    "finish_reason": None,
                }
            ],
        }
        lines.append(f"data: {json.dumps(chunk)}".encode())
        lines.append(b"")
    final = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": completion_tokens,
            "total_tokens": 50 + completion_tokens,
        },
    }
    lines.append(f"data: {json.dumps(final)}".encode())
    lines.append(b"")
    lines.append(b"data: [DONE]")
    return lines


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None,
                 text_body: str = "", lines: list[bytes] | None = None):
        self.status_code = status_code
        self.reason = "OK" if status_code < 400 else "Bad Request"
        self.ok = 200 <= status_code < 400
        self._json = json_body
        self.text = text_body or (json.dumps(json_body) if json_body else "")
        self._lines = lines or []

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"{self.status_code} {self.reason}", response=self)

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            yield line.decode("utf-8") if decode_unicode else line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPureHelpers(unittest.TestCase):
    def test_format_duration_seconds_only(self):
        self.assertEqual(tf._format_duration(0), "0s")
        self.assertEqual(tf._format_duration(45), "45s")
        self.assertEqual(tf._format_duration(59.4), "59s")

    def test_format_duration_minutes(self):
        self.assertEqual(tf._format_duration(60), "1m 00s")
        self.assertEqual(tf._format_duration(125), "2m 05s")

    def test_format_duration_hours(self):
        self.assertEqual(tf._format_duration(3600), "1h 00m 00s")
        self.assertEqual(tf._format_duration(3725), "1h 02m 05s")

    def test_format_duration_negative_clamped(self):
        self.assertEqual(tf._format_duration(-5), "0s")

    def test_strip_think_tags_basic(self):
        self.assertEqual(tf._strip_think_tags("<think>noise</think>real"), "real")

    def test_strip_think_tags_multiline(self):
        out = tf._strip_think_tags("<think>line1\nline2\nline3</think>\n\n# title")
        self.assertEqual(out, "# title")

    def test_strip_think_tags_no_tags(self):
        self.assertEqual(tf._strip_think_tags("just text"), "just text")

    def test_approx_token_count(self):
        self.assertEqual(tf._approx_token_count("a" * 400), 100)
        self.assertEqual(tf._approx_token_count(""), 1)

    def test_render_bar_endpoints(self):
        self.assertEqual(tf._render_bar(0.0, width=4), "[----]")
        self.assertEqual(tf._render_bar(1.0, width=4), "[####]")

    def test_render_bar_clamps(self):
        self.assertEqual(tf._render_bar(-0.5, width=4), "[----]")
        self.assertEqual(tf._render_bar(2.0, width=4), "[####]")

    def test_render_bar_partial(self):
        self.assertEqual(tf._render_bar(0.5, width=4), "[##--]")

    def test_stream_url_for_clean(self):
        self.assertEqual(
            tf._stream_url_for("http://x:8000/v1/listen"),
            "http://x:8000/v1/listen/stream",
        )

    def test_stream_url_for_trailing_slash(self):
        self.assertEqual(
            tf._stream_url_for("http://x:8000/v1/listen/"),
            "http://x:8000/v1/listen/stream",
        )

    def test_stream_url_for_already_stream(self):
        self.assertEqual(
            tf._stream_url_for("http://x:8000/v1/listen/stream"),
            "http://x:8000/v1/listen/stream",
        )

    def test_extract_transcript(self):
        self.assertEqual(tf.extract_transcript(SAMPLE_PAYLOAD),
                         "Hello world. This is a test transcript.")

    def test_human_size(self):
        self.assertEqual(tf._human_size(0), "0 B")
        self.assertEqual(tf._human_size(512), "512 B")
        self.assertIn("KB", tf._human_size(2048))
        self.assertIn("MB", tf._human_size(5 * 1024 * 1024))
        self.assertIn("GB", tf._human_size(2 * 1024 ** 3))


class TestParseCallTime(unittest.TestCase):
    def test_parse_call_time_none(self):
        self.assertEqual(tf._parse_call_time(None), (None, None))
        self.assertEqual(tf._parse_call_time(""), (None, None))

    def test_parse_call_time_now(self):
        ts, disp = tf._parse_call_time("now")
        self.assertIsNotNone(ts)
        self.assertIsNotNone(disp)
        self.assertLess(abs(ts - time.time()), 5)

    def test_parse_call_time_iso(self):
        ts, disp = tf._parse_call_time("2026-05-08T14:30")
        self.assertIsNotNone(ts)
        self.assertIn("2026-05-08", disp)
        self.assertIn("14:30", disp)

    def test_parse_call_time_unix_epoch(self):
        ts, disp = tf._parse_call_time("1700000000")
        self.assertEqual(ts, 1700000000.0)
        self.assertIsNotNone(disp)

    def test_parse_call_time_invalid_returns_none(self):
        self.assertEqual(tf._parse_call_time("not-a-time"), (None, None))


class TestBuildMetadata(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as t:
            t.write(FAKE_AUDIO)
            self.path = Path(t.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_metadata_fields(self):
        meta = tf.build_metadata(SAMPLE_PAYLOAD, self.path)
        self.assertEqual(meta["source_file"], self.path.name)
        self.assertEqual(meta["duration_seconds"], 125.5)
        self.assertEqual(meta["duration_human"], "2m 06s")
        self.assertEqual(meta["language"], "en")
        self.assertNotEqual(meta["recorded_at"], "unknown")
        self.assertIn("(file mtime - approximate)", meta["call_recorded_at"])
        self.assertIsNotNone(meta["transcribed_at"])
        self.assertIsNotNone(meta["transcribed_at_unix"])
        self.assertEqual(Path(meta["source_path"]).name, self.path.name)

    def test_metadata_missing_language(self):
        payload = {
            "metadata": {"duration": 10.0},
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
        }
        meta = tf.build_metadata(payload, self.path)
        self.assertEqual(meta["language"], "unknown")
        self.assertEqual(meta["duration_human"], "10s")

    def test_metadata_call_time_override(self):
        meta = tf.build_metadata(SAMPLE_PAYLOAD, self.path,
                                 call_time_override="2026-05-08T14:30")
        self.assertIn("2026-05-08", meta["recorded_at"])
        self.assertIn("(user-provided)", meta["call_recorded_at"])

    def test_metadata_call_time_now(self):
        meta = tf.build_metadata(SAMPLE_PAYLOAD, self.path, call_time_override="now")
        self.assertIn("(user-provided)", meta["call_recorded_at"])

    def test_metadata_call_time_invalid_falls_back(self):
        meta = tf.build_metadata(SAMPLE_PAYLOAD, self.path, call_time_override="garbage")
        self.assertEqual(meta["recorded_at"], "unknown")

    def test_metadata_uses_source_block_when_present(self):
        payload = dict(SAMPLE_PAYLOAD)
        payload["_source"] = {
            "file_mtime": 1700000000.0,
            "transcribed_at_unix": 1700000050.0,
        }
        meta = tf.build_metadata(payload, self.path)
        self.assertEqual(meta["transcribed_at_unix"], 1700000050.0)
        self.assertIn("file mtime", meta["call_recorded_at"])


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="whisper_cache_test_")
        self.cache_dir = Path(self._tmpdir)
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as t:
            t.write(FAKE_AUDIO)
            self.audio_path = Path(t.name)

    def tearDown(self):
        import shutil as _shutil
        _shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.audio_path.unlink(missing_ok=True)

    def test_file_sha256_deterministic(self):
        h1 = tf.file_sha256(self.audio_path)
        h2 = tf.file_sha256(self.audio_path)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_file_sha256_content_addressed(self):
        h1 = tf.file_sha256(self.audio_path)
        with self.audio_path.open("ab") as f:
            f.write(b"extra")
        h2 = tf.file_sha256(self.audio_path)
        self.assertNotEqual(h1, h2)

    def test_cache_path_uses_hash(self):
        cache_file = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        digest = tf.file_sha256(self.audio_path)
        self.assertEqual(cache_file.name, f"{digest}.json")
        self.assertEqual(cache_file.parent, self.cache_dir)

    def test_cache_miss_returns_none(self):
        cache_file = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        self.assertIsNone(tf.cache_load(cache_file))

    def test_cache_save_and_load_roundtrip(self):
        cache_file = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        tf.cache_save(cache_file, SAMPLE_PAYLOAD)
        self.assertTrue(cache_file.exists())
        loaded = tf.cache_load(cache_file)
        self.assertEqual(loaded, SAMPLE_PAYLOAD)

    def test_cache_load_handles_corrupt_json(self):
        cache_file = self.cache_dir / "corrupt.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{ not valid json }}}")
        self.assertIsNone(tf.cache_load(cache_file))

    def test_cache_clear_counts_and_removes(self):
        cache_file = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        tf.cache_save(cache_file, SAMPLE_PAYLOAD)
        (self.cache_dir / "another.json").write_text("{}")
        removed = tf.cache_clear(self.cache_dir)
        self.assertEqual(removed, 2)
        self.assertFalse(self.cache_dir.exists())

    def test_cache_clear_on_missing_dir_is_noop(self):
        bogus = self.cache_dir / "does_not_exist"
        self.assertEqual(tf.cache_clear(bogus), 0)

    def test_annotate_payload_with_source(self):
        sha = tf.file_sha256(self.audio_path)
        annotated = tf.annotate_payload_with_source(dict(SAMPLE_PAYLOAD), self.audio_path, sha)
        src = annotated["_source"]
        self.assertEqual(src["sha256"], sha)
        self.assertEqual(src["name"], self.audio_path.name)
        self.assertEqual(Path(src["path"]).name, self.audio_path.name)
        self.assertGreater(src["size_bytes"], 0)
        self.assertIsNotNone(src["file_mtime"])
        self.assertIsNotNone(src["transcribed_at_unix"])
        self.assertEqual(src["whisper_duration_seconds"], 125.5)

    def test_cache_list_returns_entries(self):
        sha = tf.file_sha256(self.audio_path)
        annotated = tf.annotate_payload_with_source(dict(SAMPLE_PAYLOAD), self.audio_path, sha)
        cf = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        tf.cache_save(cf, annotated)

        entries = tf.cache_list(self.cache_dir)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["sha256"], sha)
        self.assertEqual(e["source_name"], self.audio_path.name)
        self.assertEqual(e["language"], "en")
        self.assertGreater(e["transcript_words"], 0)

    def test_cache_list_handles_missing_dir(self):
        self.assertEqual(tf.cache_list(self.cache_dir / "nope"), [])

    def test_cache_list_skips_corrupt_entries(self):
        (self.cache_dir).mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "bad.json").write_text("not json")
        sha = tf.file_sha256(self.audio_path)
        annotated = tf.annotate_payload_with_source(dict(SAMPLE_PAYLOAD), self.audio_path, sha)
        cf = tf.cache_path_for(self.audio_path, cache_dir=self.cache_dir)
        tf.cache_save(cf, annotated)
        entries = tf.cache_list(self.cache_dir)
        self.assertEqual(len(entries), 1)


class TestRunLLMNonStreaming(unittest.TestCase):
    """Non-streaming path - simpler to mock and matches what tests historically covered."""

    def test_run_llm_strips_think_tags(self):
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(200, SAMPLE_LLM_RESPONSE)) as p:
            out = tf.run_llm(
                "transcript text", "http://x/v1/chat/completions", "qwen3-30b-a3b-instruct-2507",
                "system prompt", metadata={"source_file": "t.mp3"},
                max_tokens=512, no_think=False, stream=False,
            )
            self.assertNotIn("<think>", out)
            self.assertIn("# Test call summary", out)
            body = p.call_args.kwargs["json"]
            self.assertEqual(body["model"], "qwen3-30b-a3b-instruct-2507")
            self.assertEqual(body["max_tokens"], 512)
            self.assertEqual(body["stream"], False)
            self.assertEqual(len(body["messages"]), 2)
            self.assertIn("Source file: t.mp3", body["messages"][1]["content"])

    def test_run_llm_no_think_prepends_directive(self):
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(200, SAMPLE_LLM_RESPONSE)) as p:
            tf.run_llm("hello", "http://x", "m", "sys", metadata=None,
                       max_tokens=256, no_think=True, stream=False)
            body = p.call_args.kwargs["json"]
            self.assertTrue(body["messages"][1]["content"].startswith("/no_think"))

    def test_run_llm_surfaces_error_body_on_failure(self):
        err_body = {"error": {"message": "context length exceeded"}}
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(400, err_body)):
            import requests as _r
            with self.assertRaises(_r.HTTPError) as ctx:
                tf.run_llm("hello", "http://x", "m", "sys", stream=False)
            msg = str(ctx.exception)
            self.assertIn("400", msg)
            self.assertIn("context length exceeded", msg)


class TestRunLLMStreaming(unittest.TestCase):
    """Streaming path - LM Studio returns text/event-stream with `data: {...}` lines."""

    def test_run_llm_streaming_assembles_content(self):
        sse_lines = make_sse_response(
            "## TL;DR\nA streamed test summary works.",
            completion_tokens=42,
        )
        post = mock.Mock(return_value=FakeResponse(200, lines=sse_lines))
        with mock.patch("local_scribe.asr.transcribe_file.requests.post", post), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            out = tf.run_llm(
                "transcript", "http://x/v1/chat/completions", "qwen3-30b-a3b-instruct-2507",
                "sys", metadata=None, max_tokens=2048, stream=True, print_stream=False,
            )
        self.assertIn("TL;DR", out)
        self.assertIn("streamed test summary works", out)
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertTrue(post.call_args.kwargs.get("stream"))

    def test_run_llm_streaming_unicode_roundtrip(self):
        """Smart quotes must round-trip cleanly. Regression test for the
        ISO-8859-1 default that mangled UTF-8 multi-byte chars."""
        sse_lines = make_sse_response("Moss\u2019s exit", completion_tokens=4)
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(200, lines=sse_lines)), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            out = tf.run_llm("t", "http://x", "m", "sys",
                             stream=True, print_stream=False)
        self.assertIn("Moss\u2019s exit", out)

    def test_run_llm_streaming_zero_tokens_raises(self):
        """LM Studio sometimes accepts a request, returns 200, then sends an
        empty stream when the prompt overflows context. We must surface that
        loudly rather than silently produce an empty summary."""
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(200, lines=[])):
            with self.assertRaises(RuntimeError) as ctx:
                tf.run_llm("t", "http://x", "qwen3-30b-a3b-instruct-2507", "sys",
                           stream=True, print_stream=False)
            self.assertIn("loaded_context_length", str(ctx.exception))

    def test_run_llm_streaming_strips_think_blocks(self):
        sse_lines = make_sse_response(
            "<think>internal reasoning</think> # Real output",
            completion_tokens=10,
        )
        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        return_value=FakeResponse(200, lines=sse_lines)), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            out = tf.run_llm("t", "http://x", "m", "sys",
                             stream=True, print_stream=False)
        self.assertNotIn("<think>", out)
        self.assertIn("Real output", out)


class TestMainIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="whisper_main_test_")
        self.cache_dir = Path(self._tmp) / "cache"
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as t:
            t.write(FAKE_AUDIO)
            self.audio = Path(t.name)

    def tearDown(self):
        import shutil as _shutil
        _shutil.rmtree(self._tmp, ignore_errors=True)
        self.audio.unlink(missing_ok=True)

    def _run(self, *extra_argv, fake_post=None):
        argv = [
            str(self.audio),
            "--no-llm",
            "--asr-backend", "whisper",
            "--cache-dir", str(self.cache_dir),
            "--server", "http://stub/v1/listen",
            *extra_argv,
        ]
        post = fake_post or mock.Mock(return_value=FakeResponse(200, SAMPLE_PAYLOAD))
        with mock.patch("local_scribe.asr.transcribe_file.requests.post", post), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = tf.main(argv)
        return rc, out.getvalue(), err.getvalue(), post

    def test_first_run_calls_whisper_and_caches(self):
        rc, out, err, post = self._run()
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(post.call_count, 1)
        cache_file = tf.cache_path_for(self.audio, cache_dir=self.cache_dir)
        self.assertTrue(cache_file.exists())
        self.assertIn("Cache MISS", out)
        self.assertIn("Cached transcript", out)

        cached = json.loads(cache_file.read_text())
        self.assertIn("_source", cached)
        self.assertEqual(cached["_source"]["name"], self.audio.name)

    def test_second_run_uses_cache(self):
        self._run()
        rc, out, err, post = self._run()
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(post.call_count, 0)
        self.assertIn("Cache HIT", out)

    def test_no_cache_flag_bypasses_both_directions(self):
        self._run()
        rc, out, err, post = self._run("--no-cache")
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("Cache HIT", out)

    def test_clear_cache_wipes_and_exits(self):
        self._run()
        cache_file = tf.cache_path_for(self.audio, cache_dir=self.cache_dir)
        self.assertTrue(cache_file.exists())

        with mock.patch("local_scribe.asr.transcribe_file.requests.post") as post, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = tf.main(["--clear-cache", "--cache-dir", str(self.cache_dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(post.call_count, 0)
        self.assertFalse(cache_file.exists())
        self.assertIn("Cleared", out.getvalue())

    def test_list_cache_command(self):
        self._run()  # populate one entry
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = tf.main(["--list-cache", "--cache-dir", str(self.cache_dir)])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("cached transcript", text)
        self.assertIn(self.audio.name, text)

    def test_list_cache_empty(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = tf.main(["--list-cache", "--cache-dir", str(self.cache_dir / "void")])
        self.assertEqual(rc, 0)
        self.assertIn("(empty)", out.getvalue())

    def test_save_md_writes_summary_only(self):
        out_md = Path(self._tmp) / "out.md"
        rc, out, err, post = self._run("--save", str(out_md))
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("--save", err)
        self.assertIn("no summary", err)
        self.assertFalse(out_md.exists())

    def test_save_txt_writes_transcript(self):
        out_txt = Path(self._tmp) / "out.txt"
        rc, out, err, post = self._run("--save", str(out_txt))
        self.assertEqual(rc, 0, msg=err)
        self.assertTrue(out_txt.exists())
        self.assertEqual(out_txt.read_text().strip(),
                         "Hello world. This is a test transcript.")

    def test_save_json_writes_full_bundle(self):
        out_json = Path(self._tmp) / "out.json"
        rc, out, err, post = self._run("--save", str(out_json))
        self.assertEqual(rc, 0, msg=err)
        self.assertTrue(out_json.exists())
        bundle = json.loads(out_json.read_text())
        self.assertEqual(bundle["transcript"], "Hello world. This is a test transcript.")
        self.assertEqual(bundle["metadata"]["duration_human"], "2m 06s")
        self.assertIn("_source", bundle["raw"])
        self.assertIsNone(bundle["summary"])

    def test_missing_file_returns_1(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = tf.main([
                "/no/such/file.m4a",
                "--no-llm",
                "--cache-dir", str(self.cache_dir),
            ])
        self.assertEqual(rc, 1)
        self.assertIn("file not found", err.getvalue())

    def test_full_run_with_streaming_llm_uses_cache(self):
        self._run()  # prime cache, --no-llm
        argv = [
            str(self.audio),
            "--asr-backend", "whisper",
            "--cache-dir", str(self.cache_dir),
            "--server", "http://stub/v1/listen",
            "--llm-url", "http://stub/v1/chat/completions",
            "--llm-model", "qwen3-30b-a3b-instruct-2507",
        ]

        sse_lines = make_sse_response(
            "## TL;DR\nThis is a streamed summary.\n\n## Action items\n- Test pass.",
            completion_tokens=20,
        )

        def post_router(url, *args, **kwargs):
            if "chat/completions" in url:
                return FakeResponse(200, lines=sse_lines)
            return FakeResponse(200, SAMPLE_PAYLOAD)

        with mock.patch("local_scribe.asr.transcribe_file.requests.post",
                        side_effect=post_router) as post, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            rc = tf.main(argv)
        self.assertEqual(rc, 0)
        urls = [c.args[0] if c.args else c.kwargs.get("url") for c in post.call_args_list]
        self.assertEqual(len(urls), 1, msg=f"only LLM should hit network, got {urls}")
        self.assertIn("chat/completions", urls[0])
        full_out = out.getvalue()
        self.assertIn("Cache HIT", full_out)
        self.assertIn("CALL SUMMARY", full_out)
        self.assertIn("LLM done", full_out)
        self.assertIn("This is a streamed summary", full_out)

    def test_call_time_override_propagates_to_metadata(self):
        out_json = Path(self._tmp) / "out.json"
        rc, out, err, post = self._run("--save", str(out_json),
                                       "--call-time", "2026-05-08T14:30")
        self.assertEqual(rc, 0, msg=err)
        bundle = json.loads(out_json.read_text())
        self.assertIn("(user-provided)", bundle["metadata"]["call_recorded_at"])
        self.assertIn("2026-05-08", bundle["metadata"]["recorded_at"])


class _FakeAlignedToken:
    def __init__(self, text: str, start: float, end: float, confidence: float = 1.0):
        self.text = text
        self.start = start
        self.end = end
        self.confidence = confidence


class _FakeAlignedSentence:
    def __init__(self, text: str, tokens: list[_FakeAlignedToken],
                 start: float = 0.0, end: float = 0.0):
        self.text = text
        self.tokens = tokens
        self.start = start
        self.end = end
        self.duration = end - start
        self.confidence = 1.0


class _FakeAlignedResult:
    def __init__(self, text: str, sentences: list[_FakeAlignedSentence]):
        self.text = text
        self.sentences = sentences


class _FakeParakeetModel:
    """Stands in for parakeet_mlx.BaseParakeet so tests don't need MLX or
    a real model on disk. `recorded_calls` lets tests assert the right
    args were forwarded."""

    def __init__(self, result: _FakeAlignedResult):
        self._result = result
        self.recorded_calls: list[dict] = []

    def transcribe(self, path, *, chunk_duration=None, overlap_duration=15.0,
                   chunk_callback=None, **_):
        self.recorded_calls.append({
            "path": str(path),
            "chunk_duration": chunk_duration,
            "overlap_duration": overlap_duration,
        })
        if chunk_callback:
            chunk_callback(0.0, 60.0)
            chunk_callback(30.0, 60.0)
            chunk_callback(60.0, 60.0)
        return self._result


def _build_sample_aligned_result() -> _FakeAlignedResult:
    """Mimics parakeet's actual token output: leading-space marks word
    starts ('▁'-style), continuations and punctuation have no leading
    space and get merged onto the open word."""
    tokens = [
        _FakeAlignedToken(" H", 0.0, 0.16, 0.99),
        _FakeAlignedToken("ello", 0.16, 0.5, 0.95),
        _FakeAlignedToken(" world", 0.6, 1.0, 0.97),
        _FakeAlignedToken(".", 1.0, 1.1, 0.99),
        _FakeAlignedToken(" This", 1.2, 1.4, 0.98),
        _FakeAlignedToken(" is", 1.4, 1.5, 0.97),
        _FakeAlignedToken(" a", 1.5, 1.55, 0.96),
        _FakeAlignedToken(" test", 1.6, 1.95, 0.99),
        _FakeAlignedToken(".", 1.95, 2.0, 0.99),
    ]
    sentence = _FakeAlignedSentence(
        text="Hello world. This is a test.", tokens=tokens, start=0.0, end=2.0,
    )
    return _FakeAlignedResult(
        text="Hello world. This is a test.", sentences=[sentence],
    )


class TestParakeetBackend(unittest.TestCase):
    """Pure-shape tests for parakeet_backend - mocked model, no MLX runtime."""

    def setUp(self):
        # Module-level import so each test sees a fresh cache state.
        from local_scribe.asr.backends import parakeet_backend
        self.pb = parakeet_backend
        self.pb._CACHED_MODEL = None
        self.pb._CACHED_MODEL_ID = None

    def test_aligned_result_to_deepgram_shape(self):
        result = _build_sample_aligned_result()
        payload = self.pb.aligned_result_to_deepgram(
            result, duration_s=2.0, model_id="test/parakeet",
        )
        self.assertEqual(payload["metadata"]["models"], ["test/parakeet"])
        self.assertEqual(payload["metadata"]["duration"], 2.0)
        self.assertEqual(payload["metadata"]["channels"], 1)
        self.assertIn("test/parakeet", payload["metadata"]["model_info"])

        chan = payload["results"]["channels"][0]
        alt = chan["alternatives"][0]
        self.assertEqual(alt["transcript"], "Hello world. This is a test.")
        self.assertEqual(alt["languages"], ["en"])
        self.assertEqual(chan["detected_language"], "en")
        # 6 real words, with punctuation snapped onto the preceding word.
        self.assertEqual([w["word"] for w in alt["words"]],
                         ["Hello", "world.", "This", "is", "a", "test."])
        self.assertEqual(alt["words"][0]["word"], "Hello")
        self.assertAlmostEqual(alt["words"][0]["start"], 0.0)
        self.assertAlmostEqual(alt["words"][0]["end"], 0.5)
        # Confidence is the minimum across merged sub-tokens (0.95 vs 0.99).
        self.assertAlmostEqual(alt["words"][0]["confidence"], 0.95)
        self.assertEqual(alt["words"][1]["word"], "world.")
        self.assertAlmostEqual(alt["words"][1]["start"], 0.6)
        self.assertAlmostEqual(alt["words"][1]["end"], 1.1)

    def test_tokens_to_words_handles_unicode_underscore_marker(self):
        """SentencePiece sometimes emits U+2581 '▁' instead of plain space
        as the word-start marker. We strip both."""
        tokens = [
            _FakeAlignedToken("\u2581Hi", 0.0, 0.2),
            _FakeAlignedToken("\u2581there", 0.3, 0.6),
        ]
        words = self.pb._tokens_to_words(tokens)
        self.assertEqual([w["word"] for w in words], ["Hi", "there"])

    def test_tokens_to_words_skips_blank(self):
        tokens = [
            _FakeAlignedToken("", 0.0, 0.1),
            _FakeAlignedToken("   ", 0.1, 0.2),
            _FakeAlignedToken(" ok", 0.2, 0.4),
        ]
        words = self.pb._tokens_to_words(tokens)
        self.assertEqual([w["word"] for w in words], ["ok"])

    def test_aligned_result_to_deepgram_empty(self):
        empty = _FakeAlignedResult(text="", sentences=[])
        payload = self.pb.aligned_result_to_deepgram(
            empty, duration_s=0.0, model_id="m",
        )
        alt = payload["results"]["channels"][0]["alternatives"][0]
        self.assertEqual(alt["transcript"], "")
        self.assertEqual(alt["words"], [])

    def test_aligned_result_skips_blank_tokens(self):
        result = _FakeAlignedResult(
            text="ok",
            sentences=[
                _FakeAlignedSentence(
                    text="ok",
                    tokens=[
                        _FakeAlignedToken("", 0.0, 0.1),
                        _FakeAlignedToken("   ", 0.1, 0.2),
                        _FakeAlignedToken(" ok", 0.2, 0.3),
                    ],
                ),
            ],
        )
        payload = self.pb.aligned_result_to_deepgram(
            result, duration_s=0.3, model_id="m",
        )
        words = payload["results"]["channels"][0]["alternatives"][0]["words"]
        self.assertEqual([w["word"] for w in words], ["ok"])

    def test_last_token_end_fallback(self):
        result = _build_sample_aligned_result()
        self.assertAlmostEqual(self.pb._last_token_end(result), 2.0)

    def test_load_model_caches(self):
        sentinel = object()

        def _fake_from_pretrained(model_id, **_):
            return sentinel

        with mock.patch.dict(sys.modules, {
            "parakeet_mlx": mock.Mock(from_pretrained=_fake_from_pretrained),
        }):
            m1 = self.pb.load_model("hf-org/p1")
            m2 = self.pb.load_model("hf-org/p1")
        self.assertIs(m1, sentinel)
        self.assertIs(m2, sentinel)

    def test_transcribe_to_deepgram_uses_callbacks(self):
        fake_model = _FakeParakeetModel(_build_sample_aligned_result())
        self.pb._CACHED_MODEL = fake_model
        self.pb._CACHED_MODEL_ID = "test/parakeet"

        starts: list[tuple[float, str]] = []
        progresses: list[tuple[float, float, float]] = []

        with mock.patch.object(self.pb, "audio_duration_seconds", return_value=60.0):
            payload = self.pb.transcribe_to_deepgram(
                Path("/tmp/fake.wav"),
                model_id="test/parakeet",
                on_start=lambda d, lang: starts.append((d, lang)),
                on_progress=lambda p, c, t: progresses.append((p, c, t)),
            )

        self.assertEqual(starts, [(60.0, "en")])
        self.assertGreaterEqual(len(progresses), 2)
        self.assertAlmostEqual(progresses[-1][0], 1.0)
        # on_progress must report seconds (current_s, total_s), not the raw
        # sample positions parakeet-mlx hands the chunk_callback. With a 60s
        # duration the final tick should be (1.0, 60.0, 60.0).
        self.assertAlmostEqual(progresses[-1][1], 60.0)
        self.assertAlmostEqual(progresses[-1][2], 60.0)

        self.assertEqual(len(fake_model.recorded_calls), 1)
        self.assertEqual(fake_model.recorded_calls[0]["path"], "/tmp/fake.wav")

        self.assertEqual(payload["metadata"]["duration"], 60.0)
        self.assertEqual(payload["metadata"]["models"], ["test/parakeet"])

    def test_transcribe_to_deepgram_falls_back_to_token_end_when_no_duration(self):
        fake_model = _FakeParakeetModel(_build_sample_aligned_result())
        self.pb._CACHED_MODEL = fake_model
        self.pb._CACHED_MODEL_ID = "test/parakeet"

        with mock.patch.object(self.pb, "audio_duration_seconds", return_value=0.0):
            payload = self.pb.transcribe_to_deepgram(
                Path("/tmp/fake.wav"),
                model_id="test/parakeet",
            )

        self.assertAlmostEqual(payload["metadata"]["duration"], 2.0)


class TestTranscribeWithParakeetWrapper(unittest.TestCase):
    """transcribe_file.transcribe_with_parakeet must wire callbacks and shape
    output the same as the HTTP whisper path."""

    def test_wrapper_returns_deepgram_payload(self):
        fake_payload = {
            "metadata": {"models": ["mlx-community/parakeet-tdt-0.6b-v3"], "duration": 1.0},
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
        }
        with mock.patch("local_scribe.asr.backends.parakeet_backend.transcribe_to_deepgram",
                        return_value=fake_payload) as t, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            out = tf.transcribe_with_parakeet(
                Path("/tmp/x.wav"),
                model_id="mlx-community/parakeet-tdt-0.6b-v3",
                progress=False,
            )
        self.assertEqual(out, fake_payload)
        self.assertEqual(t.call_count, 1)
        kwargs = t.call_args.kwargs
        self.assertEqual(kwargs["model_id"], "mlx-community/parakeet-tdt-0.6b-v3")
        self.assertIsNone(kwargs["on_progress"])

    def test_wrapper_passes_progress_callback_when_enabled(self):
        with mock.patch("local_scribe.asr.backends.parakeet_backend.transcribe_to_deepgram",
                        return_value={"metadata": {}, "results": {"channels": [
                            {"alternatives": [{"transcript": ""}]}]}}) as t, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            tf.transcribe_with_parakeet(Path("/tmp/x.wav"), progress=True)
        self.assertIsNotNone(t.call_args.kwargs["on_progress"])


class TestDiarizationBackend(unittest.TestCase):
    """Pure helpers in diarization_backend - no sherpa-onnx runtime needed."""

    def setUp(self):
        from local_scribe.asr.backends import diarization_backend
        self.db = diarization_backend

    def test_attach_speaker_to_words_overlap(self):
        words = [
            {"word": "hi", "start": 0.5, "end": 0.8},
            {"word": "there", "start": 1.0, "end": 1.3},
            {"word": "hey", "start": 5.5, "end": 5.7},
        ]
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "start": 5.0, "end": 6.0},
        ]
        out = self.db.attach_speaker_to_words(words, turns)
        self.assertEqual([w["speaker"] for w in out],
                         ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"])

    def test_attach_speaker_to_words_falls_back_to_nearest(self):
        # Word sits in a gap between turns - should snap to nearest turn.
        words = [{"word": "hmm", "start": 2.4, "end": 2.6}]
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "start": 3.0, "end": 5.0},
        ]
        out = self.db.attach_speaker_to_words(words, turns)
        self.assertEqual(out[0]["speaker"], "SPEAKER_00")

    def test_attach_speaker_to_words_no_turns_defaults_speaker0(self):
        out = self.db.attach_speaker_to_words(
            [{"word": "hi", "start": 0.0, "end": 0.5}], turns=[]
        )
        self.assertEqual(out[0]["speaker"], "SPEAKER_00")

    def test_group_words_into_lines_alternating(self):
        words = [
            {"word": "hi", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
            {"word": "there", "start": 0.4, "end": 0.7, "speaker": "SPEAKER_00"},
            {"word": "hey", "start": 1.0, "end": 1.2, "speaker": "SPEAKER_01"},
            {"word": "back", "start": 1.3, "end": 1.5, "speaker": "SPEAKER_01"},
            {"word": "ok", "start": 2.0, "end": 2.2, "speaker": "SPEAKER_00"},
        ]
        lines = self.db.group_words_into_lines(words)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["speaker"], "SPEAKER_00")
        self.assertEqual(lines[0]["text"], "hi there")
        self.assertEqual(lines[1]["speaker"], "SPEAKER_01")
        self.assertEqual(lines[1]["text"], "hey back")
        self.assertEqual(lines[2]["speaker"], "SPEAKER_00")

    def test_group_words_splits_long_silence_within_speaker(self):
        words = [
            {"word": "first", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
            {"word": "second", "start": 5.0, "end": 5.5, "speaker": "SPEAKER_00"},
        ]
        lines = self.db.group_words_into_lines(words, max_gap_seconds=1.5)
        self.assertEqual(len(lines), 2)

    def test_group_words_uses_punctuated_word_when_present(self):
        words = [
            {"word": "ok", "punctuated_word": "ok,",
             "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
            {"word": "go", "punctuated_word": "go.",
             "start": 0.4, "end": 0.6, "speaker": "SPEAKER_00"},
        ]
        lines = self.db.group_words_into_lines(words)
        self.assertEqual(lines[0]["text"], "ok, go.")

    def test_render_diarized_transcript_with_labels(self):
        lines = [
            {"speaker": "SPEAKER_00", "text": "hi there", "start": 0.0, "end": 0.5},
            {"speaker": "SPEAKER_01", "text": "hey back", "start": 1.0, "end": 1.4},
        ]
        out = self.db.render_diarized_transcript(
            lines, speaker_labels={"SPEAKER_00": "Moss", "SPEAKER_01": "Caller"},
        )
        self.assertEqual(out, "Moss: hi there\nCaller: hey back")

    def test_render_diarized_transcript_with_timestamps(self):
        lines = [{"speaker": "SPEAKER_00", "text": "hello", "start": 65.5, "end": 66.0}]
        out = self.db.render_diarized_transcript(lines, include_timestamps=True)
        self.assertIn("01:05.50", out)
        self.assertIn("01:06.00", out)
        self.assertIn("SPEAKER_00", out)

    def test_render_diarized_transcript_unknown_speaker_falls_back_to_id(self):
        lines = [{"speaker": "SPEAKER_42", "text": "x", "start": 0.0, "end": 0.1}]
        out = self.db.render_diarized_transcript(
            lines, speaker_labels={"SPEAKER_00": "Moss"},
        )
        self.assertIn("SPEAKER_42:", out)

    def test_infer_speaker_labels_extracts_json_from_llm(self):
        llm_response = {
            "choices": [
                {"message": {"content": '{"SPEAKER_00": "Moss", "SPEAKER_01": "Caller"}'}}
            ]
        }
        with mock.patch("local_scribe.asr.backends.diarization_backend.requests.post",
                        return_value=FakeResponse(200, llm_response)):
            mapping = self.db.infer_speaker_labels(
                "SPEAKER_00: hi\nSPEAKER_01: there",
                llm_url="http://x", llm_model="m",
            )
        self.assertEqual(mapping, {"SPEAKER_00": "Moss", "SPEAKER_01": "Caller"})

    def test_infer_speaker_labels_strips_think_tags(self):
        llm_response = {
            "choices": [
                {"message": {"content": "<think>...</think>\n{\"SPEAKER_00\": \"Alex\"}"}}
            ]
        }
        with mock.patch("local_scribe.asr.backends.diarization_backend.requests.post",
                        return_value=FakeResponse(200, llm_response)):
            mapping = self.db.infer_speaker_labels(
                "SPEAKER_00: hi", llm_url="http://x", llm_model="m",
            )
        self.assertEqual(mapping, {"SPEAKER_00": "Alex"})

    def test_infer_speaker_labels_returns_empty_on_garbage(self):
        with mock.patch("local_scribe.asr.backends.diarization_backend.requests.post",
                        return_value=FakeResponse(
                            200, {"choices": [{"message": {"content": "no json here"}}]})):
            mapping = self.db.infer_speaker_labels(
                "SPEAKER_00: hi", llm_url="http://x", llm_model="m",
            )
        self.assertEqual(mapping, {})

    def test_infer_speaker_labels_no_speakers_returns_empty(self):
        # When the input doesn't contain any SPEAKER_NN pattern, we don't
        # bother calling the LLM.
        with mock.patch("local_scribe.asr.backends.diarization_backend.requests.post") as p:
            mapping = self.db.infer_speaker_labels(
                "no speakers in this text", llm_url="http://x", llm_model="m",
            )
        self.assertEqual(mapping, {})
        self.assertEqual(p.call_count, 0)

    def test_infer_speaker_labels_filters_invalid_keys(self):
        llm_response = {
            "choices": [
                {"message": {"content":
                    '{"SPEAKER_00": "Moss", "garbage": "x", "SPEAKER_01": ""}'}}
            ]
        }
        with mock.patch("local_scribe.asr.backends.diarization_backend.requests.post",
                        return_value=FakeResponse(200, llm_response)):
            mapping = self.db.infer_speaker_labels(
                "SPEAKER_00: hi\nSPEAKER_01: hello",
                llm_url="http://x", llm_model="m",
            )
        # "garbage" is filtered (not SPEAKER_NN); SPEAKER_01="" is filtered (empty)
        self.assertEqual(mapping, {"SPEAKER_00": "Moss"})


class TestMainAsrBackendDispatch(unittest.TestCase):
    """main() must route to the right ASR backend based on --asr-backend."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="whisper_dispatch_test_")
        self.cache_dir = Path(self._tmp) / "cache"
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as t:
            t.write(FAKE_AUDIO)
            self.audio = Path(t.name)

    def tearDown(self):
        import shutil as _shutil
        _shutil.rmtree(self._tmp, ignore_errors=True)
        self.audio.unlink(missing_ok=True)

    def test_parakeet_backend_invokes_in_process(self):
        """When --asr-backend parakeet is set, no HTTP call is made and
        parakeet_backend.transcribe_to_deepgram is called instead."""
        fake_payload = {
            "metadata": {
                "duration": 5.0,
                "models": ["mlx-community/parakeet-tdt-0.6b-v3"],
                "model_info": {"mlx-community/parakeet-tdt-0.6b-v3": {
                    "name": "mlx-community/parakeet-tdt-0.6b-v3",
                    "version": "parakeet-mlx", "arch": "Parakeet-TDT",
                }},
            },
            "results": {"channels": [{
                "alternatives": [{
                    "transcript": "Hello from parakeet.",
                    "confidence": 1.0, "languages": ["en"], "words": [],
                }],
                "detected_language": "en",
            }]},
        }
        argv = [
            str(self.audio),
            "--no-llm",
            "--asr-backend", "parakeet",
            "--cache-dir", str(self.cache_dir),
        ]
        post = mock.Mock()
        with mock.patch("local_scribe.asr.backends.parakeet_backend.transcribe_to_deepgram",
                        return_value=fake_payload) as t, \
             mock.patch("local_scribe.asr.transcribe_file.requests.post", post), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = tf.main(argv)

        self.assertEqual(rc, 0, msg=err.getvalue())
        self.assertEqual(t.call_count, 1)
        self.assertEqual(post.call_count, 0,
                         msg="parakeet path must not hit any HTTP server")

        cache_file = tf.cache_path_for(self.audio, cache_dir=self.cache_dir)
        cached = json.loads(cache_file.read_text())
        self.assertEqual(
            cached["_source"]["whisper_model"],
            "mlx-community/parakeet-tdt-0.6b-v3",
        )
        self.assertIn("Hello from parakeet", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
