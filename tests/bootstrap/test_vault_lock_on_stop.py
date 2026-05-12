"""Tests for ``run.sh``'s ``vault_lock_on_stop`` helper.

Why this file exists
--------------------

Until 2026-05-11, ``./run.sh stop`` left the encrypted sparse-bundle
vault mounted. The operator caught this manually -- they ran ``mount``
after ``stop`` and saw their ``local_scribe-vault`` volume still in
the list, defeating most of the "at-rest encryption" guarantee
documented in SECURITY.md § 'Defense layer 4'.

Fix: ``cmd_stop`` now calls ``vault_lock_on_stop`` after every service
has been killed. The helper is paranoid by design:

1. Polite-only detach (``vault_unlock lock --polite``). If anything
   holds the volume open we leave it mounted and tell the operator
   exactly which process to quit, because forcing the detach mid-
   write would risk corrupting Char's SQLite ``app.db``.

2. Refuses to even attempt the detach while Char.app is running --
   that *is* the most common open-handle case and the warning we
   print is more actionable than ``lsof`` output.

3. Idempotent: no-op when the vault is already unmounted, doesn't
   exist on disk, or when the venv hasn't been built yet (e.g. mid-
   bootstrap).

4. Honours ``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1`` and
   ``LOCAL_SCRIBE_SKIP_INTEGRITY=1`` (test seam) for symmetry with
   the start-side ``vault_relocation_gate`` / ``vault_auto_unlock_gate``.

The tests below pin each branch by injecting a fake ``$VENV_PY``
that emulates ``hdiutil``-free behaviour, and a fake ``pgrep`` for
the Char-running check.
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
    polite_detach_succeeds: bool,
) -> Path:
    """Write a fake ``$VENV_PY`` that returns the requested state
    when ``vault_lock_on_stop`` runs its two probes:

    1.  ``-c '... vault.exists() / vault.is_mounted() ...'`` —
        returns 0 (mounted) / 1 (exists but not mounted) / 2 (no
        vault).

    2.  ``-m local_scribe.security.vault_unlock lock --polite`` —
        returns 0 on success, 1 on polite failure.

    Other invocations forward to the system python so the rest of
    ``run.sh``'s bash glue keeps working.
    """
    if not exists:
        probe_rc = 2
    elif not mounted:
        probe_rc = 1
    else:
        probe_rc = 0
    lock_rc = 0 if polite_detach_succeeds else 1
    fake = tmp / "fake_venv_py"
    fake.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Probe: ``-c '... vault.exists() ... vault.is_mounted() ...'``
        for arg in "$@"; do
          if [[ "$arg" == *"vault.exists()"* ]]; then
            exit {probe_rc}
          fi
        done
        # CLI: ``-m local_scribe.security.vault_unlock lock --polite``
        # ``$@`` after the module flag looks like:
        #   -m local_scribe.security.vault_unlock lock --polite
        if [[ "$1" == "-m" && "$2" == "local_scribe.security.vault_unlock" \
              && "$3" == "lock" ]]; then
          exit {lock_rc}
        fi
        exec /usr/bin/env python3 "$@"
        """))
    fake.chmod(0o755)
    return fake


def _fake_char_running(tmp: Path, *, running: bool) -> Path:
    """Shadow ``pgrep`` so ``char_running`` in run.sh returns the
    requested state without touching real processes."""
    bin_dir = tmp / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "pgrep"
    rc = 0 if running else 1
    fake.write_text(f"#!/usr/bin/env bash\nexit {rc}\n")
    fake.chmod(0o755)
    return bin_dir


def _invoke(
    *,
    tmp: Path,
    exists: bool,
    mounted: bool,
    polite_detach_succeeds: bool = True,
    char_running: bool = False,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    fake_py = _fake_venv_py(
        tmp,
        exists=exists,
        mounted=mounted,
        polite_detach_succeeds=polite_detach_succeeds,
    )
    fake_bin = _fake_char_running(tmp, running=char_running)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp),
        "TERM": "dumb",
    }
    if extra_env:
        env.update(extra_env)
    script = (
        f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
        f'VENV_PY="{fake_py}"\n'
        f'if vault_lock_on_stop 2>&1; then rc=0; else rc=$?; fi\n'
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


class VaultLockOnStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-vault-lock-on-stop-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- happy path --------------------------------------------------

    def test_mounted_and_char_quit_polite_detach_succeeds(self) -> None:
        """The whole-life-cycle expectation: services down, Char.app
        quit, vault mounted → we polite-detach and tell the operator."""
        r = _invoke(tmp=self.tmp, exists=True, mounted=True, char_running=False)
        rc = _exit_code_from(r.stdout)
        self.assertEqual(rc, 0, msg=f"unexpected rc={rc}; output:\n{r.stdout}\n{r.stderr}")
        # Success banner is contractual -- the user looks for it
        # specifically to confirm the volume dismounted.
        self.assertIn("vault dismounted", r.stdout)
        self.assertIn("ciphertext-at-rest", r.stdout)

    # -- idempotent no-op branches -----------------------------------

    def test_no_vault_on_disk_is_silent_noop(self) -> None:
        """Bootstrap path: no vault yet, ``stop`` shouldn't be noisy."""
        r = _invoke(tmp=self.tmp, exists=False, mounted=False)
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("vault dismounted", r.stdout)
        # And NEVER the loud warning when there's simply nothing to lock.
        combined = r.stdout + r.stderr
        self.assertNotIn("vault left mounted", combined)

    def test_already_unmounted_is_silent_noop(self) -> None:
        """Operator manually ran ``./run.sh vault lock`` before ``stop``;
        ``stop`` shouldn't print a redundant success message."""
        r = _invoke(tmp=self.tmp, exists=True, mounted=False)
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("vault dismounted", r.stdout)

    # -- safety branches ---------------------------------------------

    def test_refuses_to_detach_while_char_is_running(self) -> None:
        """SQLite safety: if Char is still up, polite detach would
        fail anyway and ``-force`` would risk corruption. We surface
        the right next step (quit Char) instead of attempting it."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=True, char_running=True,
        )
        rc = _exit_code_from(r.stdout)
        # We deliberately exit 0 -- ``cmd_stop`` shouldn't fail just
        # because the operator still has Char open; the services are
        # already down.
        self.assertEqual(rc, 0, msg=f"unexpected rc={rc}; output:\n{r.stdout}\n{r.stderr}")
        combined = r.stdout + r.stderr
        self.assertIn("Char.app is still running", combined)
        self.assertIn("./run.sh vault lock", combined)
        # Critically: we did NOT claim the vault was dismounted.
        self.assertNotIn("vault dismounted", r.stdout)

    def test_polite_detach_failure_warns_but_does_not_fail_stop(self) -> None:
        """Spotlight indexer or stray Finder window: polite detach
        fails, ``-force`` is explicitly off-limits in this path, and
        we want ``cmd_stop`` to *still* succeed overall (services
        are already down)."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=True,
            polite_detach_succeeds=False, char_running=False,
        )
        rc = _exit_code_from(r.stdout)
        self.assertEqual(rc, 0, msg=f"polite failure broke cmd_stop (rc={rc}):\n{r.stdout}\n{r.stderr}")
        combined = r.stdout + r.stderr
        self.assertIn("vault left mounted", combined)
        # The operator needs an actionable next step: name the
        # interactive lock command (which DOES force) and the lsof
        # incantation to find the offending process.
        self.assertIn("./run.sh vault lock", combined)
        self.assertIn("lsof", combined)

    # -- environment overrides ---------------------------------------

    def test_allow_plaintext_env_skips_lock(self) -> None:
        """``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1`` means the
        operator has explicitly opted out of the vault relationship.
        Don't try to manage a vault that isn't part of their flow."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=True,
            extra_env={"LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA": "1"},
        )
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("vault dismounted", r.stdout)

    def test_skip_integrity_env_skips_lock(self) -> None:
        """``LOCAL_SCRIBE_SKIP_INTEGRITY=1`` is the test seam for
        sibling test files; pin it so future contributors can rely
        on the short-circuit."""
        r = _invoke(
            tmp=self.tmp, exists=True, mounted=True,
            extra_env={"LOCAL_SCRIBE_SKIP_INTEGRITY": "1"},
        )
        self.assertEqual(_exit_code_from(r.stdout), 0)
        self.assertNotIn("vault dismounted", r.stdout)


# ---------------------------------------------------------------------
# Static wiring checks.


class CmdStopInvokesLockTests(unittest.TestCase):
    """``cmd_stop`` must invoke ``vault_lock_on_stop`` AFTER every
    service has been stopped. Without these static checks, a future
    refactor could quietly drop the call (leaving the vault mounted)
    or move it before the service stops (risking detach-while-busy)."""

    def _cmd_stop_body(self) -> str:
        text = _RUN_SH.read_text()
        m = re.search(r"^cmd_stop\(\)\s*\{\s*$", text, flags=re.M)
        self.assertIsNotNone(m, "cmd_stop() not found in run.sh")
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

    def test_cmd_stop_calls_vault_lock_on_stop(self) -> None:
        body = self._cmd_stop_body()
        self.assertIn(
            "vault_lock_on_stop", body,
            msg=(
                "cmd_stop() no longer calls vault_lock_on_stop. Without "
                "it, ./run.sh stop leaves the encrypted vault mounted, "
                "defeating the at-rest encryption guarantee documented "
                "in SECURITY.md § 'Defense layer 4'."
            ),
        )

    def test_vault_lock_on_stop_runs_after_services_stop(self) -> None:
        """Order matters: we must kill ASR / inspector / egress proxy
        BEFORE detaching, otherwise they still hold read handles on
        files inside the mount and the polite detach bounces."""
        body = self._cmd_stop_body()
        asr = body.find("asr_stop")
        ins = body.find("inspector_stop")
        eg = body.find("egress_proxy_stop")
        lock = body.find("vault_lock_on_stop")
        for name, idx in [("asr_stop", asr), ("inspector_stop", ins),
                          ("egress_proxy_stop", eg),
                          ("vault_lock_on_stop", lock)]:
            self.assertGreaterEqual(
                idx, 0, f"cmd_stop is missing {name}: body={body!r}",
            )
        self.assertLess(asr, lock, "asr_stop must precede vault_lock_on_stop")
        self.assertLess(ins, lock, "inspector_stop must precede vault_lock_on_stop")
        self.assertLess(eg, lock, "egress_proxy_stop must precede vault_lock_on_stop")


if __name__ == "__main__":
    unittest.main()
