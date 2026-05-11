"""Unit tests for ``script_integrity.py``.

These cover the public surface (verify / enforce_or_die / format_banner)
against a *fake* git repo built inside a tempdir, so we never depend on
the state of the real working tree.
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


# Import the module fresh per-test so REPO_ROOT can be monkey-patched
# without leaking between tests.
from local_scribe.security import script_integrity  # noqa: E402


def _run(cmd: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"{cmd!r} failed in {cwd}: rc={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
    return proc.stdout


class _FakeRepo:
    """Builds a tiny git working tree with two tracked files
    (``service_auth.py`` + ``run.sh``) committed at HEAD."""

    def __init__(self, tmpdir: Path):
        self.root = tmpdir
        _run(["git", "init", "-q", "-b", "main"], cwd=self.root)
        _run(["git", "config", "user.email", "test@example.com"], cwd=self.root)
        _run(["git", "config", "user.name", "Test User"], cwd=self.root)
        _run(["git", "config", "commit.gpgsign", "false"], cwd=self.root)
        (self.root / "service_auth.py").write_text("X = 1\n")
        (self.root / "run.sh").write_text("#!/bin/bash\nset -eu\n")
        (self.root / "docs.md").write_text("# unrelated, should be skipped\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("# should be skipped\n")
        _run(["git", "add", "."], cwd=self.root)
        _run(["git", "commit", "-q", "-m", "seed"], cwd=self.root)

    def modify(self, rel: str, content: str) -> None:
        (self.root / rel).write_text(content)

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink()

    def add_untracked(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


class VerifyCleanTreeTests(unittest.TestCase):
    """A freshly-committed tree is clean by definition."""

    def test_clean_tree_reports_clean(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            rep = script_integrity.verify(repo.root)
            self.assertTrue(rep.clean, f"unexpected drifts: {rep.drifts}")
            self.assertEqual(rep.checked_count, 2,
                             msg="should verify service_auth.py + run.sh; "
                                 f"got {rep.checked_count}")
            self.assertTrue(rep.is_git_repo)
            self.assertIsNotNone(rep.head_short)

    def test_clean_tree_enforce_returns_normally(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            rep = script_integrity.verify(repo.root)
            # stream= captures the (would-be) banner so it doesn't
            # pollute test output.
            buf = io.StringIO()
            out = script_integrity.enforce_or_die(rep, stream=buf)
            self.assertIs(out, rep)
            self.assertEqual(buf.getvalue(), "",
                             msg="clean tree should print nothing")


class VerifyDriftDetectionTests(unittest.TestCase):
    """Modified / deleted / untracked files all surface as drifts."""

    def test_modified_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 2  # tampered\n")
            rep = script_integrity.verify(repo.root)
            self.assertFalse(rep.clean)
            self.assertEqual(len(rep.drifts), 1)
            d = rep.drifts[0]
            self.assertEqual(d.path, "service_auth.py")
            self.assertEqual(d.kind, "modified")
            self.assertIsNotNone(d.head_hash)
            self.assertIsNotNone(d.working_hash)
            self.assertNotEqual(d.head_hash, d.working_hash)

    def test_missing_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.delete("service_auth.py")
            rep = script_integrity.verify(repo.root)
            self.assertFalse(rep.clean)
            kinds = {d.path: d.kind for d in rep.drifts}
            self.assertEqual(kinds.get("service_auth.py"), "missing")

    def test_untracked_matching_pattern_is_flagged(self):
        # Dropping a new ``.py`` next to ``service_auth.py`` is the
        # classic "shim that imports first" attack — must show up
        # as drift.
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.add_untracked("auth_shim.py", "def make_token_dependency(*a,**k): pass\n")
            rep = script_integrity.verify(repo.root)
            self.assertFalse(rep.clean)
            paths = {d.path for d in rep.drifts}
            self.assertIn("auth_shim.py", paths)
            kind = next(d.kind for d in rep.drifts if d.path == "auth_shim.py")
            self.assertEqual(kind, "untracked")

    def test_excluded_dirs_do_not_count(self):
        # Files under ``tests/`` and the ``docs.md`` are both supposed
        # to be ignored by the gate.
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("tests/test_x.py", "# drift, but ignored\n")
            repo.modify("docs.md", "# drift, but not a verified pattern\n")
            rep = script_integrity.verify(repo.root)
            self.assertTrue(rep.clean,
                            f"unexpected drifts: {[d.path for d in rep.drifts]}")

    def test_untracked_in_excluded_dir_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.add_untracked("tests/test_new.py", "# new test\n")
            rep = script_integrity.verify(repo.root)
            self.assertTrue(rep.clean)


class EnforceOrDieTests(unittest.TestCase):
    """Override semantics + raised-when-dirty behaviour."""

    def setUp(self):
        # Snapshot + isolate the override env var.
        self._prior = os.environ.pop(script_integrity.ALLOW_DIRTY_ENV, None)

    def tearDown(self):
        if self._prior is None:
            os.environ.pop(script_integrity.ALLOW_DIRTY_ENV, None)
        else:
            os.environ[script_integrity.ALLOW_DIRTY_ENV] = self._prior

    def test_dirty_without_override_raises(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 99\n")
            rep = script_integrity.verify(repo.root)
            buf = io.StringIO()
            with self.assertRaises(script_integrity.ScriptIntegrityError):
                script_integrity.enforce_or_die(rep, stream=buf)
            # Banner must be on the stream so the operator sees it.
            self.assertIn("SCRIPT INTEGRITY DRIFT", buf.getvalue())
            self.assertIn("service_auth.py", buf.getvalue())

    def test_override_env_lets_dirty_pass(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 99\n")
            rep = script_integrity.verify(repo.root)
            os.environ[script_integrity.ALLOW_DIRTY_ENV] = "1"
            self.assertTrue(script_integrity.is_override_enabled())
            buf = io.StringIO()
            # Should NOT raise.
            out = script_integrity.enforce_or_die(rep, stream=buf)
            self.assertIs(out, rep)
            # Banner is still printed so the override doesn't hide drift.
            self.assertIn("SCRIPT INTEGRITY DRIFT", buf.getvalue())

    def test_override_one_liner_includes_head(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 99\n")
            rep = script_integrity.verify(repo.root)
            line = script_integrity.format_override_warning(rep, color=False)
            self.assertIn("DIRTY-OVERRIDE", line)
            self.assertIn(rep.head_short, line)


class NotARepoTests(unittest.TestCase):
    """A clone-from-tarball install (no ``.git``) skips the check
    with a note rather than failing — we don't have data to verify
    against."""

    def test_no_dot_git_skips_with_note(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "service_auth.py").write_text("X = 1\n")
            rep = script_integrity.verify(Path(td))
            self.assertTrue(rep.clean,
                            "non-git installs must not be marked dirty")
            self.assertFalse(rep.is_git_repo)
            self.assertIsNotNone(rep.note)
            self.assertIn("git", rep.note)


class CLIEntrypointTests(unittest.TestCase):
    """``python -m script_integrity`` is the surface ``run.sh`` calls."""

    def _run_cli(self, repo_root: Path, *args: str) -> tuple[int, str]:
        """Invoke the module's ``__main__`` against a specific repo
        by monkey-patching REPO_ROOT for the duration of the call."""
        old = script_integrity.REPO_ROOT
        script_integrity.REPO_ROOT = repo_root
        try:
            stdout = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = stdout
            try:
                rc = script_integrity._main(list(args))
            finally:
                sys.stdout = old_stdout
            return rc, stdout.getvalue()
        finally:
            script_integrity.REPO_ROOT = old

    def test_check_clean_returns_0(self):
        with tempfile.TemporaryDirectory() as td:
            _FakeRepo(Path(td))
            rc, _ = self._run_cli(Path(td), "--check")
            self.assertEqual(rc, 0)

    def test_check_dirty_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 9\n")
            rc, out = self._run_cli(Path(td), "--check")
            self.assertEqual(rc, 2)
            self.assertIn("SCRIPT INTEGRITY DRIFT", out)

    def test_json_machine_readable(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _FakeRepo(Path(td))
            repo.modify("service_auth.py", "X = 9\n")
            rc, out = self._run_cli(Path(td), "--json")
            self.assertEqual(rc, 2)
            import json
            doc = json.loads(out)
            self.assertEqual(doc["clean"], False)
            self.assertEqual(len(doc["drifts"]), 1)
            self.assertEqual(doc["drifts"][0]["path"], "service_auth.py")
            self.assertEqual(doc["drifts"][0]["kind"], "modified")

    def test_banner_prints_green_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            _FakeRepo(Path(td))
            rc, out = self._run_cli(Path(td), "--banner")
            self.assertEqual(rc, 0)
            self.assertIn("script-integrity OK", out)


if __name__ == "__main__":
    unittest.main()
