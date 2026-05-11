"""Tests for ``run.sh``'s ``ensure_config_json`` helper.

This helper is bootstrap stage 7. It uses a Python heredoc to call
``local_scribe.common.config.write_default_config_if_missing()``.

Coverage
--------

  test_writes_config_when_missing
        Fresh ~/.config/local_scribe with no config.json: the helper
        creates one. Idempotent: a second call leaves it untouched.

  test_handles_existing_config_gracefully
        Pre-existing config.json: the helper logs "already exists"
        and returns 0 without overwriting it.

  test_uses_post_reorg_import_path
        Regression guard. The original heredoc used the pre-reorg
        ``from config import ...`` path; after the 2026-05-11 reorg
        that errored at runtime. We assert the new path appears
        verbatim in run.sh so a future refactor can't silently
        regress it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"


def _invoke(fn: str, *, home: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TERM": "dumb",
        "LOCAL_SCRIBE_DEV_MODE": "1",
    }
    if extra_env:
        env.update(extra_env)
    script = (
        f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
        f'if {fn} 2>&1; then rc=0; else rc=$?; fi\n'
        f'echo "__rc=$rc"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_REPO),
    )


def _exit_code_from(output: str) -> int:
    for line in reversed(output.splitlines()):
        if line.startswith("__rc="):
            return int(line[len("__rc="):])
    raise AssertionError(f"no __rc=N marker in output:\n{output}")


class EnsureConfigJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="ls-cfgjson-home-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def _config_path(self) -> Path:
        return self.home / ".config" / "local_scribe" / "config.json"

    def test_writes_config_when_missing(self) -> None:
        cfg = self._config_path()
        self.assertFalse(cfg.exists())
        r = _invoke("ensure_config_json", home=self.home)
        self.assertEqual(_exit_code_from(r.stdout), 0, msg=r.stdout + r.stderr)
        self.assertTrue(
            cfg.exists(),
            msg=f"config.json not created at {cfg}\nstdout:\n{r.stdout}",
        )
        # Sanity: it must be valid JSON.
        import json
        with cfg.open() as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)

    def test_idempotent_second_call_does_not_modify_existing(self) -> None:
        # First call writes the default.
        r1 = _invoke("ensure_config_json", home=self.home)
        self.assertEqual(_exit_code_from(r1.stdout), 0, msg=r1.stdout)
        cfg = self._config_path()
        first = cfg.read_text()
        first_mtime = cfg.stat().st_mtime_ns

        # Second call must NOT overwrite (we'd lose any operator edits).
        r2 = _invoke("ensure_config_json", home=self.home)
        self.assertEqual(_exit_code_from(r2.stdout), 0, msg=r2.stdout)
        second = cfg.read_text()
        self.assertEqual(first, second, "second call rewrote config.json")
        self.assertEqual(
            first_mtime,
            cfg.stat().st_mtime_ns,
            "second call touched config.json's mtime",
        )
        # Verbose-mode marker on the "already exists" path:
        self.assertIn("already exists", r2.stdout)

    def test_handles_operator_edits(self) -> None:
        """If the operator has hand-edited config.json, the helper
        must leave it strictly alone — we never overwrite operator
        intent."""
        cfg = self._config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"asr_backend": "whisper", "llm_port": 5555}\n')
        original = cfg.read_text()
        r = _invoke("ensure_config_json", home=self.home)
        self.assertEqual(_exit_code_from(r.stdout), 0, msg=r.stdout)
        self.assertEqual(cfg.read_text(), original)

    def test_uses_post_reorg_import_path(self) -> None:
        """Static regression guard. The heredoc must import from the
        new ``local_scribe.common.config`` path. The grandfathered
        flat-module ``from config import ...`` path failed at
        runtime after the 2026-05-11 reorg."""
        text = _RUN_SH.read_text()
        self.assertIn(
            "from local_scribe.common.config import",
            text,
            msg="run.sh must use the post-reorg import path",
        )
        # And the old path must NOT be present.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("from config import") or stripped == "import config":
                self.fail(
                    f"stale pre-reorg import found in run.sh:\n  {line!r}"
                )


if __name__ == "__main__":
    unittest.main()
