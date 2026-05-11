"""Tests for inspector_server.py — endpoints, traversal-defense, auth,
PUT validation, sessions enumeration."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from local_scribe.common import config
from local_scribe.inspector import inspector_server


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

    def test_transcript_txt_renders_clean_labels_plus_airtime(self):
        # User explicitly asked for the body to be readable -- the
        # transcript lines must NOT carry an inline (NN%) tag; the
        # confidence number lives in the airtime block at the bottom.
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
            # Already-named speakers (Alice, Bob) pass through; backend
            # "speaker_N" labels would be rewritten to "Speaker N+1".
            self.assertIn("Alice: hello world", text)
            self.assertIn("Bob: again", text)
            # No inline percentage tag in the body.
            self.assertNotIn("Alice (90%)", text)
            self.assertNotIn("Bob (40%)", text)
            # Trailing airtime block still carries the metric.
            self.assertIn("--- Speaker airtime ---", text)
            self.assertIn("Alice: 0m 01s", text)
            self.assertIn("(67%)", text)
            self.assertIn("90% mean confidence", text)

    def test_pretty_speaker_relabels_backend_indices(self):
        # speaker_0 -> "Speaker 1", speaker_1 -> "Speaker 2".
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            # Use real Char-style hints so the flatten path produces
            # the backend label "speaker_0" etc., which the renderer
            # rewrites for display.
            p = data_dir / "sessions" / "s2"
            p.mkdir(parents=True)
            (p / "_meta.json").write_text(json.dumps({
                "id": "s2", "title": "indexed", "created_at": "2026-05-09T00:00:00.000Z",
            }))
            (p / "audio.mp3").write_bytes(b"x")
            (p / "transcript.json").write_text(json.dumps({
                "transcripts": [{
                    "id": "t1", "session_id": "s2",
                    "words": [
                        {"id": "w1", "text": "hi", "start": 0.0, "end": 0.5},
                        {"id": "w2", "text": "yo", "start": 0.5, "end": 1.0},
                    ],
                    "speaker_hints": [
                        {"id": "h1", "type": "provider_speaker_index",
                         "value": '{"provider":"openai","channel":0,"speaker_index":0}',
                         "word_id": "w1"},
                        {"id": "h2", "type": "provider_speaker_index",
                         "value": '{"provider":"openai","channel":0,"speaker_index":1}',
                         "word_id": "w2"},
                    ],
                }],
                "local_scribe": {"diarization": {
                    "speakers": [
                        {"label": "speaker_0", "seconds": 0.5, "percent": 0.5,
                         "mean_confidence": 0.8},
                        {"label": "speaker_1", "seconds": 0.5, "percent": 0.5,
                         "mean_confidence": 0.7},
                    ],
                }},
            }))
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            text = client.get("/api/sessions/s2/transcript.txt").text
            self.assertIn("Speaker 1: hi", text)
            self.assertIn("Speaker 2: yo", text)
            self.assertIn("Speaker 1: 0m 00s", text)
            self.assertIn("Speaker 2: 0m 00s", text)
            # The raw underlying labels should never leak in the
            # rendered output.
            self.assertNotIn("speaker_0:", text)
            self.assertNotIn("speaker_1:", text)

    def test_transcript_txt_omits_airtime_when_none(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed(data_dir, "s1")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            text = client.get("/api/sessions/s1/transcript.txt").text
            self.assertNotIn("--- Speaker airtime ---", text)


_DELETE_CONFIRM = {"confirm": "DELETE"}


class AudioDeleteEndpointTests(unittest.TestCase):
    """``DELETE /api/sessions/{id}/audio`` permanently removes the
    audio.mp3 file. The endpoint requires a typed-DELETE confirm body
    (``{"confirm": "DELETE"}``) on the wire as a defense-in-depth
    check that complements the SPA's modal. These tests cover both
    the bytes-on-disk happy path and the typed-confirm gate."""

    def test_delete_audio_removes_file_and_returns_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "del-aud")
            audio_path = sd / "audio.mp3"
            self.assertTrue(audio_path.is_file())
            size_before = audio_path.stat().st_size
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.request(
                "DELETE", "/api/sessions/del-aud/audio",
                json=_DELETE_CONFIRM,
            )
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["deleted"], "audio.mp3")
            self.assertEqual(body["session_id"], "del-aud")
            self.assertEqual(body["bytes_removed"], size_before)
            self.assertFalse(audio_path.exists())

    def test_delete_audio_404_when_missing(self):
        # Second DELETE of the same audio (or a session that never had
        # one) returns 404; the UI treats this as a soft success.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "del-aud", with_audio=False)
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.request(
                "DELETE", "/api/sessions/del-aud/audio",
                json=_DELETE_CONFIRM,
            )
            self.assertEqual(r.status_code, 404)
            # And the 404 path must NOT touch unrelated session files.
            sd = data_dir / "sessions" / "del-aud"
            self.assertTrue((sd / "transcript.json").is_file())
            self.assertTrue((sd / "_meta.json").is_file())

    def test_delete_audio_does_not_remove_transcript_or_notes(self):
        # Transcript + notes belong to the session even after the audio
        # has been wiped; verify we don't accidentally cascade.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "del-aud")
            (sd / "notes" / "n1.md").parent.mkdir(parents=True, exist_ok=True)
            (sd / "notes" / "n1.md").write_text("keep me")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.request(
                "DELETE", "/api/sessions/del-aud/audio",
                json=_DELETE_CONFIRM,
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue((sd / "transcript.json").is_file())
            self.assertTrue((sd / "notes" / "n1.md").is_file())

    def test_sessions_list_reflects_audio_removal(self):
        # After the audio is gone the per-session aggregate must flip
        # ``has_audio`` to false so the card hides the Delete-audio
        # button on the next refresh.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "del-aud")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            before = client.get("/api/sessions").json()["sessions"][0]
            self.assertTrue(before["has_audio"])
            client.request(
                "DELETE", "/api/sessions/del-aud/audio",
                json=_DELETE_CONFIRM,
            ).raise_for_status()
            after = client.get("/api/sessions").json()["sessions"][0]
            self.assertFalse(after["has_audio"])

    def test_delete_audio_rejects_empty_body(self):
        # Defense-in-depth: a bare ``curl -X DELETE`` (no body) must
        # NOT touch the file. This is the regression that previously
        # let a stolen inspector token destroy data by-passing the
        # client-side modal.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "del-aud")
            audio_path = sd / "audio.mp3"
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.delete("/api/sessions/del-aud/audio")
            self.assertEqual(r.status_code, 400)
            self.assertIn("confirm", r.json()["detail"].lower())
            self.assertTrue(audio_path.is_file(),
                            "audio.mp3 must still exist after a rejected DELETE")

    def test_delete_audio_rejects_wrong_confirm_value(self):
        # ``{"confirm": "yes"}`` / ``{"confirm": "delete"}`` (lowercase) /
        # any spelling variant must be rejected. The word is exact +
        # case-sensitive so accidental autocorrect doesn't count.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "del-aud")
            audio_path = sd / "audio.mp3"
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            for bad in [
                {"confirm": "yes"},
                {"confirm": "delete"},  # lowercase
                {"confirm": "DELETE "},  # trailing space
                {"confirm": True},
                {"confirm": None},
                {"other": "DELETE"},  # right value, wrong key
                {},
                [],  # not even a dict
                "DELETE",  # bare string body
            ]:
                with self.subTest(bad=bad):
                    r = client.request(
                        "DELETE", "/api/sessions/del-aud/audio", json=bad,
                    )
                    self.assertEqual(r.status_code, 400, msg=f"payload={bad!r}")
                    self.assertTrue(audio_path.is_file(),
                                    msg=f"audio destroyed on payload={bad!r}")

    def test_delete_audio_rejects_non_json_body(self):
        # Garbage / non-JSON bodies must also bounce.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "del-aud")
            audio_path = sd / "audio.mp3"
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.request(
                "DELETE", "/api/sessions/del-aud/audio",
                content=b"not-json-at-all{{{",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(r.status_code, 400)
            self.assertTrue(audio_path.is_file())


class ConfirmModalUITests(unittest.TestCase):
    """The typed-DELETE confirmation flow lives client-side, but the
    server must serve the modal DOM and the helper JS for it. These
    smoke tests guard against accidental removal of either."""

    def test_index_html_contains_confirm_modal_skeleton(self):
        # Render the index page and assert the modal markup + helper
        # function name are both present. Cheap regression net for
        # someone refactoring _INDEX_HTML.
        from local_scribe.inspector.inspector_server import _INDEX_HTML
        # Modal skeleton (id used by confirmTypedDelete).
        self.assertIn('id="confirm-modal"', _INDEX_HTML)
        self.assertIn('id="confirm-modal-input"', _INDEX_HTML)
        self.assertIn('id="confirm-modal-ok"', _INDEX_HTML)
        # The user-facing instruction must remain literal "DELETE" --
        # the click handler compares against the exact uppercase
        # string, so a copy that says "delete" would silently lock
        # users out.
        self.assertIn('Type <code>DELETE</code>', _INDEX_HTML)
        # JS helpers wiring it all up.
        self.assertIn('function confirmTypedDelete(', _INDEX_HTML)
        self.assertIn("input.value !== 'DELETE'", _INDEX_HTML)
        self.assertIn('async function deleteSessionAudio(', _INDEX_HTML)


class TranscriptDownloadHeadersTests(unittest.TestCase):
    """Both the live transcript.txt endpoint and the per-archive
    history transcript.txt endpoint must send a
    ``Content-Disposition: attachment`` header so the browser saves
    instead of rendering inline. Verifies the UI's ``Download
    transcript`` / ``Download historical transcript`` buttons work
    regardless of the HTML ``download`` attribute."""

    def test_live_endpoint_sets_attachment_header_with_session_id_filename(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get("/api/sessions/abc-123/transcript.txt")
            self.assertEqual(r.status_code, 200)
            cd = r.headers.get("content-disposition", "")
            self.assertIn("attachment", cd.lower())
            self.assertIn("transcript-abc-123.txt", cd)

    def test_filename_sanitization_strips_path_chars(self):
        """Just in case a future session_id route ever lets slashes
        through (it shouldn't), the helper must collapse them so the
        Content-Disposition value can't smuggle directory components."""
        from local_scribe.inspector.inspector_server import _safe_filename_token
        self.assertEqual(_safe_filename_token("../etc/passwd"), "etc_passwd")
        self.assertEqual(_safe_filename_token(""), "session")
        self.assertEqual(_safe_filename_token("abc"), "abc")
        # The token is the *whole* file name, but the regex collapses
        # any non-portable run to a single underscore.
        self.assertEqual(_safe_filename_token("a b/c"), "a_b_c")


class HistoryTranscriptTxtEndpointTests(unittest.TestCase):
    """``GET /api/sessions/{id}/history/{filename}/transcript.txt``
    renders an archived transcript through the same flatten + airtime
    pipeline as the live endpoint, so the historical download reads
    identically to the live one."""

    def _seed_archive(self, data_dir: Path, session_id: str,
                      *, payload: dict, fname: str = "20260510T120000Z_abc1234.json") -> Path:
        from local_scribe.inspector import transcript_history
        sd = _seed_session(data_dir / "sessions", session_id)
        h = transcript_history.ensure_history_dir(sd)
        (h / fname).write_text(json.dumps(payload))
        return sd

    def test_archive_renders_same_shape_as_live_endpoint(self):
        archive = {
            "transcripts": [{
                "id": "t1", "session_id": "abc-123",
                "words": [
                    {"id": "w1", "text": "hello", "start": 0.0, "end": 0.5},
                    {"id": "w2", "text": "world", "start": 0.5, "end": 1.0},
                ],
                "speaker_hints": [
                    {"id": "h1", "type": "name", "value": "Alice", "word_id": ["w1"]},
                    {"id": "h2", "type": "name", "value": "Bob",   "word_id": ["w2"]},
                ],
            }],
            "local_scribe": {"diarization": {
                "speakers": [
                    {"label": "Alice", "seconds": 0.5, "percent": 0.5,
                     "mean_confidence": 0.9},
                    {"label": "Bob",   "seconds": 0.5, "percent": 0.5,
                     "mean_confidence": 0.8},
                ],
            }},
        }
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._seed_archive(data_dir, "abc-123", payload=archive)
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            # Discover the archive filename via the list endpoint
            # rather than hard-coding it, mirroring how the UI does it.
            listed = client.get("/api/sessions/abc-123/history").json()
            fname = listed["entries"][0]["filename"]
            r = client.get(
                f"/api/sessions/abc-123/history/{fname}/transcript.txt",
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("Alice: hello", r.text)
            self.assertIn("Bob: world", r.text)
            self.assertIn("--- Speaker airtime ---", r.text)
            # Filename has both the session id and the archive stem so
            # multiple archives from the same session don't collide on
            # disk for the user.
            cd = r.headers.get("content-disposition", "")
            self.assertIn("attachment", cd.lower())
            self.assertIn("abc-123", cd)
            self.assertIn("20260510T120000Z_abc1234", cd)
            self.assertTrue(cd.endswith('.txt"'), f"bad CD: {cd!r}")

    def test_history_txt_404_when_archive_missing(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get(
                "/api/sessions/abc-123/history/no-such-archive.json/transcript.txt",
            )
            self.assertEqual(r.status_code, 404)

    def test_history_txt_rejects_traversal_filenames(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            for bad in ["..foo.json", "....json", "foo..json"]:
                r = client.get(
                    f"/api/sessions/abc-123/history/{bad}/transcript.txt",
                )
                self.assertEqual(
                    r.status_code, 400, f"{bad} -> {r.status_code}",
                )

    def test_history_txt_500_when_archive_json_malformed(self):
        # transcript_history.read_archive returns whatever bytes are in
        # the file; the endpoint surfaces a 500 when the JSON is bad
        # rather than crashing or returning misleading content.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = _seed_session(data_dir / "sessions", "abc-123")
            from local_scribe.inspector import transcript_history
            h = transcript_history.ensure_history_dir(sd)
            (h / "broken.json").write_text("{not valid json")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.get(
                "/api/sessions/abc-123/history/broken.json/transcript.txt",
            )
            self.assertEqual(r.status_code, 500)


class TranscriptHistoryEndpointTests(unittest.TestCase):
    """Endpoint tests for the new /api/sessions/{id}/history surface."""

    def _seed_with_history(self, data_dir: Path, session_id: str,
                            *, archives: list[dict]) -> Path:
        from local_scribe.inspector import transcript_history
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
            r = client.request(
                "DELETE", f"/api/sessions/abc-123/history/{fname}",
                json=_DELETE_CONFIRM,
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["deleted"], fname)
            self.assertFalse((sd / ".local_scribe_history" / fname).exists())

    def test_history_file_delete_404_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            _seed_session(data_dir / "sessions", "abc-123")
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            r = client.request(
                "DELETE", "/api/sessions/abc-123/history/no-such.json",
                json=_DELETE_CONFIRM,
            )
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
                r = client.request(
                    "DELETE", f"/api/sessions/abc-123/history/{bad}",
                    json=_DELETE_CONFIRM,
                )
                self.assertEqual(
                    r.status_code, 400, f"{bad} -> {r.status_code}",
                )

    def test_history_file_delete_rejects_empty_body(self):
        # Defense-in-depth: typed-DELETE confirm body is required.
        # A bare ``curl -X DELETE`` (no body) must NOT remove the archive.
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
            self.assertEqual(r.status_code, 400)
            self.assertTrue(
                (sd / ".local_scribe_history" / fname).exists(),
                "archive must still exist after a rejected DELETE",
            )

    def test_history_file_delete_rejects_wrong_confirm_value(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            sd = self._seed_with_history(data_dir, "abc-123", archives=[
                {"transcripts": [], "local_scribe": {"asr_model": "x"}},
            ])
            cfg = _make_cfg(data_dir)
            client = TestClient(inspector_server.create_app(cfg))
            listed = client.get("/api/sessions/abc-123/history").json()
            fname = listed["entries"][0]["filename"]
            for bad in [
                {"confirm": "yes"},
                {"confirm": "delete"},
                {"confirm": "DELETE "},
                {},
            ]:
                with self.subTest(bad=bad):
                    r = client.request(
                        "DELETE", f"/api/sessions/abc-123/history/{fname}",
                        json=bad,
                    )
                    self.assertEqual(r.status_code, 400)
                    self.assertTrue(
                        (sd / ".local_scribe_history" / fname).exists(),
                        msg=f"archive destroyed on payload={bad!r}",
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
                        # Token-shaped value so audit() treats it as OK
                        # (the legacy "local" placeholder is now WARN).
                        "api_key": "ls_asr_" + "a" * 32,
                    }},
                },
            }))
            (data_dir / "store.json").write_text(json.dumps({
                "analytics": json.dumps({"Disabled": True}),
            }))
            # The /api/char/audit handler reloads config() each call
            # (so an out-of-band edit shows up immediately), which
            # means it ignores the cfg we pass into create_app() and
            # reads ~/.config/local_scribe/config.json instead. Point
            # that file at our temp data_dir for the duration of the
            # test so we don't accidentally audit the user's REAL
            # Char installation.
            tmp_cfg = data_dir / "config.json"
            raw = copy.deepcopy(config.DEFAULT_CONFIG)
            raw["char"]["data_dir"] = str(data_dir)
            tmp_cfg.write_text(json.dumps(raw, indent=2))
            real = config.DEFAULT_CONFIG_PATH
            config.DEFAULT_CONFIG_PATH = tmp_cfg
            # audit() now also reports firewall coverage; stub it out
            # so this test doesn't depend on the host's /etc/hosts.
            # The firewall integration itself is exercised by
            # ``tests/test_char_audit.py::FirewallIntegrationTests``.
            from unittest import mock
            from local_scribe.egress import firewall
            fake_fw = firewall.Status(
                installed=True,
                blocked_hostnames=[e.hostname for e in firewall.BLOCK_CATALOG
                                   if e.category in firewall.DEFAULT_ENABLED_CATEGORIES],
                coverage_by_category={
                    cat: {"blocked": 1, "expected": 1}
                    for cat in firewall.DEFAULT_ENABLED_CATEGORIES
                },
                missing_by_category={},
            )
            try:
                with mock.patch.object(firewall, "status", return_value=fake_fw):
                    cfg = _make_cfg(data_dir)
                    client = TestClient(inspector_server.create_app(cfg))
                    r = client.get("/api/char/audit")
                self.assertEqual(r.status_code, 200)
                d = r.json()
                self.assertTrue(d["settings_present"])
                self.assertGreaterEqual(d["summary"]["ok"], 4)
                self.assertEqual(d["summary"]["warn"], 0)
            finally:
                config.DEFAULT_CONFIG_PATH = real


class AuthTests(unittest.TestCase):
    """Inspector now uses per-service bearer tokens derived from the
    Keychain master key (see ``service_auth.py``). The tests inject a
    pre-built ``ServiceToken`` via ``create_app(..., token_holder=...)``
    so they don't need Touch ID / the real Keychain.

    Cookie / Authorization / X-API-Key / ?api_key all accepted (in
    that priority order); /api/health remains open as the liveness
    probe; / is open so the HTML SPA can load before the cookie has
    been set.
    """

    TEST_MK = b"\xab" * 32

    @classmethod
    def setUpClass(cls):
        # The rest of the inspector_server suite runs with
        # LOCAL_SCRIBE_DISABLE_AUTH=1 (set by run.sh / shell). To
        # exercise the real auth path, this class explicitly clears
        # the bypass.
        from local_scribe.security import service_auth
        cls._old_bypass = os.environ.pop(service_auth.BYPASS_ENV, None)

    @classmethod
    def tearDownClass(cls):
        from local_scribe.security import service_auth
        if cls._old_bypass is not None:
            os.environ[service_auth.BYPASS_ENV] = cls._old_bypass

    def _holder(self):
        from local_scribe.security import service_auth
        return service_auth.ServiceToken.from_master_key(self.TEST_MK, "inspector")

    def _client(self, td):
        # Auth is always required when a holder is injected, regardless
        # of the legacy auth_token config field. Pass auth_token=None
        # to keep the rest of the cfg shape happy.
        cfg = _make_cfg(Path(td), auth_token=None)
        return TestClient(
            inspector_server.create_app(cfg, token_holder=self._holder()),
        )

    def test_health_endpoint_remains_open(self):
        # /api/health stays open so monitoring / doctor can probe it
        # without a token.
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            self.assertEqual(client.get("/api/health").status_code, 200)

    def test_dev_mode_status_endpoint_remains_open(self):
        # /api/dev_mode/status MUST be reachable without a token —
        # the red banner has to render on the /auth cold-landing
        # view BEFORE the operator types their bearer token in.
        # See the middleware allowlist + the endpoint docstring.
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            r = client.get("/api/dev_mode/status")
            self.assertEqual(r.status_code, 200)
            payload = r.json()
            # Schema pin — front-end consumes these fields directly.
            self.assertIn("enabled", payload)
            self.assertIn("env_var", payload)
            self.assertIn("sip_state", payload)
            self.assertIn("severity", payload)
            self.assertEqual(payload["env_var"], "LOCAL_SCRIBE_DEV_MODE")

    def test_dev_mode_status_reports_enabled_when_env_set(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            with mock.patch.dict(
                os.environ, {"LOCAL_SCRIBE_DEV_MODE": "1"}
            ):
                r = client.get("/api/dev_mode/status")
            self.assertEqual(r.status_code, 200)
            payload = r.json()
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["severity"], "critical")

    def test_dev_mode_status_reports_disabled_when_env_unset(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            saved = os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)
            try:
                r = client.get("/api/dev_mode/status")
            finally:
                if saved is not None:
                    os.environ["LOCAL_SCRIBE_DEV_MODE"] = saved
            self.assertEqual(r.status_code, 200)
            payload = r.json()
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["severity"], "info")

    def test_api_sessions_401_without_auth(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            r = client.get("/api/sessions")
            self.assertEqual(r.status_code, 401)
            self.assertIn("WWW-Authenticate", r.headers)
            self.assertEqual(r.json()["detail"]["error"]["service"], "inspector")

    def test_api_sessions_401_with_wrong_bearer(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._client(td)
            r = client.get("/api/sessions",
                           headers={"Authorization": "Bearer ls_inspector_wrong"})
            self.assertEqual(r.status_code, 401)

    def test_api_sessions_200_with_correct_bearer(self):
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            r = client.get(
                "/api/sessions",
                headers={"Authorization": f"Bearer {holder.token}"},
            )
            self.assertEqual(r.status_code, 200)

    def test_api_sessions_accepts_x_api_key(self):
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            r = client.get("/api/sessions", headers={"X-API-Key": holder.token})
            self.assertEqual(r.status_code, 200)

    def test_api_sessions_accepts_cookie(self):
        # Browser auth flow: cookie set by /auth?token=... must let
        # subsequent /api/* requests through.
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            client.cookies.set("ls_inspector", holder.token)
            r = client.get("/api/sessions")
            self.assertEqual(r.status_code, 200)

    def test_auth_endpoint_sets_cookie_on_correct_token(self):
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            # follow_redirects=False so we can inspect the 302 + cookie.
            r = client.get(
                f"/auth?token={holder.token}",
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["location"], "/")
            self.assertIn("ls_inspector", r.cookies)
            self.assertEqual(r.cookies["ls_inspector"], holder.token)
            # HttpOnly + SameSite=Strict in raw Set-Cookie header.
            raw = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") \
                else [r.headers.get("set-cookie", "")]
            blob = " ".join(raw).lower()
            self.assertIn("httponly", blob)
            self.assertIn("samesite=strict", blob)

    def test_auth_endpoint_401_on_wrong_token(self):
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            r = client.get("/auth?token=wrong", follow_redirects=False)
            self.assertEqual(r.status_code, 401)

    def test_auth_endpoint_401_on_missing_token(self):
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            r = client.get("/auth", follow_redirects=False)
            self.assertEqual(r.status_code, 401)

    def test_bypass_env_disables_auth_entirely(self):
        # LOCAL_SCRIBE_DISABLE_AUTH=1 → no holder needed; all /api/* open.
        from local_scribe.security import service_auth
        old = os.environ.get(service_auth.BYPASS_ENV)
        os.environ[service_auth.BYPASS_ENV] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                cfg = _make_cfg(Path(td), auth_token=None)
                # No token_holder passed; lifespan would normally
                # unlock via Keychain but bypass should short-circuit.
                client = TestClient(inspector_server.create_app(cfg))
                self.assertEqual(client.get("/api/sessions").status_code, 200)
        finally:
            if old is None:
                os.environ.pop(service_auth.BYPASS_ENV, None)
            else:
                os.environ[service_auth.BYPASS_ENV] = old

    def test_index_unauthenticated_even_with_token(self):
        # The HTML page itself loads without auth so the browser can
        # render the bearer-token entry UI before the user's pasted it.
        holder = self._holder()
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td), auth_token=None)
            client = TestClient(
                inspector_server.create_app(cfg, token_holder=holder),
            )
            self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
