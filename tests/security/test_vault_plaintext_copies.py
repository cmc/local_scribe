"""Tests for :func:`local_scribe.security.vault.find_plaintext_char_data_copies`
and the companion :func:`vault.delete_plaintext_copy` guided cleanup.

Context
-------

The 2026-05-11 operator audit ("please confirm the only copy of my
char data is inside the sparse mount and add a complete security
check verification view to the 'char audit' tab in the UI") surfaced
**three** plaintext leftover copies on the live host:

  1. ``~/Library/Application Support/hyprnote.pre_vault_backup.<ts>``
     — written by ``relocate_char_data()`` itself as the safety
     rollback target.
  2. ``~/local_scribe_pre_arch_backup_<ts>/hyprnote`` — older
     bootstrap rev's package-reorg backup.
  3. ``~/.cache/local_scribe-demo/hyprnote`` — synthetic demo seed
     from ``tools/seed_demo.py``.

Together they were 5+ GiB of plaintext Char data despite a working
encrypted vault, master key, and symlink. Nothing surfaced this
state to the operator.

This file pins the scanner that surfaces the leftovers + the
deleter that cleans them up. Every test runs against a synthetic
``$HOME`` (via env-var overrides on :data:`vault.VAULT_BUNDLE_ENV` /
``VAULT_MOUNT_ENV`` / ``CHAR_DATA_DIR_ENV`` plus a ``$HOME`` patch)
so we never touch the operator's real Library / .cache / home dir
contents.

What this file pins
-------------------

* **Scanner detection** for each documented leftover pattern.
* **Scanner exclusions** for symlinks (the live vault symlink target
  must not be returned) and "looks like Char data" heuristics
  (empty directories, unrelated dirs that happen to match the
  glob, …).
* **Sorting + sizing** — findings are sorted by descending size so
  the UI presents the biggest footgun first.
* **Deleter safety** — ``delete_plaintext_copy`` refuses any path
  not currently returned by the scanner (no arbitrary recursive
  rm).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from local_scribe.security import vault


# ---------------------------------------------------------------------------
# Test harness — sandbox HOME + vault paths


class _LeftoverSandbox(unittest.TestCase):
    """Base class that wires every path the scanner inspects into a
    throwaway ``$HOME``. ``vault._paths`` is reloaded from env so
    the canonical char-data dir resolves under the sandbox too."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls_leftover_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Build a fake $HOME tree.
        self.home = self.tmp / "home"
        (self.home / "Library" / "Application Support").mkdir(parents=True)
        (self.home / ".cache" / "local_scribe-demo").mkdir(parents=True)
        self.bundle = self.home / "Library" / "Application Support" \
                      / "local_scribe-vault.sparsebundle"
        self.mount = self.home / "Library" / "Application Support" \
                     / "local_scribe-vault"
        self.char_data = self.home / "Library" / "Application Support" \
                         / "hyprnote"

        # Patch env so vault uses our sandbox; the scanner's patterns
        # are anchored to ``Path("~/...").expanduser()`` which reads
        # ``$HOME`` so we patch that too.
        self._old_env: dict[str, str | None] = {}
        for k, v in {
            "HOME": str(self.home),
            vault.VAULT_BUNDLE_ENV: str(self.bundle),
            vault.VAULT_MOUNT_ENV: str(self.mount),
            vault.CHAR_DATA_DIR_ENV: str(self.char_data),
        }.items():
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v
        vault.reload_paths()
        # Reload at teardown too so subsequent tests see the real
        # env.
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        vault.reload_paths()

    def _make_char_dir(self, path: Path, *,
                       sessions: int = 0,
                       audio_files: int = 0,
                       app_db: bool = True) -> None:
        """Create a directory that ``_looks_like_char_data`` will
        accept: ``app.db`` + a ``sessions/`` subtree with N sessions
        each carrying an audio.mp3."""
        path.mkdir(parents=True, exist_ok=True)
        if app_db:
            (path / "app.db").write_bytes(b"x" * 32)
        if sessions:
            sdir = path / "sessions"
            sdir.mkdir(parents=True, exist_ok=True)
            for i in range(sessions):
                s = sdir / f"sess-{i:03d}"
                s.mkdir(parents=True, exist_ok=True)
                (s / "transcript.json").write_text("{}")
                if i < audio_files:
                    (s / "audio.mp3").write_bytes(b"\xff" * 1024)


# ---------------------------------------------------------------------------
# Scanner


class ScannerDetectionTests(_LeftoverSandbox):
    def test_finds_pre_vault_backup(self) -> None:
        # Pattern: ~/Library/Application Support/hyprnote.pre_vault_backup.<ts>
        backup = (self.home / "Library" / "Application Support"
                  / "hyprnote.pre_vault_backup.20260511-184408")
        self._make_char_dir(backup, sessions=3, audio_files=2)
        findings = vault.find_plaintext_char_data_copies()
        kinds = {f.kind for f in findings}
        paths = {f.path for f in findings}
        self.assertIn(vault.LeftoverKind.PRE_VAULT_BACKUP, kinds)
        self.assertIn(backup, paths)

    def test_finds_pre_arch_backup_with_hyprnote_child(self) -> None:
        # Pattern: ~/local_scribe_pre_*backup*/hyprnote
        wrapper = self.home / "local_scribe_pre_arch_backup_20260510_155302"
        inner = wrapper / "hyprnote"
        self._make_char_dir(inner, sessions=5, audio_files=5)
        findings = vault.find_plaintext_char_data_copies()
        paths = {f.path for f in findings}
        self.assertIn(inner, paths)
        finding = next(f for f in findings if f.path == inner)
        self.assertEqual(finding.kind, vault.LeftoverKind.PRE_ARCH_BACKUP)
        self.assertEqual(finding.session_count, 5)

    def test_finds_demo_cache(self) -> None:
        # Pattern: ~/.cache/local_scribe-demo/hyprnote
        demo = self.home / ".cache" / "local_scribe-demo" / "hyprnote"
        self._make_char_dir(demo, sessions=5)
        findings = vault.find_plaintext_char_data_copies()
        kinds = {f.kind for f in findings}
        self.assertIn(vault.LeftoverKind.DEMO_CACHE, kinds)

    def test_finds_hyprnote_bak_variant(self) -> None:
        # Belt-and-braces: manual `.bak`-style copies the user might
        # have made before running our auto-relocate.
        bak = (self.home / "Library" / "Application Support"
               / "hyprnote.bak.20260101")
        self._make_char_dir(bak, sessions=1)
        findings = vault.find_plaintext_char_data_copies()
        self.assertIn(bak, {f.path for f in findings})

    def test_ignores_canonical_symlink_into_vault(self) -> None:
        """The live ``~/Library/Application Support/hyprnote``
        symlink that points INTO the vault must never be reported as
        a plaintext leftover -- that's the legit at-rest-encrypted
        path."""
        inside = self.mount / "hyprnote"
        self._make_char_dir(inside, sessions=2)
        # Create the canonical symlink.
        self.char_data.symlink_to(inside)
        findings = vault.find_plaintext_char_data_copies()
        for f in findings:
            self.assertNotEqual(f.path, self.char_data,
                                f"canonical symlink reported as leftover: {f}")

    def test_ignores_empty_directories(self) -> None:
        """An empty `hyprnote.pre_vault_backup.*` dir (no app.db, no
        sessions) is not Char data; don't report it."""
        empty = (self.home / "Library" / "Application Support"
                 / "hyprnote.pre_vault_backup.20260511-000000")
        empty.mkdir(parents=True)
        findings = vault.find_plaintext_char_data_copies()
        self.assertNotIn(empty, {f.path for f in findings})

    def test_ignores_unrelated_glob_matches(self) -> None:
        """A directory named like our pattern but missing Char's
        signature files (app.db / sessions/) must not be reported."""
        bogus = (self.home / "Library" / "Application Support"
                 / "hyprnote.pre_vault_backup.fake")
        bogus.mkdir(parents=True)
        (bogus / "README.md").write_text("not char data")
        findings = vault.find_plaintext_char_data_copies()
        self.assertNotIn(bogus, {f.path for f in findings})

    def test_empty_filesystem_returns_empty_list(self) -> None:
        """Fresh sandbox, no Char data anywhere = no findings."""
        findings = vault.find_plaintext_char_data_copies()
        self.assertEqual(findings, [])

    def test_findings_sorted_by_descending_size(self) -> None:
        small = (self.home / "Library" / "Application Support"
                 / "hyprnote.pre_vault_backup.small")
        big = (self.home / "Library" / "Application Support"
               / "hyprnote.pre_vault_backup.big")
        self._make_char_dir(small, sessions=1, audio_files=0)
        # ``audio_files=N`` writes 1 KiB per file; big has 10 large
        # audio blobs to dwarf the small one.
        big.mkdir(parents=True)
        (big / "app.db").write_bytes(b"x" * 32)
        big_sessions = big / "sessions"
        big_sessions.mkdir(parents=True)
        for i in range(5):
            s = big_sessions / f"sess-{i}"
            s.mkdir(parents=True)
            (s / "audio.mp3").write_bytes(b"\xff" * (1024 * 1024))
            (s / "transcript.json").write_text("{}")
        findings = vault.find_plaintext_char_data_copies()
        self.assertGreaterEqual(len(findings), 2)
        # First should be the bigger one.
        self.assertEqual(findings[0].path, big,
                         f"sort order wrong: {[(f.path, f.size_bytes) for f in findings]}")

    def test_finding_has_session_and_audio_counts(self) -> None:
        backup = (self.home / "Library" / "Application Support"
                  / "hyprnote.pre_vault_backup.audited")
        self._make_char_dir(backup, sessions=4, audio_files=3)
        findings = vault.find_plaintext_char_data_copies()
        f = next(fnd for fnd in findings if fnd.path == backup)
        self.assertEqual(f.session_count, 4)
        self.assertEqual(f.audio_count, 3)
        self.assertGreater(f.size_bytes, 0)
        self.assertGreater(f.mtime, 0)


# ---------------------------------------------------------------------------
# Deleter


class DeleterSafetyTests(_LeftoverSandbox):
    def test_deletes_a_leftover(self) -> None:
        backup = (self.home / "Library" / "Application Support"
                  / "hyprnote.pre_vault_backup.20260101-000000")
        self._make_char_dir(backup, sessions=1)
        self.assertTrue(backup.is_dir())
        vault.delete_plaintext_copy(backup)
        self.assertFalse(backup.exists(),
                         f"deleter did not remove {backup}")

    def test_refuses_unknown_path(self) -> None:
        """A path that's NOT currently a detected leftover must be
        refused. This is what stops a stolen-bearer-token replay
        from being able to recursively rm an arbitrary directory."""
        evil = self.home / "documents" / "important"
        evil.mkdir(parents=True)
        (evil / "doc.txt").write_text("don't delete me")
        with self.assertRaises(vault.VaultError) as ctx:
            vault.delete_plaintext_copy(evil)
        self.assertIn("not in the current leftover set", str(ctx.exception))
        self.assertTrue(evil.is_dir(),
                        "refused path was deleted anyway")

    def test_refuses_canonical_symlink_target(self) -> None:
        """The live vault symlink target must not be deletable via
        this path. (It would obliterate the operator's encrypted
        Char data — the whole point of the at-rest encryption.)"""
        inside = self.mount / "hyprnote"
        self._make_char_dir(inside, sessions=2)
        self.char_data.symlink_to(inside)
        with self.assertRaises(vault.VaultError):
            vault.delete_plaintext_copy(self.char_data)
        self.assertTrue(inside.is_dir(),
                        "vault contents got deleted via canonical path")

    def test_refuses_relative_path(self) -> None:
        # ``delete_plaintext_copy`` resolves the path first, so a
        # relative-cwd path would resolve to the wrong absolute
        # location and (correctly) not be in the leftover set.
        with self.assertRaises(vault.VaultError):
            vault.delete_plaintext_copy(Path("relative/garbage"))


if __name__ == "__main__":
    unittest.main()
