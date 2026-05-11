"""Tests for char_persist.py — the rescue path that sidecar-writes
``transcript.json`` into Char's session directory after our ASR
finishes, so the user gets a transcript even when Char's progressive
batch parser drops our words.

What we cover:
  * SHA256-based session matching (single match, no match, multi match,
    missing session dir).
  * Schema correctness: Char's persister
    (`apps/desktop/src/store/tinybase/persister/session/load/transcript.ts`)
    expects exactly these fields per transcript row.
  * Speaker hints: each word gets exactly one hint and the hints'
    speaker_index is correctly carried from word.speaker (or defaulted
    to 0).
  * Reuse of an existing transcript ``id`` so we don't fork rows that
    already exist on disk.
  * Path-traversal defense: a session dir that escapes the configured
    char_data_dir (via a symlink, say) must be rejected.
  * Atomic write: existing transcript.json is replaced wholesale, no
    half-written tmpfile leaks.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from local_scribe.char import char_persist


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Tmp(unittest.TestCase):
    """Each test gets its own ``hyprnote/`` data dir under a tempdir."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data_dir = Path(self._td.name) / "hyprnote"
        (self.data_dir / "sessions").mkdir(parents=True)

    def _seed_session(self, sid: str, audio_bytes: bytes) -> Path:
        sd = self.data_dir / "sessions" / sid
        sd.mkdir()
        (sd / "audio.mp3").write_bytes(audio_bytes)
        (sd / "_meta.json").write_text("{}")
        return sd


class FindSessionTests(_Tmp):
    def test_returns_session_dir_on_sha_match(self) -> None:
        audio = b"raw mp3 bytes here"
        sd = self._seed_session("aaaa", audio)

        match, sha = char_persist.find_session_dir_for_audio(
            self._write_temp(audio), self.data_dir,
        )

        self.assertEqual(match, sd)
        self.assertEqual(sha, _hash_bytes(audio))

    def test_returns_none_when_no_session_matches(self) -> None:
        self._seed_session("aaaa", b"audio A")
        self._seed_session("bbbb", b"audio B")

        match, sha = char_persist.find_session_dir_for_audio(
            self._write_temp(b"different audio"), self.data_dir,
        )

        self.assertIsNone(match)
        self.assertEqual(sha, _hash_bytes(b"different audio"))

    def test_returns_none_when_sessions_dir_missing(self) -> None:
        # data_dir/sessions exists from setUp; remove it.
        sessions = self.data_dir / "sessions"
        for d in sessions.iterdir():
            d.is_dir() and __import__("shutil").rmtree(d)
        sessions.rmdir()

        match, _ = char_persist.find_session_dir_for_audio(
            self._write_temp(b"foo"), self.data_dir,
        )
        self.assertIsNone(match)

    def test_picks_newest_when_multiple_sessions_share_audio(self) -> None:
        audio = b"shared"
        old = self._seed_session("old0", audio)
        new = self._seed_session("new0", audio)
        # Force timestamps so 'old' is older than 'new'.
        old_ts = 100.0
        new_ts = 200.0
        os.utime(old, (old_ts, old_ts))
        os.utime(new, (new_ts, new_ts))

        match, _ = char_persist.find_session_dir_for_audio(
            self._write_temp(audio), self.data_dir,
        )
        self.assertEqual(match, new)

    def _write_temp(self, data: bytes) -> Path:
        p = Path(self._td.name) / f"upload_{len(os.listdir(self._td.name))}.mp3"
        p.write_bytes(data)
        return p


class WriteTranscriptTests(_Tmp):
    def _basic_words(self) -> list[dict]:
        return [
            {"word": "Hello", "punctuated_word": "Hello,", "start": 0.0, "end": 0.5,
             "confidence": 0.9, "speaker": 0},
            {"word": "world", "punctuated_word": "world.", "start": 0.5, "end": 1.0,
             "confidence": 0.91, "speaker": 1},
        ]

    def test_writes_full_schema_for_matched_session(self) -> None:
        audio = b"audio-1"
        sd = self._seed_session("ses1", audio)
        upload = sd.parent.parent / "uploaded.mp3"
        upload.write_bytes(audio)

        result = char_persist.write_transcript_for_audio(
            upload, self.data_dir,
            words=self._basic_words(),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.session_id, "ses1")
        self.assertEqual(result.word_count, 2)
        self.assertEqual(result.speaker_count, 2)

        data = json.loads((sd / "transcript.json").read_text())
        # New writes include a top-level "local_scribe" metadata block
        # alongside Char's "transcripts" array. Char's parser ignores
        # unknown keys (verified against tinybase's session loader) so
        # this is invisible to Char's UI but powers the inspector's
        # history view.
        self.assertEqual(set(data.keys()), {"transcripts", "local_scribe"})
        self.assertIn("written_at_iso", data["local_scribe"])
        t = data["transcripts"][0]

        # Exact field set Char's persister expects.
        self.assertEqual(
            sorted(t.keys()),
            ["created_at", "id", "memo_md", "session_id",
             "speaker_hints", "started_at", "user_id", "words"],
        )
        self.assertEqual(t["session_id"], "ses1")
        # ms conversion is round(seconds*1000); validate both endpoints.
        self.assertEqual(t["words"][0]["start_ms"], 0)
        self.assertEqual(t["words"][0]["end_ms"], 500)
        self.assertEqual(t["words"][1]["start_ms"], 500)
        self.assertEqual(t["words"][1]["end_ms"], 1000)
        # Punctuated form takes precedence over plain `word`.
        self.assertEqual(t["words"][0]["text"], " Hello,")
        # 1:1 hint count, each hint references a real word_id.
        self.assertEqual(len(t["speaker_hints"]), 2)
        word_ids = {w["id"] for w in t["words"]}
        for h in t["speaker_hints"]:
            self.assertIn(h["word_id"], word_ids)
            self.assertEqual(h["type"], "provider_speaker_index")
            v = json.loads(h["value"])
            self.assertEqual(v["provider"], "openai")
        # Speaker indices propagate from word.speaker.
        idx0 = json.loads(t["speaker_hints"][0]["value"])["speaker_index"]
        idx1 = json.loads(t["speaker_hints"][1]["value"])["speaker_index"]
        self.assertEqual({idx0, idx1}, {0, 1})

    def test_word_confidences_threaded_into_local_scribe_metadata(self) -> None:
        # When the ASR pipeline attaches a `speaker_confidence` to each
        # word, char_persist should NOT add a field to Char's word
        # schema (it's strict). Instead the confidences live in a
        # parallel array under ``local_scribe.diarization.word_confidences``
        # so the inspector can join them back by position.
        audio = b"audio-conf"
        sd = self._seed_session("ses-conf", audio)
        upload = sd.parent.parent / "uploaded.mp3"
        upload.write_bytes(audio)

        words = [
            {"word": "Hi", "punctuated_word": "Hi,", "start": 0.0, "end": 0.3,
             "speaker": 0, "speaker_confidence": 0.91},
            {"word": "world", "punctuated_word": "world.", "start": 0.3, "end": 0.8,
             "speaker": 1, "speaker_confidence": 0.42},
        ]
        char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=words,
            metadata={"diarization": {"algorithm": "auto_silhouette"}},
        )
        data = json.loads((sd / "transcript.json").read_text())
        diar = data["local_scribe"]["diarization"]
        self.assertEqual(diar["word_confidences"], [0.91, 0.42])
        # Char's word schema must remain exactly the 5 documented fields.
        for w in data["transcripts"][0]["words"]:
            self.assertEqual(
                set(w.keys()),
                {"id", "text", "start_ms", "end_ms", "channel"},
            )

    def test_word_confidences_absent_when_no_word_has_one(self) -> None:
        # Skip the array entirely when nothing meaningful would land
        # in it — keeps the JSON small on diarize-skipped runs.
        audio = b"audio-noconf"
        sd = self._seed_session("ses-noconf", audio)
        upload = sd.parent.parent / "uploaded.mp3"
        upload.write_bytes(audio)
        char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=self._basic_words(),
        )
        data = json.loads((sd / "transcript.json").read_text())
        diar = data["local_scribe"].get("diarization") or {}
        self.assertNotIn("word_confidences", diar)

    def test_returns_none_when_no_matching_session(self) -> None:
        self._seed_session("ses1", b"a")
        upload = Path(self._td.name) / "noaudio.mp3"
        upload.write_bytes(b"different bytes")

        result = char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=self._basic_words(),
        )
        self.assertIsNone(result)

    def test_reuses_existing_transcript_id(self) -> None:
        audio = b"audio-1"
        sd = self._seed_session("ses1", audio)
        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(audio)

        # Pre-existing transcript.json with a known id.
        (sd / "transcript.json").write_text(json.dumps({
            "transcripts": [{
                "id": "fixed-uuid-1234",
                "session_id": "ses1",
                "words": [],
                "speaker_hints": [],
                "memo_md": "{\"type\":\"doc\",\"content\":[{\"type\":\"paragraph\"}]}",
                "user_id": "00000000-0000-0000-0000-000000000000",
                "created_at": "2026-01-01T00:00:00.000Z",
                "started_at": 0,
            }],
        }))

        char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=self._basic_words(),
        )

        d = json.loads((sd / "transcript.json").read_text())
        self.assertEqual(d["transcripts"][0]["id"], "fixed-uuid-1234")
        # But content is overwritten with the new words, not appended.
        self.assertEqual(len(d["transcripts"][0]["words"]), 2)

    def test_zero_words_results_in_empty_arrays_not_crash(self) -> None:
        audio = b"a"
        sd = self._seed_session("ses1", audio)
        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(audio)

        result = char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=[],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.word_count, 0)
        d = json.loads((sd / "transcript.json").read_text())
        self.assertEqual(d["transcripts"][0]["words"], [])
        self.assertEqual(d["transcripts"][0]["speaker_hints"], [])

    def test_no_temp_files_remain_after_write(self) -> None:
        audio = b"audio-1"
        sd = self._seed_session("ses1", audio)
        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(audio)

        char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=self._basic_words(),
        )

        leftovers = [p.name for p in sd.iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_uses_word_text_when_punctuated_missing(self) -> None:
        audio = b"a"
        sd = self._seed_session("ses1", audio)
        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(audio)

        char_persist.write_transcript_for_audio(
            upload, self.data_dir,
            words=[{"word": "hi", "start": 0.0, "end": 0.1, "speaker": 0}],
        )

        d = json.loads((sd / "transcript.json").read_text())
        self.assertEqual(d["transcripts"][0]["words"][0]["text"], " hi")

    def test_normalize_word_text_keeps_existing_leading_space(self) -> None:
        audio = b"a"
        sd = self._seed_session("ses1", audio)
        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(audio)

        char_persist.write_transcript_for_audio(
            upload, self.data_dir,
            words=[{"punctuated_word": " hi", "start": 0.0, "end": 0.1}],
        )

        d = json.loads((sd / "transcript.json").read_text())
        self.assertEqual(d["transcripts"][0]["words"][0]["text"], " hi")


class SpeakerCoercionTests(unittest.TestCase):
    """Regression tests for the Aug-2026 incident where redo-session on
    a long meeting crashed in char_persist with
    ``ValueError: invalid literal for int() with base 10: 'speaker_0'``.

    asr_server returns ``speaker="speaker_0"`` (string label) when the
    auto-skip path fires, but emits an integer index when real
    diarization runs. The sidecar writer must handle both shapes.
    """

    def test_int_passthrough(self) -> None:
        self.assertEqual(char_persist._coerce_speaker_index(0), 0)
        self.assertEqual(char_persist._coerce_speaker_index(3), 3)

    def test_none_returns_fallback(self) -> None:
        self.assertEqual(
            char_persist._coerce_speaker_index(None, fallback=7), 7,
        )

    def test_lowercase_speaker_underscore_label(self) -> None:
        self.assertEqual(char_persist._coerce_speaker_index("speaker_0"), 0)
        self.assertEqual(char_persist._coerce_speaker_index("speaker_1"), 1)
        self.assertEqual(char_persist._coerce_speaker_index("speaker_42"), 42)

    def test_uppercase_pyannote_label(self) -> None:
        self.assertEqual(char_persist._coerce_speaker_index("SPEAKER_00"), 0)
        self.assertEqual(char_persist._coerce_speaker_index("SPEAKER_15"), 15)

    def test_numeric_string(self) -> None:
        self.assertEqual(char_persist._coerce_speaker_index("3"), 3)

    def test_unknown_string_returns_fallback(self) -> None:
        # Forward-compat: a sherpa upgrade renames labels -> we'd
        # rather ship a one-speaker transcript than crash.
        self.assertEqual(
            char_persist._coerce_speaker_index("Alice", fallback=0), 0,
        )

    def test_string_label_round_trip_in_writer(self) -> None:
        # End-to-end: the same crash that broke redo-session must
        # not reappear. We pass words shaped exactly like asr_server's
        # auto-skip path emits them.
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        data_dir = Path(td.name) / "hyprnote"
        sd = data_dir / "sessions" / "ses1"
        sd.mkdir(parents=True)
        (sd / "audio.mp3").write_bytes(b"a")
        (sd / "_meta.json").write_text("{}")
        upload = Path(td.name) / "u.mp3"
        upload.write_bytes(b"a")

        char_persist.write_transcript_for_audio(
            upload, data_dir,
            words=[
                {"word": "hi", "start": 0.0, "end": 0.1, "speaker": "speaker_0"},
                {"word": "yo", "start": 0.1, "end": 0.2, "speaker": "speaker_0"},
            ],
        )

        d = json.loads((sd / "transcript.json").read_text())
        hints = d["transcripts"][0]["speaker_hints"]
        idx0 = json.loads(hints[0]["value"])["speaker_index"]
        idx1 = json.loads(hints[1]["value"])["speaker_index"]
        self.assertEqual(idx0, 0)
        self.assertEqual(idx1, 0)


class TraversalDefenseTests(_Tmp):
    def test_refuses_to_write_outside_char_data_dir(self) -> None:
        # Build an alternate dir we'd never want to write to and a
        # symlink inside sessions/ that points at it.
        elsewhere = Path(self._td.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "audio.mp3").write_bytes(b"a")

        link = self.data_dir / "sessions" / "evil"
        try:
            link.symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable on this platform")

        upload = Path(self._td.name) / "u.mp3"
        upload.write_bytes(b"a")

        # The traversal-checked write path must refuse: either by
        # returning None (no match accepted) or by skipping the write.
        result = char_persist.write_transcript_for_audio(
            upload, self.data_dir, words=[],
        )

        # Either we found no acceptable session, or we wrote inside
        # data_dir but never to ``elsewhere``.
        self.assertFalse((elsewhere / "transcript.json").exists())
        if result is not None:
            self.assertTrue(
                str(result.transcript_path).startswith(str(self.data_dir.resolve()))
            )


if __name__ == "__main__":
    unittest.main()
