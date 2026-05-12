"""Tests for ``run.sh``'s ``vault_relocation_gate`` helper.

Why this file exists
--------------------

The 2026-05-11 operator audit found 6 sessions of recorded data
sitting plaintext at ``~/Library/Application Support/hyprnote/`` —
sparse-bundle vault on disk, master key working, signed config OK,
but Char's data dir was a real directory rather than a symlink into
the mount. Nothing in the boot flow forced the operator's eye to
this state.

Fix: ``vault_relocation_gate`` runs from ``cmd_start`` between
``pinned_config_gate`` and ``char_integrity_gate``. It calls
``vault.char_data_relocated()`` and refuses to start with a loud
red banner pointing the operator at ``./run.sh vault unlock``.

This module pins the gate's contract:

  test_gate_skips_when_relocated
        char_data_relocated() == True → gate returns 0 silently.

  test_gate_refuses_when_plaintext
        char_data_relocated() == False → gate returns non-zero and
        the loud banner with "REFUSING TO START: Char data is
        PLAINTEXT on disk." appears on stdout/stderr.

  test_override_env_honored
        LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1 → gate returns 0
        but prints a yellow override warning so the bypass cannot
        be silent.

  test_skip_integrity_env_returns_zero
        LOCAL_SCRIBE_SKIP_INTEGRITY=1 → gate returns 0 silently
        (test seam used by other test modules).

  test_cmd_start_invokes_the_gate
        Static check on run.sh: cmd_start() calls
        vault_relocation_gate before char_integrity_gate. Pins the
        wiring so a future refactor can't silently drop the gate.

The gate uses ``$VENV_PY -c 'from local_scribe.security import
vault; ...'`` to query state; in tests we monkey-patch the python
invocation by providing a fake ``$VENV_PY`` that returns a hardcoded
exit code. This keeps the test fast (no Keychain, no hdiutil) while
still exercising the bash control flow + banner output verbatim.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"


def _fake_venv_py(tmp: Path, *, char_data_relocated: bool) -> Path:
    """Write a fake $VENV_PY that returns the requested exit code
    when the gate runs its ``from local_scribe.security import vault
    ...`` probe. Other invocations forward to the system python so
    the rest of run.sh's bash glue keeps working.
    """
    rc = 0 if char_data_relocated else 1
    fake = tmp / "fake_venv_py"
    fake.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Match the SPECIFIC probe the gate runs: ``-c '...
        # vault.char_data_relocated() ...'`` — any other invocation
        # delegates to the real python so unrelated run.sh code paths
        # still work.
        for arg in "$@"; do
          if [[ "$arg" == *char_data_relocated* ]]; then
            exit {rc}
          fi
        done
        exec /usr/bin/env python3 "$@"
        """))
    fake.chmod(0o755)
    return fake


def _invoke_gate(
    *,
    tmp: Path,
    char_data_relocated: bool,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    fake_py = _fake_venv_py(tmp, char_data_relocated=char_data_relocated)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp),
        "TERM": "dumb",
        # The gate honours LOCAL_SCRIBE_SKIP_INTEGRITY as a test seam;
        # we deliberately leave it unset by default so the gate's
        # actual control flow gets exercised. Tests that want the
        # skip path set it explicitly via extra_env.
    }
    if extra_env:
        env.update(extra_env)
    # Source run.sh, then call the gate function. We override $VENV_PY
    # AFTER sourcing because run.sh sets it itself based on $REPO.
    script = (
        f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
        f'VENV_PY="{fake_py}"\n'
        f'if vault_relocation_gate 2>&1; then rc=0; else rc=$?; fi\n'
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


class VaultRelocationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-vault-gate-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gate_skips_when_relocated(self) -> None:
        """char_data_relocated() == True → quiet success."""
        r = _invoke_gate(tmp=self.tmp, char_data_relocated=True)
        self.assertEqual(
            _exit_code_from(r.stdout), 0,
            msg=f"gate refused even though data is relocated:\n{r.stdout}\n{r.stderr}",
        )
        # And the loud banner must NOT appear.
        self.assertNotIn(
            "REFUSING TO START: Char data is PLAINTEXT", r.stdout,
        )

    def test_gate_refuses_when_plaintext(self) -> None:
        """char_data_relocated() == False → non-zero + loud banner."""
        r = _invoke_gate(tmp=self.tmp, char_data_relocated=False)
        rc = _exit_code_from(r.stdout)
        self.assertNotEqual(
            rc, 0,
            msg=f"gate let plaintext data through (rc=0):\n{r.stdout}\n{r.stderr}",
        )
        # The banner content is contractual: it must name the
        # remediation command and explain why the data is at risk.
        self.assertIn("REFUSING TO START", r.stdout)
        self.assertIn("PLAINTEXT", r.stdout)
        self.assertIn("./run.sh vault unlock", r.stdout)
        self.assertIn("Quit Char.app", r.stdout)

    def test_override_env_honored(self) -> None:
        """LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1 → success +
        loud yellow warning (NEVER silent — that would defeat the
        whole point of having a documented override)."""
        r = _invoke_gate(
            tmp=self.tmp, char_data_relocated=False,
            extra_env={"LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA": "1"},
        )
        self.assertEqual(
            _exit_code_from(r.stdout), 0,
            msg=f"override env didn't unblock the gate:\n{r.stdout}\n{r.stderr}",
        )
        # Override warning fires on stderr (the existing
        # LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG handler uses the same
        # pattern). It must NAME the env var so the operator can
        # tell what they're overriding.
        combined = r.stdout + r.stderr
        self.assertIn("LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA", combined)
        self.assertIn("PLAINTEXT", combined)

    def test_skip_integrity_env_returns_zero(self) -> None:
        """LOCAL_SCRIBE_SKIP_INTEGRITY=1 short-circuits the gate
        entirely (used by sibling tests that don't want gate noise)."""
        r = _invoke_gate(
            tmp=self.tmp, char_data_relocated=False,
            extra_env={"LOCAL_SCRIBE_SKIP_INTEGRITY": "1"},
        )
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("REFUSING TO START", r.stdout)


# ---------------------------------------------------------------------
# Static wiring check.


class CmdStartInvokesGateTests(unittest.TestCase):
    """``cmd_start`` must invoke ``vault_relocation_gate``.

    Without this static check, a future refactor could comment out
    the gate line and the runtime tests above would still pass
    (because they exercise the gate function in isolation, not the
    end-to-end start flow).
    """

    def test_cmd_start_calls_vault_relocation_gate(self) -> None:
        text = _RUN_SH.read_text()
        # Slice out cmd_start's body with a simple brace counter
        # (mirrors test_bootstrap_autosign.py's _extract_cmd_bootstrap_body
        # — the parser handles the bash-function-body shape we care
        # about).
        m = re.search(r"^cmd_start\(\)\s*\{\s*$", text, flags=re.M)
        self.assertIsNotNone(m, "cmd_start() not found in run.sh")
        assert m is not None
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body = text[start:i]
        self.assertRegex(
            body,
            r"vault_relocation_gate\s*\|\|\s*return\s+1",
            msg=(
                "cmd_start no longer invokes vault_relocation_gate. "
                "Without it, plaintext Char data sails through start. "
                "If you're intentionally removing the gate, also "
                "delete this test + the gate definition + the SECURITY.md "
                "section."
            ),
        )

    def test_vault_relocation_gate_runs_before_char_integrity_gate(self) -> None:
        """Ordering matters: char_integrity_gate fingerprints Char.app
        and refuses to launch on drift. If we'd refuse anyway because
        the data is plaintext, spend the cycles on the cheaper gate
        first."""
        text = _RUN_SH.read_text()
        m = re.search(r"^cmd_start\(\)\s*\{\s*$", text, flags=re.M)
        assert m is not None
        body = text[m.end():m.end() + 4000]   # plenty for the gate chain
        # Match the actual invocations (``gate_name || return 1``),
        # NOT bare mentions inside comments — the comment block above
        # the gate chain mentions char_integrity_gate by name for
        # documentation purposes and we don't want that to count.
        vault_m = re.search(r"vault_relocation_gate\s*\|\|", body)
        char_m = re.search(r"char_integrity_gate\s*\|\|", body)
        self.assertIsNotNone(vault_m, "vault_relocation_gate invocation missing")
        self.assertIsNotNone(char_m, "char_integrity_gate invocation missing")
        assert vault_m is not None and char_m is not None
        self.assertLess(
            vault_m.start(), char_m.start(),
            msg="vault_relocation_gate must precede char_integrity_gate.",
        )


if __name__ == "__main__":
    unittest.main()
