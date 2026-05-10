"""Tests for char_settings_writer.py.

This is a thin patcher but its threat-model property — "the ASR
token never appears on argv" — is load-bearing for the Option C
hardening pass, so we exercise both the API and the CLI surface
(``python -m char_settings_writer`` with stdin) to lock in the
contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import char_settings_writer


class PatchSettingsTests(unittest.TestCase):
    def test_patches_four_keys_and_leaves_others_intact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "settings.json"
            p.write_text(json.dumps({
                "ai": {"existing_top_level": True},
                "general": {"theme": "dark"},
                "calendars": [{"id": 1}],
            }))
            char_settings_writer.patch_settings(p, "8000", "ls_asr_test")
            after = json.loads(p.read_text())
            self.assertEqual(after["ai"]["current_stt_provider"], "openai")
            self.assertEqual(after["ai"]["current_stt_model"], "gpt-4o-transcribe")
            self.assertEqual(
                after["ai"]["stt"]["openai"]["base_url"],
                "http://127.0.0.1:8000/v1",
            )
            self.assertEqual(after["ai"]["stt"]["openai"]["api_key"], "ls_asr_test")
            # Untouched sections survive.
            self.assertTrue(after["ai"]["existing_top_level"])
            self.assertEqual(after["general"]["theme"], "dark")
            self.assertEqual(after["calendars"], [{"id": 1}])

    def test_creates_nested_keys_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "settings.json"
            p.write_text("{}")
            char_settings_writer.patch_settings(p, "9000", "ls_asr_xyz")
            d = json.loads(p.read_text())
            self.assertEqual(
                d["ai"]["stt"]["openai"]["base_url"],
                "http://127.0.0.1:9000/v1",
            )


class CLITests(unittest.TestCase):
    """``python -m char_settings_writer`` over stdin — the contract
    ``run.sh`` depends on. We invoke a real subprocess so the test
    catches any future regression where the module-level glue stops
    parsing stdin."""

    def _run(self, stdin_text: str, *, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "char_settings_writer"],
            input=stdin_text,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            timeout=10,
        )

    def test_round_trip_via_stdin(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sp = tdp / "settings.json"
            sp.write_text('{"ai": {"keep": "me"}}')
            stdin_text = f"{sp}\n8000\nls_asr_secret_token_must_not_leak\n"
            proc = self._run(stdin_text, cwd=tdp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            d = json.loads(sp.read_text())
            self.assertEqual(
                d["ai"]["stt"]["openai"]["api_key"],
                "ls_asr_secret_token_must_not_leak",
            )
            # Token must not appear in stdout/stderr either — the
            # writer is silent on success.
            self.assertEqual(proc.stdout, "")
            self.assertNotIn("ls_asr_secret_token_must_not_leak", proc.stderr)

    def test_rejects_short_stdin(self):
        with tempfile.TemporaryDirectory() as td:
            proc = self._run("only-one-line\n", cwd=Path(td))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("3 stdin lines", proc.stderr)

    def test_rejects_missing_settings_file(self):
        with tempfile.TemporaryDirectory() as td:
            stdin_text = f"{td}/nope.json\n8000\nls_asr_x\n"
            proc = self._run(stdin_text, cwd=Path(td))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("not found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
