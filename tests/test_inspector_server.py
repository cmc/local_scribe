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


class TranscriptConfidenceAndAirtimeTests(unittest.TestCase):
    """``local_scribe.diarization.word_confidences`` + ``.speakers``
    flow through the flatten step and surface in both /api/sessions/
    {id} and /transcript.txt."""

    def _seed(self, data_dir: Path, session_id: str, *,
              word_confidences=None, speakers=None) -> None:
        p = data_dir / "sessions" / session_id
        p.mkdir(parents=True, exist_ok=True)
        (p / "_meta.json").write_text(json.dumps({
            "id": session_id, "title": "Conf test",
            "created_at": "2026-05-09T22:00:00.000Z",
        }))
        (p / "audio.mp3").write_bytes(b"x" * 10)
        body = {
            "transcripts": [{
                "id": "t1", "session_id": session_id,
                "words": [
                    {"id": "w1", "text": "hello", "start": 0.0, "end": 0.5},
                    {"id": "w2", "text": "world", "start": 0.5, "end": 1.0},
                    {"id": "w3", "text": "again", "start": 1.0, "end": 1.5},
                ],
                "speaker_hints": [
                    {"id": "h1", "type": "name", "value": "Alice", "word_id": "w1"},
                    {"id": "h2", "type": "name", "value": "Alice", "word_id": "w2"},
                    {"id": "h3", "type": "name", "value": "Bob", "word_id": "w3"},
                ],
            }],
        }
        diar = {}
        if word_confidences is not None:
            diar["word_confidences"] = word_confidences
        if speakers is not None:
            diar["speakers"] = speakers
        if diar:
            body["local_scribe"] = {"diarization": diar}
        (p / "transcript.json").write_text(json.dumps(body))

    def test_paragraph_confidence_is_mean_of_word_confidences(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed(data_dir, "s1",
                       word_confidences=[0.9, 0.7, 0.4],
                       speakers=[
                           {"label": "Alice", "seconds": 1.0, "percent": 0.667,
                            "mean_confidence": 0.8},
                           {"label": "Bob", "seconds": 0.5, "percent": 0.333,
                            "mean_confidence": 0.4},
                       ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            data = client.get("/api/sessions/s1").json()
            paragraphs = data["transcript"]["paragraphs"]
            # Two paragraphs: Alice (mean of 0.9 and 0.7 = 0.8), Bob (0.4).
            self.assertEqual(len(paragraphs), 2)
            self.assertAlmostEqual(paragraphs[0]["confidence"], 0.8, places=6)
            self.assertAlmostEqual(paragraphs[1]["confidence"], 0.4, places=6)
            speakers = data["transcript"]["speakers"]
            self.assertEqual(len(speakers), 2)
            self.assertEqual(speakers[0]["label"], "Alice")

    def test_paragraph_confidence_is_none_when_array_absent(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed(data_dir, "s1")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            data = client.get("/api/sessions/s1").json()
            for p in data["transcript"]["paragraphs"]:
                self.assertIsNone(p["confidence"])
            self.assertEqual(data["transcript"]["speakers"], [])

    def test_transcript_txt_renders_confidence_and_airtime(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed(data_dir, "s1",
                       word_confidences=[0.9, 0.9, 0.4],
                       speakers=[
                           {"label": "Alice", "seconds": 1.0, "percent": 0.667,
                            "mean_confidence": 0.9},
                           {"label": "Bob", "seconds": 0.5, "percent": 0.333,
                            "mean_confidence": 0.4},
                       ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            text = client.get("/api/sessions/s1/transcript.txt").text
            # Speaker-prefixed lines now include "(NN%)".
            self.assertIn("Alice (90%): hello world", text)
            self.assertIn("Bob (40%): again", text)
            # Trailing airtime block.
            self.assertIn("--- Speaker airtime ---", text)
            self.assertIn("Alice: 0m 01s", text)
            self.assertIn("(67%)", text)
            self.assertIn("90% mean confidence", text)

    def test_transcript_txt_omits_airtime_when_none(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed(data_dir, "s1")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            text = client.get("/api/sessions/s1/transcript.txt").text
            self.assertNotIn("--- Speaker airtime ---", text)


class TranscriptHistoryEndpointTests(unittest.TestCase):
    """Endpoint tests for the new /api/sessions/{id}/history surface."""

    def _seed_with_history(self, data_dir: Path, session_id: str,
                            *, archives: list[dict]) -> Path:
        import transcript_history
        sd = _seed_session(data_dir / "sessions", session_id)
        h = transcript_history.ensure_history_dir(sd)
        for i, doc in enumerate(archives):
            fname = f"2026051{i}T120000Z_{'a' * 7}.json"
            (h / fname).write_text(json.dumps(doc))
        return sd

    def test_history_empty_when_no_dir(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions/abc-123/history")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["count"], 0)
            self.assertEqual(r.json()["entries"], [])

    def test_history_lists_entries_with_embedded_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed_with_history(data_dir, "abc-123", archives=[
                {
                    "transcripts": [],
                    "local_scribe": {
                        "asr_model": "parakeet",
                        "diarization": {"algorithm": "auto_silhouette",
                                        "num_speakers": 3},
                    },
                },
                {"transcripts": []},  # legacy archive, no metadata
            ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            data = client.get("/api/sessions/abc-123/history").json()
            self.assertEqual(data["count"], 2)
            metadatas = [e["metadata"] for e in data["entries"]]
            asr_models = {m.get("asr_model") for m in metadatas}
            self.assertEqual(asr_models, {"parakeet", None})

    def test_history_count_surfaces_in_sessions_list(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed_with_history(data_dir, "abc-123", archives=[
                {"transcripts": []},
                {"transcripts": []},
            ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions").json()
            self.assertEqual(r["sessions"][0]["history_count"], 2)

    def test_history_file_get_returns_archive_content(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed_with_history(data_dir, "abc-123", archives=[
                {"transcripts": [], "local_scribe": {"asr_model": "x"}},
            ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            listed = client.get("/api/sessions/abc-123/history").json()
            fname = listed["entries"][0]["filename"]
            r = client.get(f"/api/sessions/abc-123/history/{fname}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["local_scribe"]["asr_model"], "x")

    def test_history_file_get_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            # Bad names that the route still matches as a single path
            # segment -- our validator must reject these with 400, not
            # disclose 404 or actually open the file. Starlette decodes
            # %2F to / which falls outside the route entirely (404),
            # so the meaningful cases are literal ".." patterns.
            # ".." alone normalises to /api/sessions/abc-123/history
            # (the collection GET route, returns 200), so only filename
            # cases with ".." as a substring exercise our validator.
            for bad in ["..foo.json", "....json", "foo..json"]:
                r = client.get(f"/api/sessions/abc-123/history/{bad}")
                self.assertEqual(
                    r.status_code, 400, f"{bad} -> {r.status_code}",
                )

    def test_history_file_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = self._seed_with_history(data_dir, "abc-123", archives=[
                {"transcripts": [], "local_scribe": {"asr_model": "x"}},
            ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            listed = client.get("/api/sessions/abc-123/history").json()
            fname = listed["entries"][0]["filename"]
            r = client.delete(f"/api/sessions/abc-123/history/{fname}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["deleted"], fname)
            # Disk-level check.
            self.assertFalse((sd / ".local_scribe_history" / fname).exists())

    def test_history_file_delete_404_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.delete("/api/sessions/abc-123/history/no-such.json")
            self.assertEqual(r.status_code, 404)

    def test_history_file_delete_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            # Note: ".." alone normalises to the parent route segment
            # (Starlette's behaviour, returns 405 against GET-only
            # /history collection), so the meaningful traversal cases
            # are filenames that contain ".." as a substring.
            for bad in ["..foo.json", "....json", "foo..json"]:
                r = client.delete(f"/api/sessions/abc-123/history/{bad}")
                self.assertEqual(
                    r.status_code, 400, f"{bad} -> {r.status_code}",
                )


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
