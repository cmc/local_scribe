"""Tests for inspector_server.py — endpoints, traversal-defence, auth,
PUT validation, sessions enumeration."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import config
import inspector_server


def _make_cfg(data_dir: Path, *, auth_token: str | None = None) -> config.Config:
    raw = copy.deepcopy(config.DEFAULT_CONFIG)
    raw["char"]["data_dir"] = str(data_dir)
    raw["inspector"]["auth_token"] = auth_token
    return config.Config(raw=raw)


def _seed_session(sessions_dir: Path, session_id: str, *, with_audio=True,
                  with_transcript=True, with_note=True,
                  title="Test Meeting") -> Path:
    p = sessions_dir / session_id
    p.mkdir(parents=True, exist_ok=True)
    (p / "_meta.json").write_text(json.dumps({
        "id": session_id,
        "title": title,
        "created_at": "2026-05-09T22:00:00.000Z",
        "participants": [],
    }))
    if with_audio:
        (p / "audio.mp3").write_bytes(b"fake mp3 bytes" * 10)
    if with_transcript:
        (p / "transcript.json").write_text(json.dumps({
            "transcripts": [{
                "id": "t1",
                "session_id": session_id,
                "words": [
                    {"id": "w1", "text": "hello", "start": 0.0, "end": 0.5},
                    {"id": "w2", "text": "world", "start": 0.5, "end": 1.0},
                ],
                "speaker_hints": [
                    {"id": "h1", "type": "name", "value": "Alice", "word_id": ["w1"]},
                    {"id": "h2", "type": "name", "value": "Bob",   "word_id": ["w2"]},
                ],
            }],
        }))
    if with_note:
        (p / "Faithful Notes.md").write_text("# Summary\n\nContent here.\n")
    return p


class HealthAndIndexTests(unittest.TestCase):
    def test_health_ping(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

    def test_index_returns_html(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/html", r.headers["content-type"])
            self.assertIn("local_scribe", r.text)
            self.assertIn("/api/sessions", r.text)


class SessionsTests(unittest.TestCase):
    def test_lists_sessions_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sessions = data_dir / "sessions"
            _seed_session(sessions, "aaa-aaa", title="Alpha")
            # Make the second one newer by mtime AND by created_at
            p2 = _seed_session(sessions, "bbb-bbb", title="Beta")
            (p2 / "_meta.json").write_text(json.dumps({
                "id": "bbb-bbb",
                "title": "Beta",
                "created_at": "2026-06-01T00:00:00.000Z",
            }))
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            data = client.get("/api/sessions").json()
            self.assertEqual(data["count"], 2)
            self.assertEqual(data["sessions"][0]["id"], "bbb-bbb")
            self.assertEqual(data["sessions"][1]["id"], "aaa-aaa")
            self.assertEqual(data["sessions"][0]["title"], "Beta")
            self.assertTrue(data["sessions"][0]["has_audio"])
            self.assertTrue(data["sessions"][0]["has_transcript"])
            self.assertEqual(data["sessions"][0]["notes"], ["Faithful Notes.md"])

    def test_session_detail_flattens_words_into_paragraphs(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sessions = data_dir / "sessions"
            _seed_session(sessions, "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            data = client.get("/api/sessions/abc-123").json()
            self.assertEqual(data["meta"]["title"], "Test Meeting")
            self.assertEqual(len(data["transcript"]["paragraphs"]), 2)
            self.assertEqual(data["transcript"]["paragraphs"][0]["speaker"],
                             "Alice")
            self.assertEqual(data["transcript"]["paragraphs"][0]["text"], "hello")
            self.assertEqual(data["transcript"]["paragraphs"][1]["speaker"], "Bob")
            self.assertEqual(len(data["notes"]), 1)
            self.assertIn("# Summary", data["notes"][0]["content"])

    def test_traversal_attempt_returns_400(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            client = TestClient(inspector_server.create_app(cfg))
            for bad in ["..", "../etc", "../../etc/passwd"]:
                r = client.get(f"/api/sessions/{bad}")
                self.assertIn(r.status_code, (400, 404),
                              f"{bad!r} -> {r.status_code}")

    def test_404_for_unknown_session(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "sessions").mkdir()
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions/no-such")
            self.assertEqual(r.status_code, 404)

    def test_audio_endpoint_streams_file(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sessions = data_dir / "sessions"
            _seed_session(sessions, "z-z-z")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions/z-z-z/audio")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "audio/mpeg")
            self.assertGreater(len(r.content), 0)

    def test_transcript_txt_renders_speaker_prefixed(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sessions = data_dir / "sessions"
            _seed_session(sessions, "z-z-z")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions/z-z-z/transcript.txt")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Alice:", r.text)
            self.assertIn("Bob:", r.text)


class ConfigEndpointTests(unittest.TestCase):
    def test_get_returns_layered_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/config")
            self.assertEqual(r.status_code, 200)
            d = r.json()["config"]
            self.assertEqual(d["asr"]["backend"], "parakeet")

    def test_put_rejects_invalid_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            client = TestClient(inspector_server.create_app(cfg))
            bad = copy.deepcopy(config.DEFAULT_CONFIG)
            bad["asr"]["port"] = -1
            r = client.put("/api/config", json=bad)
            self.assertEqual(r.status_code, 400)
            self.assertIn("errors", r.json())

    def test_put_persists_to_temp_path(self):
        # We monkey-patch DEFAULT_CONFIG_PATH so the test doesn't touch
        # the user's real config file.
        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = Path(td) / "config.json"
            real = config.DEFAULT_CONFIG_PATH
            config.DEFAULT_CONFIG_PATH = tmp_cfg
            try:
                cfg = _make_cfg(Path(td))
                client = TestClient(inspector_server.create_app(cfg))
                payload = copy.deepcopy(config.DEFAULT_CONFIG)
                payload["asr"]["port"] = 8765
                r = client.put("/api/config", json=payload)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(tmp_cfg.is_file())
                written = json.loads(tmp_cfg.read_text())
                self.assertEqual(written["asr"]["port"], 8765)
            finally:
                config.DEFAULT_CONFIG_PATH = real


class CharAuditEndpointTests(unittest.TestCase):
    def test_audit_endpoint_returns_summary(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "settings.json").write_text(json.dumps({
                "ai": {
                    "current_stt_provider": "openai",
                    "current_stt_model": "gpt-4o-transcribe",
                    "stt": {"openai": {
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "local",
                    }},
                },
            }))
            (data_dir / "store.json").write_text(json.dumps({
                "analytics": json.dumps({"Disabled": True}),
            }))
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/char/audit")
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertTrue(d["settings_present"])
            self.assertGreaterEqual(d["summary"]["ok"], 4)
            self.assertEqual(d["summary"]["warn"], 0)


class AuthTests(unittest.TestCase):
    def test_no_token_means_open_api(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(inspector_server.create_app(cfg))
            self.assertEqual(client.get("/api/health").status_code, 200)

    def test_token_required_when_set(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token="hunter2hunter2")
            client = TestClient(inspector_server.create_app(cfg))
            # No header -> 401
            self.assertEqual(client.get("/api/health").status_code, 401)
            # Wrong header -> 401
            self.assertEqual(
                client.get("/api/health", headers={"authorization": "Bearer wrong"})
                      .status_code,
                401,
            )
            # Right header -> 200
            self.assertEqual(
                client.get("/api/health",
                           headers={"authorization": "Bearer hunter2hunter2"})
                      .status_code,
                200,
            )

    def test_index_unauthenticated_even_with_token(self):
        # The HTML page itself loads without auth so the browser can
        # render the bearer-token entry UI before the user's pasted it.
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token="abc")
            client = TestClient(inspector_server.create_app(cfg))
            self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
