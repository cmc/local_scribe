"""Tests for ``run.sh``'s ``vault_auto_unlock_gate`` helper.

Why this file exists
--------------------

The 2026-05-11 lock-on-stop change made ``./run.sh stop`` dismount
the encrypted vault. That created a new failure mode: on the NEXT
``./run.sh start``, the canonical Char data dir
(``~/Library/Application Support/hyprnote``) is still a symlink into
the (now-unmounted) mount path. Any service that follows the
symlink hits ENOENT and dies with a cryptic message.

Fix: ``vault_auto_unlock_gate`` runs from ``cmd_start`` BETWEEN
``vault_relocation_gate`` (which confirms the symlink-into-mount
exists) and ``char_integrity_gate``. If the vault is on disk but
unmounted, it shells out to ``vault_unlock unlock --no-relocate``
which prompts for Touch ID + YubiKey and mounts the volume. If the
operator cancels the prompt, the gate refuses to start with a loud
red banner pointing at the recovery command.

The gate is a no-op when:

* The vault is already mounted (idempotent re-start).
* No vault exists on disk (``vault_relocation_gate`` would already
  have refused — defense in depth).
* ``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1`` (operator explicitly
  opted out of the vault relationship).
* ``LOCAL_SCRIBE_SKIP_INTEGRITY=1`` (test seam).
* The venv isn't built yet (mid-bootstrap).

These tests pin each branch + a static check that ``cmd_start``
actually invokes the gate in the correct position.
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


def _fake_venv_py(
    tmp: Path,
    *,
    exists: bool,
    mounted: bool,
    unlock_succeeds: bool,
) -> Path:
    """Fake ``$VENV_PY``:

    1.  ``-c '... vault.exists() / vault.is_mounted() ...'`` →
        0 (mounted) / 1 (exists but not mounted) / 2 (no vault).

    2.  ``-m local_scribe.security.vault_unlock unlock ...`` →
        0 on success, 1 on failure (e.g. operator cancelled Touch ID).

    Other invocations fall through to the system python.
    """
    if not exists:
        probe_rc = 2
    elif not mounted:
        probe_rc = 1
    else:
        probe_rc = 0
    unlock_rc = 0 if unlock_succeeds else 1
    fake = tmp / "fake_venv_py"
    fake.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for arg in "$@"; do
          if [[ "$arg" == *"vault.exists()"* ]]; then
            exit {probe_rc}
          fi
        done
        if [[ "$1" == "-m" && "$2" == "local_scribe.security.vault_unlock" \
              && "$3" == "unlock" ]]; then
          exit {unlock_rc}
        fi
        exec /usr/bin/env python3 "$@"
        """))
    fake.chmod(0o755)
    return fake


def _invoke(
    *,
    tmp: Path,
    exists: bool,
    mounted: bool,
    unlock_succeeds: bool = True,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    fake_py = _fake_venv_py(
        tmp,
        exists=exists,
        mounted=mounted,
        unlock_succeeds=unlock_succeeds,
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp),
        "TERM": "dumb",
    }
    if extra_env:
        env.update(extra_env)
    script = (
        f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
        f'VENV_PY="{fake_py}"\n'
        f'if vault_auto_unlock_gate 2>&1; then rc=0; else rc=$?; fi\n'
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


class VaultAutoUnlockGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-vault-auto-unlock-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_already_mounted_is_silent_success(self) -> None:
        """Idempotent re-start of an already-mounted vault. We must
        NOT prompt for Touch ID -- this gate fires on every start
        and re-prompting when nothing changed would be insufferable."""
        r = _invoke(tmp=self.tmp, exists=True, mounted=True)
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("Touch ID", r.stdout)
        self.assertNotIn("REFUSING TO START", r.stdout)

    def test_no_vault_is_silent_success(self) -> None:
        """Defense in depth: ``vault_relocation_gate`` should already
        have refused when no vault exists, but if it didn't we just
        return success and let downstream gates surface the issue."""
        r = _invoke(tmp=self.tmp, exists=False, mounted=False)
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("REFUSING TO START", r.stdout)

    def test_not_mounted_triggers_unlock(self) -> None:
        """The whole point: vault exists on disk but isn't mounted →
        prompt + mount. We assert on the operator-facing banner so
        the user gets a clear "here's why I'm asking for Touch ID"
        before the prompt fires."""
        r = _invoke(tmp=self.tmp, exists=True, mounted=False, unlock_succeeds=True)
        rc = _exit_code_from(r.stdout)
        self.assertEqual(rc, 0, msg=f"unlock should succeed (rc={rc}):\n{r.stdout}\n{r.stderr}")
        self.assertIn("Vault is locked", r.stdout)
        self.assertIn("Touch ID + YubiKey", r.stdout)

    def test_unlock_failure_refuses_with_loud_banner(self) -> None:
        """Operator cancels Touch ID / no YubiKey / wrong master key
        → we MUST refuse to start. Launching services against a
        dangling symlink would corrupt the symlink the moment Char
        tries to read from it."""
        r = _invoke(tmp=self.tmp, exists=True, mounted=False, unlock_succeeds=False)
        rc = _exit_code_from(r.stdout)
        self.assertNotEqual(rc, 0, msg=f"unlock failure must fail the gate (rc={rc}):\n{r.stdout}")
        self.assertIn("REFUSING TO START", r.stdout)
        self.assertIn("./run.sh vault unlock", r.stdout)

    def test_allow_plaintext_env_short_circuits(self) -> None:
        """``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1``: there's no
        vault relationship to manage. Don't trip over an unmounted
        bundle in this mode."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=False, unlock_succeeds=False,
            extra_env={"LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA": "1"},
        )
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("REFUSING TO START", r.stdout)
        self.assertNotIn("Touch ID", r.stdout)

    def test_skip_integrity_env_short_circuits(self) -> None:
        """``LOCAL_SCRIBE_SKIP_INTEGRITY=1`` is the cross-test escape
        hatch; the gate must honour it the same way every other
        ``*_gate`` does."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=False, unlock_succeeds=False,
            extra_env={"LOCAL_SCRIBE_SKIP_INTEGRITY": "1"},
        )
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("REFUSING TO START", r.stdout)


# ---------------------------------------------------------------------
# Static wiring check.


class CmdStartInvokesGateTests(unittest.TestCase):
    """``cmd_start`` must invoke ``vault_auto_unlock_gate`` AFTER
    ``vault_relocation_gate`` and BEFORE ``char_integrity_gate``."""

    def _cmd_start_body(self) -> str:
        text = _RUN_SH.read_text()
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
        return text[start:i]

    def test_cmd_start_calls_vault_auto_unlock_gate(self) -> None:
        body = self._cmd_start_body()
        self.assertRegex(
            body,
            r"vault_auto_unlock_gate\s*\|\|\s*return\s+1",
            msg=(
                "cmd_start() no longer invokes vault_auto_unlock_gate. "
                "Without it, restarting after `./run.sh stop` (which "
                "dismounts the vault) launches services against a "
                "dangling symlink. If you're intentionally removing the "
                "gate, also drop tests/bootstrap/test_vault_auto_unlock_gate.py "
                "and the gate definition + the lock-on-stop hook."
            ),
        )

    def test_gate_order_relocation_then_auto_unlock_then_char(self) -> None:
        body = self._cmd_start_body()
        relocate_m = re.search(r"vault_relocation_gate\s*\|\|", body)
        unlock_m = re.search(r"vault_auto_unlock_gate\s*\|\|", body)
        char_m = re.search(r"char_integrity_gate\s*\|\|", body)
        for name, m in [("vault_relocation_gate", relocate_m),
                        ("vault_auto_unlock_gate", unlock_m),
                        ("char_integrity_gate", char_m)]:
            self.assertIsNotNone(m, f"{name} invocation missing in cmd_start")
        assert relocate_m is not None and unlock_m is not None and char_m is not None
        self.assertLess(
            relocate_m.start(), unlock_m.start(),
            "vault_relocation_gate must precede vault_auto_unlock_gate "
            "(no point in mounting a vault if the data isn't relocated).",
        )
        self.assertLess(
            unlock_m.start(), char_m.start(),
            "vault_auto_unlock_gate must precede char_integrity_gate "
            "(no point in fingerprinting Char if we'd refuse to start anyway).",
        )


if __name__ == "__main__":
    unittest.main()
