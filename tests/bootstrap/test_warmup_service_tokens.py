"""Tests for ``run.sh``'s ``warmup_service_tokens`` helper + its
wiring into ``cmd_start``.

Why this file exists
--------------------

The 2026-05-11 operator audit found that ``./run.sh start`` issued
TWO independent Touch ID + YubiKey unlocks (one per
authentication-requiring service: ASR and inspector), each fired
inside a daemonised subprocess whose stderr was log-redirected.
The operator was left staring at "starting asr ..." with no
textual cue in the terminal that they needed to authenticate.

Fix: a foreground ``warmup_service_tokens`` step in ``cmd_start``:

1.  Prints a loud heads-up banner BEFORE the unlock kicks off.
2.  Shells out to ``service_auth warm asr inspector`` which does
    ONE unlock, derives every requested token, emits JSON.
3.  Bash parses the JSON into locals and spawns each service with
    its token in the PER-SUBPROCESS environ (``VAR=val funcname``
    form so the parent shell never holds the secret).

These tests pin:

* The static wiring (cmd_start invokes the helper BEFORE the
  service starts).
* The runtime behaviour (banner, JSON capture, success / failure
  / bypass paths).
* The token-passing safety invariant (parent shell's environ
  must NOT contain the tokens after start completes).
"""

from __future__ import annotations

import json
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


def _fake_venv_py(tmp: Path, *, warm_payload: str | None,
                  warm_rc: int = 0) -> Path:
    """Write a fake ``$VENV_PY`` that:

    * For ``-m local_scribe.security.service_auth warm <args>``:
      prints ``warm_payload`` (JSON dict, or empty for failure
      branch) to stdout and exits ``warm_rc``.
    * For ``-c 'import json...'`` (the JSON-parse helper run.sh
      uses to pluck individual tokens out of the warm payload):
      forwards to system python3 so we exercise the REAL parse.
    * Everything else: forwards to system python3.
    """
    fake = tmp / "fake_venv_py"
    payload = warm_payload if warm_payload is not None else ""
    fake.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [[ "$1" == "-m" \
              && "$2" == "local_scribe.security.service_auth" \
              && "$3" == "warm" ]]; then
          printf '%s\\n' {payload!r}
          exit {warm_rc}
        fi
        exec /usr/bin/env python3 "$@"
        """))
    fake.chmod(0o755)
    return fake


def _invoke_warmup(
    *,
    tmp: Path,
    warm_payload: str | None,
    warm_rc: int = 0,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    fake_py = _fake_venv_py(tmp, warm_payload=warm_payload, warm_rc=warm_rc)
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
        f'declare _out=""\n'
        f'if warmup_service_tokens _out; then rc=0; else rc=$?; fi\n'
        f'printf "__rc=%s\\n" "$rc"\n'
        f'printf "__payload=%s\\n" "$_out"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env, capture_output=True, text=True, timeout=30,
        cwd=str(_REPO),
    )


def _grab(output: str, key: str) -> str:
    """Return the LAST line matching ``__<key>=<value>``. The bash
    helper prints the markers on stdout after the human-facing
    banners on stderr."""
    target = f"__{key}="
    for line in reversed(output.splitlines()):
        if line.startswith(target):
            return line[len(target):]
    raise AssertionError(f"no {target}<value> marker in output:\n{output}")


class WarmupServiceTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-warmup-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- happy path --------------------------------------------------

    def test_happy_path_captures_payload(self) -> None:
        """The expected production case: warm verb emits a JSON dict,
        warmup helper assigns it to the caller's nameref and exits 0."""
        payload = json.dumps({
            "asr": "ls_asr_" + "a" * 32,
            "inspector": "ls_inspector_" + "b" * 32,
        })
        r = _invoke_warmup(tmp=self.tmp, warm_payload=payload, warm_rc=0)
        self.assertEqual(_grab(r.stdout, "rc"), "0",
                         msg=f"unexpected rc:\n{r.stdout}\n{r.stderr}")
        captured = _grab(r.stdout, "payload")
        self.assertEqual(json.loads(captured), json.loads(payload))

    def test_heads_up_banner_printed_before_warm_invocation(self) -> None:
        """The operator-facing banner explaining the upcoming Touch
        ID + YubiKey prompts must appear in the terminal output. The
        2026-05-11 audit explicitly asked for this; we pin the
        banner content here so a future "just clean up the cosmetics"
        commit can't quietly drop it."""
        payload = json.dumps({"asr": "ls_asr_x", "inspector": "ls_inspector_y"})
        r = _invoke_warmup(tmp=self.tmp, warm_payload=payload, warm_rc=0)
        combined = r.stdout + r.stderr
        self.assertIn("Authentication warmup", combined)
        self.assertIn("Touch ID modal", combined)
        self.assertIn("YubiKey", combined)
        self.assertIn("flashing", combined)

    # -- bypass / failure branches -----------------------------------

    def test_disable_auth_short_circuits_with_empty_object(self) -> None:
        """``LOCAL_SCRIBE_DISABLE_AUTH=1``: no unlock, empty payload.
        Caller branches on emptiness and skips the env injection."""
        r = _invoke_warmup(
            tmp=self.tmp, warm_payload=None, warm_rc=99,   # would-be-failure
            extra_env={"LOCAL_SCRIBE_DISABLE_AUTH": "1"},
        )
        self.assertEqual(_grab(r.stdout, "rc"), "0")
        self.assertEqual(_grab(r.stdout, "payload"), "{}")
        # And we should NOT print the banner if there's no unlock to do.
        self.assertNotIn("Authentication warmup", r.stderr)
        self.assertNotIn("Authentication warmup", r.stdout)

    def test_warm_failure_returns_nonzero_with_recovery_hint(self) -> None:
        """Operator cancels Touch ID / no YubiKey: warm verb exits
        non-zero. Helper surfaces the failure with a clear recovery
        hint that names the bypass env var."""
        r = _invoke_warmup(tmp=self.tmp, warm_payload="", warm_rc=1)
        self.assertNotEqual(_grab(r.stdout, "rc"), "0")
        combined = r.stdout + r.stderr
        self.assertIn("token warmup failed", combined)
        self.assertIn("LOCAL_SCRIBE_DISABLE_AUTH", combined)

    def test_venv_missing_returns_nonzero(self) -> None:
        """Mid-bootstrap call: helper refuses cleanly rather than
        bashing into an unset VENV_PY."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.tmp),
            "TERM": "dumb",
        }
        script = (
            f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
            f'VENV_PY="/nonexistent/python"\n'
            f'declare _out=""\n'
            f'if warmup_service_tokens _out; then rc=0; else rc=$?; fi\n'
            f'printf "__rc=%s\\n" "$rc"\n'
            f'printf "__payload=%s\\n" "$_out"\n'
        )
        r = subprocess.run(
            ["bash", "-c", script],
            env=env, capture_output=True, text=True, timeout=10,
            cwd=str(_REPO),
        )
        self.assertNotEqual(_grab(r.stdout, "rc"), "0")
        combined = r.stdout + r.stderr
        self.assertIn("venv python missing", combined)


# ---------------------------------------------------------------------
# Static wiring checks — ``cmd_start`` must invoke the helper at the
# right point in the start sequence.


class CmdStartInvokesWarmupTests(unittest.TestCase):
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

    def test_cmd_start_calls_warmup(self) -> None:
        body = self._cmd_start_body()
        self.assertIn(
            "warmup_service_tokens", body,
            msg=(
                "cmd_start() no longer invokes warmup_service_tokens. "
                "Without it, ./run.sh start regresses to issuing two "
                "Touch ID + YubiKey prompts inside daemonised "
                "subprocesses, with no textual cue in the operator's "
                "terminal. See SECURITY_AUDIT.md F10 for the audit "
                "finding and the operator-facing impact."
            ),
        )

    def test_warmup_runs_before_service_starts(self) -> None:
        """Order matters: the unlock has to happen BEFORE we spawn
        the daemons so the prompts land in run.sh's foreground
        terminal, not the daemons' log files."""
        body = self._cmd_start_body()
        warmup_m = re.search(r"\bwarmup_service_tokens\b", body)
        asr_m = re.search(r"\basr_start\b", body)
        inspector_m = re.search(r"\binspector_start\b", body)
        for name, m in [("warmup_service_tokens", warmup_m),
                        ("asr_start", asr_m),
                        ("inspector_start", inspector_m)]:
            self.assertIsNotNone(m, f"{name} not found in cmd_start body")
        assert warmup_m is not None and asr_m is not None and inspector_m is not None
        self.assertLess(warmup_m.start(), asr_m.start(),
                        "warmup must run before asr_start")
        self.assertLess(warmup_m.start(), inspector_m.start(),
                        "warmup must run before inspector_start")

    def test_asr_start_invoked_with_per_subprocess_token_env(self) -> None:
        """Cardinal rule of bearer-token handoff: the token MUST be
        passed via ``VAR=val cmd`` (per-subprocess) NEVER ``export
        VAR; cmd`` (parent shell). We pin the per-subprocess form
        statically so a future cleanup can't quietly switch to the
        leak-prone variant.

        The expected line in cmd_start is essentially::

            LOCAL_SCRIBE_ASR_TOKEN="$_asr_tok" asr_start || return 1
        """
        body = self._cmd_start_body()
        # Must see the per-subprocess env-on-invocation form.
        self.assertRegex(
            body,
            r"LOCAL_SCRIBE_ASR_TOKEN=\"\$\{?_asr_tok\}?\"\s+asr_start\b",
            msg=(
                "asr_start must be invoked with LOCAL_SCRIBE_ASR_TOKEN "
                "set ONLY for that subprocess (bash 'VAR=val cmd' form). "
                "Using 'export LOCAL_SCRIBE_ASR_TOKEN=...' would leak "
                "the token into the parent shell's environ where the "
                "later ``exec tail -F`` inherits it."
            ),
        )
        # And the same for inspector.
        self.assertRegex(
            body,
            r"LOCAL_SCRIBE_INSPECTOR_TOKEN=\"\$\{?_inspector_tok\}?\"\s+inspector_start\b",
            msg=(
                "inspector_start must be invoked with "
                "LOCAL_SCRIBE_INSPECTOR_TOKEN set ONLY for that "
                "subprocess."
            ),
        )

    def test_cmd_start_never_exports_service_tokens(self) -> None:
        """Defense in depth: the parent shell must NEVER ``export`` a
        derived service token. The per-subprocess form is the only
        sanctioned way to hand a token to a child."""
        body = self._cmd_start_body()
        self.assertNotRegex(
            body,
            r"export\s+LOCAL_SCRIBE_(ASR|INSPECTOR)_TOKEN",
            msg=(
                "cmd_start exports a service token into the parent "
                "shell's environ. This leaks the token to every "
                "subsequent subprocess (including the final "
                "``exec tail -F``). Use the bash 'VAR=val cmd' "
                "per-subprocess form instead."
            ),
        )

    def test_cmd_start_unsets_token_locals_after_use(self) -> None:
        """After both services are spawned, the bash-local token
        variables should be unset so a follow-up ``set`` / ``env``
        / sourced sub-script can't read them. We pin the cleanup
        line so a refactor can't drop it silently."""
        body = self._cmd_start_body()
        self.assertRegex(
            body,
            r"unset\s+_asr_tok\s+_inspector_tok",
            msg=(
                "cmd_start no longer unsets the bash-local token "
                "variables after spawning services. They linger in "
                "the shell until cmd_start returns, broadening the "
                "leak window."
            ),
        )


if __name__ == "__main__":
    unittest.main()
