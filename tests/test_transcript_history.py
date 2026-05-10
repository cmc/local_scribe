"""Tests for transcript_history.py — archive write, list, read, delete,
prune, filename validation, and the integration with
``char_persist.write_transcript_for_audio`` (archive-on-overwrite +
embedded ``local_scribe`` metadata)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import transcript_history


# ---------- helpers --------------------------------------------------


def _seed_transcript(
    session_dir: Path,
    *,
    body: dict | None = None,
    local_scribe_meta: dict | None = None,
) -> Path:
    """Write a transcript.json with the given Char-shaped body + optional
    embedded ``local_scribe`` metadata block (mirrors what the new
    char_persist write path emits)."""
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = body if body is not None else {
        "transcripts": [{
            "id": "t1",
            "session_id": session_dir.name,
            "words": [{"id": "w1", "text": "hi"}],
            "speaker_hints": [],
        }],
    }
    if local_scribe_meta is not None:
        payload["local_scribe"] = local_scribe_meta
    p = session_dir / "transcript.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


# ---------- filename validation -------------------------------------


class FilenameSafetyTests(unittest.TestCase):
    def test_safe_names_accepted(self):
        for name in [
            "20260510T134431Z_abc1234.json",
            "x.json",
            "a-b_c.json",
            "A.B.C.json",
        ]:
            self.assertTrue(transcript_history.is_safe_filename(name), name)

    def test_unsafe_names_rejected(self):
        # Traversal, separators, wrong extension, empty.
        for name in [
            "../etc/passwd",
            "..",
            "../foo.json",
            "foo/bar.json",
            "foo\\bar.json",
            "foo.txt",
            "",
            "no_extension",
            "a..json",  # ".." substring forbidden defense-in-depth
        ]:
            self.assertFalse(transcript_history.is_safe_filename(name), name)


# ---------- archive write -------------------------------------------


class ArchiveTests(unittest.TestCase):
    def test_archive_creates_history_dir_with_versioned_filename(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "session-a"
            _seed_transcript(sd, local_scribe_meta={
                "asr_model": "parakeet",
                "diarization": {"algorithm": "auto_silhouette", "num_speakers": 2},
            })
            archived = transcript_history.archive_existing_transcript(sd)
            self.assertIsNotNone(archived)
            self.assertTrue(archived.is_file())
            self.assertEqual(archived.parent.name, ".local_scribe_history")
            self.assertTrue(archived.name.endswith(".json"))
            # Filename format: <ISO-compact-timestamp>_<sha7>.json
            self.assertRegex(archived.name, r"^\d{8}T\d{6}Z_[a-f0-9]{7}\.json$")

    def test_archive_preserves_bytes_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            tpath = _seed_transcript(sd, local_scribe_meta={"asr_model": "whisper"})
            original = tpath.read_bytes()
            archived = transcript_history.archive_existing_transcript(sd)
            self.assertEqual(archived.read_bytes(), original)

    def test_archive_returns_none_when_no_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "empty-session"
            sd.mkdir()
            self.assertIsNone(
                transcript_history.archive_existing_transcript(sd)
            )

    def test_archive_idempotent_for_identical_bytes_same_second(self):
        # Same bytes archived twice in quick succession -> second call
        # should NOT create a duplicate file (timestamp + sha7 match).
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            _seed_transcript(sd)
            a1 = transcript_history.archive_existing_transcript(sd)
            a2 = transcript_history.archive_existing_transcript(sd)
            self.assertEqual(a1, a2)

    def test_prune_keeps_only_newest_N(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            hdir = transcript_history.ensure_history_dir(sd)
            # Seed 5 archives with distinct mtimes.
            for i in range(5):
                p = hdir / f"2026051{i}T120000Z_aaaaaaa.json"
                p.write_text(f'{{"local_scribe":{{"i":{i}}}}}')
                import os
                t = time.time() - (5 - i) * 10
                os.utime(p, (t, t))
            transcript_history._prune_old_archives(hdir, max_backups=2)
            kept = sorted(p.name for p in hdir.iterdir())
            self.assertEqual(len(kept), 2)
            # Newest two are i=3 and i=4 by name+mtime.
            self.assertIn("20260513T120000Z_aaaaaaa.json", kept)
            self.assertIn("20260514T120000Z_aaaaaaa.json", kept)


# ---------- listing -------------------------------------------------


class ListTests(unittest.TestCase):
    def test_list_returns_newest_first_with_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            hdir = transcript_history.ensure_history_dir(sd)
            import os
            # First archive (older): no local_scribe block.
            f1 = hdir / "20260510T120000Z_aaaaaaa.json"
            f1.write_text(json.dumps({"transcripts": []}))
            os.utime(f1, (time.time() - 100, time.time() - 100))
            # Second archive (newer): with metadata.
            f2 = hdir / "20260511T120000Z_bbbbbbb.json"
            f2.write_text(json.dumps({
                "transcripts": [],
                "local_scribe": {
                    "asr_model": "parakeet",
                    "diarization": {"algorithm": "auto_silhouette",
                                    "num_speakers": 4},
                },
            }))
            os.utime(f2, (time.time(), time.time()))

            entries = transcript_history.list_archives(sd)
            self.assertEqual(len(entries), 2)
            # Newest first.
            self.assertEqual(entries[0].filename, f2.name)
            self.assertEqual(entries[0].metadata["asr_model"], "parakeet")
            self.assertEqual(
                entries[0].metadata["diarization"]["num_speakers"], 4,
            )
            self.assertEqual(entries[1].filename, f1.name)
            self.assertEqual(entries[1].metadata, {})  # legacy file

    def test_list_empty_for_no_history_dir(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "no-history"
            sd.mkdir()
            self.assertEqual(transcript_history.list_archives(sd), [])

    def test_list_skips_non_json_files(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            hdir = transcript_history.ensure_history_dir(sd)
            (hdir / "garbage.txt").write_text("not json")
            (hdir / "valid.json").write_text("{}")
            entries = transcript_history.list_archives(sd)
            self.assertEqual([e.filename for e in entries], ["valid.json"])


# ---------- read / delete -------------------------------------------


class ReadDeleteTests(unittest.TestCase):
    def test_read_returns_bytes_for_valid_filename(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            hdir = transcript_history.ensure_history_dir(sd)
            f = hdir / "20260510T120000Z_aaaaaaa.json"
            f.write_text('{"hello":"world"}')
            data = transcript_history.read_archive(sd, f.name)
            self.assertEqual(data, b'{"hello":"world"}')

    def test_read_rejects_traversal_filename(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            transcript_history.ensure_history_dir(sd)
            for bad in ["../foo.json", "../../etc/passwd", "foo/bar.json"]:
                self.assertIsNone(
                    transcript_history.read_archive(sd, bad), bad,
                )

    def test_read_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            transcript_history.ensure_history_dir(sd)
            self.assertIsNone(
                transcript_history.read_archive(sd, "no-such.json"),
            )

    def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            hdir = transcript_history.ensure_history_dir(sd)
            f = hdir / "20260510T120000Z_aaaaaaa.json"
            f.write_text("{}")
            self.assertTrue(transcript_history.delete_archive(sd, f.name))
            self.assertFalse(f.exists())

    def test_delete_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            transcript_history.ensure_history_dir(sd)
            self.assertFalse(
                transcript_history.delete_archive(sd, "../../etc/passwd"),
            )

    def test_delete_missing_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            sd.mkdir()
            transcript_history.ensure_history_dir(sd)
            self.assertFalse(
                transcript_history.delete_archive(sd, "missing.json"),
            )


# ---------- restore (API surface; UI doesn't expose it yet) ----------


class RestoreTests(unittest.TestCase):
    def test_restore_overwrites_transcript_after_archiving_current(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "s"
            _seed_transcript(sd, body={"transcripts": [{"id": "newest"}]})
            # Stage an older archive.
            hdir = transcript_history.ensure_history_dir(sd)
            old = hdir / "20260101T000000Z_0000000.json"
            old.write_text(json.dumps({"transcripts": [{"id": "vintage"}]}))

            restored = transcript_history.restore_archive(sd, old.name)
            self.assertEqual(restored, sd / "transcript.json")
            now = json.loads((sd / "transcript.json").read_text())
            self.assertEqual(now["transcripts"][0]["id"], "vintage")
            # The previously-current transcript should now be backed up
            # under history (so the restore itself is reversible).
            entries = transcript_history.list_archives(sd)
            self.assertGreaterEqual(len(entries), 2)


# ---------- integration with char_persist ---------------------------


class CharPersistIntegrationTests(unittest.TestCase):
    """Smoke-test the wiring: when ``char_persist.write_transcript_for_audio``
    overwrites an existing transcript.json, the previous file must end up
    archived AND the new file must carry a ``local_scribe`` metadata block.
    """

    def _seed_char_layout(self, root: Path, audio_bytes: bytes):
        sessions = root / "sessions"
        session_id = "sess-1"
        sd = sessions / session_id
        sd.mkdir(parents=True)
        (sd / "audio.mp3").write_bytes(audio_bytes)
        return sd, session_id

    def test_overwrite_archives_previous_and_embeds_metadata(self):
        import char_persist
        with tempfile.TemporaryDirectory() as td:
            char_root = Path(td)
            audio = b"some audio bytes" * 100
            sd, _sid = self._seed_char_layout(char_root, audio)

            audio_input = Path(td) / "input.mp3"
            audio_input.write_bytes(audio)

            # First write -> no archive yet (no prior transcript.json).
            char_persist.write_transcript_for_audio(
                audio_input, char_root,
                words=[{"text": "first", "start": 0.0, "end": 0.1,
                        "speaker": "speaker_0"}],
                metadata={
                    "asr_model": "parakeet",
                    "diarization": {"algorithm": "auto_silhouette",
                                    "num_speakers": 1},
                },
            )
            self.assertTrue((sd / "transcript.json").is_file())
            self.assertEqual(transcript_history.list_archives(sd), [])
            # Embedded metadata is present.
            d = json.loads((sd / "transcript.json").read_text())
            self.assertEqual(d["local_scribe"]["asr_model"], "parakeet")

            # Second write -> the first should be archived.
            char_persist.write_transcript_for_audio(
                audio_input, char_root,
                words=[{"text": "second", "start": 0.0, "end": 0.1,
                        "speaker": "speaker_0"}],
                metadata={
                    "asr_model": "whisper-large-v3",
                    "diarization": {"algorithm": "manual_ahc",
                                    "num_speakers": 2},
                },
            )
            entries = transcript_history.list_archives(sd)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].metadata["asr_model"], "parakeet")
            # New transcript carries new metadata.
            d2 = json.loads((sd / "transcript.json").read_text())
            self.assertEqual(d2["local_scribe"]["asr_model"], "whisper-large-v3")


if __name__ == "__main__":
    unittest.main()
