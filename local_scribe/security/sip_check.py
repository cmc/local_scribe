"""System Integrity Protection (SIP) gate.

local_scribe refuses to run on a macOS host where SIP is disabled.
This module is the source of truth for that policy.

Why SIP is *load-bearing* for our threat model
----------------------------------------------

Every other defense in the project (the Option C split-key, the
service-auth bearer tokens, the firewall, the char-binary integrity
check, the script-integrity gate) assumes the kernel honours the
boundaries macOS normally enforces between processes. **SIP is what
makes those boundaries real.** With SIP disabled:

* ``task_for_pid()`` becomes unrestricted for codesigned binaries,
  so any user-space process can ``mach_vm_read`` our heap and
  extract the reconstituted master key seconds after a Touch ID
  unlock.
* ``DYLD_INSERT_LIBRARIES`` is no longer stripped from
  ``hardened-runtime`` / ``library-validation`` binaries, so an
  attacker can side-load arbitrary code into Char.app, Python, or
  the Swift Touch ID helper before our code has a chance to run.
* ``dtrace -p <pid>`` can attach to anything, including the
  ``secd`` Keychain daemon — at which point the Touch ID ACL is a
  speed bump, not a barrier.
* Filesystem protections on ``/System``, ``/usr/bin``, ``/sbin``
  are off, so an attacker can replace ``codesign``, ``spctl``,
  ``security`` (the Keychain CLI), ``csrutil`` itself, or any of
  the standard binaries our integrity checks shell out to.
* NVRAM protections are off, so an attacker can set boot args
  (e.g. ``boot-args="amfi_get_out_of_my_way=1"``) that defeat code
  signing entirely on the next boot.
* Kernel-extension signing is off, so a kext rootkit can be
  installed without a signed certificate.

Each of those means a motivated adversary can compromise the key
material that secures the user's recordings *without ever
triggering one of our higher-layer gates*. The script-integrity
check, the Char-binary check, even the YubiKey tap itself, all
become bypassable by an attacker with code-execution-as-user on a
SIP-disabled machine.

Therefore: we treat SIP as a non-negotiable prerequisite, gated at
the very top of ``./run.sh start``, the bootstrap path, every
``./run.sh key …`` operation, and inside every service's startup
lifespan. ``./run.sh stop / status / doctor`` continue to work on
a SIP-disabled host so the operator can clean up and reboot to
recovery to re-enable SIP, but anything that *touches keys*
refuses to proceed.

Dev-mode bypass
~~~~~~~~~~~~~~~

There is one documented escape hatch: the ``LOCAL_SCRIBE_DEV_MODE``
environment variable. See
[`local_scribe/common/dev_mode.py`](../common/dev_mode.py) for the
full rationale + the inspector banner it triggers. When dev mode
is on, :func:`enforce_or_die` returns the parsed report (with its
real ``state``) instead of raising — but it prints a multi-line
red banner to stderr on every call so the operator cannot forget
the kernel boundary is no longer being enforced. Callers that
explicitly want strict behaviour regardless of dev mode (the unit
tests for this module, the production key-rotation CLI) use
:func:`enforce_or_die_strict` directly.

Detecting SIP
-------------

We shell out to ``/usr/bin/csrutil status``. The expected output
forms are:

* ``System Integrity Protection status: enabled.`` — full SIP, the
  only state we accept.
* ``System Integrity Protection status: disabled.`` — fully off,
  rejected.
* ``System Integrity Protection status: enabled (Custom
  Configuration).`` — followed by a ``Configuration:`` block. We
  reject unless every protection we care about (Filesystem,
  Debugging, DTrace, NVRAM, Kext, BaseSystem) is on. Apple
  Internal is ignored (it's an Apple-engineering flag, not a
  security boundary).

If ``csrutil`` is missing, returns a nonzero exit, or prints
something we don't recognise, we treat that as "unknown" and
refuse — fail-closed is the only safe default here.

Testing
-------

The hard-coded check is suppressed when
``LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT`` is set: the value of that env
variable is substituted for the live ``csrutil`` output. This is
the *only* way to bypass the gate, and it is documented as a
test-only seam — there is no operator-facing override.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


# ---------- typed errors / results ----------------------------------


class SIPState(str, Enum):
    """High-level summary of what ``csrutil status`` reported."""

    #: Top-line ``enabled.`` with no custom-configuration block, or
    #: every protection we care about is on within a custom config.
    FULLY_ENABLED = "fully_enabled"

    #: Top-line ``disabled.`` — every protection is off.
    DISABLED = "disabled"

    #: Top-line ``enabled (Custom Configuration).`` AND at least one
    #: of {Filesystem, Debugging, DTrace, NVRAM, Kext, BaseSystem}
    #: is off.
    PARTIALLY_DISABLED = "partially_disabled"

    #: ``csrutil`` not available, returned nonzero, or produced
    #: output we can't parse. Treated as "fail closed" by
    #: :func:`enforce_or_die`.
    UNKNOWN = "unknown"


class SIPDisabledError(RuntimeError):
    """Raised by :func:`enforce_or_die` when SIP is not fully on.

    Callers catch this at the top of ``./run.sh start`` and the
    service-startup lifespan to print the banner and exit. The
    message embeds the parsed report so the caller can log it
    verbatim without re-running ``csrutil``."""


# Configuration keys we insist on when csrutil reports a custom
# configuration. Order matters: it's the order the banner prints.
REQUIRED_PROTECTIONS: tuple[str, ...] = (
    "Filesystem Protections",
    "Debugging Restrictions",
    "DTrace Restrictions",
    "NVRAM Protections",
    "Kext Signing",
    "BaseSystem Verification",
)

# Keys we know about but don't require to be on (informational only).
INFORMATIONAL_PROTECTIONS: tuple[str, ...] = (
    "Apple Internal",
)


@dataclass
class SIPReport:
    """JSON-safe snapshot of the current SIP state. Returned by
    :func:`status` and embedded in :class:`SIPDisabledError`."""

    state: SIPState
    raw_top_line: Optional[str] = None
    protections: dict[str, bool] = field(default_factory=dict)
    missing_protections: list[str] = field(default_factory=list)
    error: Optional[str] = None
    raw_output: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "raw_top_line": self.raw_top_line,
            "protections": dict(self.protections),
            "missing_protections": list(self.missing_protections),
            "error": self.error,
            # ``raw_output`` is deliberately *not* in the dict by
            # default — it's only useful when debugging unknown
            # outputs and otherwise clutters logs.
        }


# ---------- subprocess + parser -------------------------------------


_CSRUTIL_PATH = "/usr/bin/csrutil"
_TEST_OVERRIDE_ENV = "LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT"


def _csrutil_output() -> tuple[Optional[str], Optional[str]]:
    """Returns ``(stdout, error_message)``. Honours the
    ``LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT`` env variable as a test
    seam — anything else shells out to the live binary.

    A missing or non-executable ``csrutil`` returns
    ``(None, "csrutil not found")``."""
    override = os.environ.get(_TEST_OVERRIDE_ENV)
    if override is not None:
        return override, None

    if not (os.path.isfile(_CSRUTIL_PATH) and os.access(_CSRUTIL_PATH, os.X_OK)):
        # csrutil ships in /usr/bin on every macOS ≥ 10.11; absence
        # means either non-macOS host or a tampered /usr/bin. Either
        # way, fail closed.
        return None, f"csrutil not found at {_CSRUTIL_PATH}"

    try:
        proc = subprocess.run(
            [_CSRUTIL_PATH, "status"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"csrutil invocation failed: {exc}"
    if proc.returncode != 0:
        return None, (
            f"csrutil returned {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout, None


# csrutil's protection lines are tab- or space-indented "Name: state".
_PROTECTION_LINE_RE = re.compile(
    r"^\s+(?P<name>[A-Za-z][A-Za-z ]+?)\s*:\s*(?P<state>enabled|disabled)\b",
)
_TOP_LINE_RE = re.compile(
    r"^System Integrity Protection status:\s*(?P<state>.+?)\s*$",
)


def _parse(output: str) -> SIPReport:
    """Parse csrutil output into an :class:`SIPReport`. Public for
    testability; in production callers go through :func:`status`."""
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return SIPReport(state=SIPState.UNKNOWN, error="empty csrutil output")

    top = _TOP_LINE_RE.match(lines[0])
    if not top:
        return SIPReport(
            state=SIPState.UNKNOWN,
            raw_output=output,
            error=f"unrecognised first line: {lines[0]!r}",
        )

    raw_state = top.group("state")
    protections: dict[str, bool] = {}
    for ln in lines[1:]:
        m = _PROTECTION_LINE_RE.match(ln)
        if not m:
            continue
        protections[m.group("name").strip()] = (m.group("state") == "enabled")

    # Top-line decision tree.
    if raw_state.startswith("enabled") and "Custom" not in raw_state:
        # Plain "enabled." — the strict-best case.
        return SIPReport(
            state=SIPState.FULLY_ENABLED,
            raw_top_line=raw_state,
            protections=protections,
        )

    if raw_state.startswith("enabled") and "Custom" in raw_state:
        missing = [
            name for name in REQUIRED_PROTECTIONS
            if protections.get(name) is not True
        ]
        if not missing:
            return SIPReport(
                state=SIPState.FULLY_ENABLED,
                raw_top_line=raw_state,
                protections=protections,
            )
        return SIPReport(
            state=SIPState.PARTIALLY_DISABLED,
            raw_top_line=raw_state,
            protections=protections,
            missing_protections=missing,
        )

    if raw_state.startswith("disabled"):
        return SIPReport(
            state=SIPState.DISABLED,
            raw_top_line=raw_state,
            protections=protections,
            missing_protections=list(REQUIRED_PROTECTIONS),
        )

    return SIPReport(
        state=SIPState.UNKNOWN,
        raw_top_line=raw_state,
        raw_output=output,
        error=f"unrecognised SIP state: {raw_state!r}",
    )


# ---------- public API ---------------------------------------------


def status() -> SIPReport:
    """Return the parsed SIP report. Never raises; UNKNOWN on
    error so :func:`enforce_or_die` can decide policy."""
    out, err = _csrutil_output()
    if out is None:
        return SIPReport(state=SIPState.UNKNOWN, error=err)
    return _parse(out)


def is_fully_enabled() -> bool:
    """Convenience boolean for the gate. ``True`` only when SIP is
    confirmed fully on. UNKNOWN, PARTIALLY_DISABLED, and DISABLED
    all return ``False`` (fail closed)."""
    return status().state == SIPState.FULLY_ENABLED


def enforce_or_die_strict() -> SIPReport:
    """Strict variant: raises :class:`SIPDisabledError` for any
    non-FULLY_ENABLED state regardless of ``LOCAL_SCRIBE_DEV_MODE``.

    Used by:

    * the unit tests for this module (we want to exercise the
      raise path even when the test host has dev-mode on),
    * the production key-rotation CLI (rotating the master key on
      a SIP-disabled host means the new key is exfiltrable from
      our heap during the rotation itself, which would silently
      compromise the rotation),
    * any future caller that needs the original always-fail-closed
      semantics.

    The dev-mode-aware variant is :func:`enforce_or_die` below; new
    callers should usually prefer that one and get the
    documented-bypass behaviour for free.
    """
    report = status()
    if report.state == SIPState.FULLY_ENABLED:
        return report
    detail = format_banner(report, color=False)
    raise SIPDisabledError(detail)


def enforce_or_die(*, allow_dev_mode: bool = True) -> SIPReport:
    """Return the report when SIP is fully enabled, otherwise raise
    :class:`SIPDisabledError`. Use this from any caller that
    *requires* SIP — top of ``cmd_start``, every key-lifecycle CLI
    entry point, every FastAPI service lifespan.

    When ``allow_dev_mode=True`` (the default) and the
    ``LOCAL_SCRIBE_DEV_MODE`` env var is set, the function emits a
    loud red banner to stderr (once per process; one-line indicator
    on subsequent calls) and returns the parsed report instead of
    raising. The returned report's ``state`` is unchanged — callers
    that want to *behave differently* in dev mode can inspect
    ``state`` themselves; callers that just want "continue but warn"
    get that automatically.

    Pass ``allow_dev_mode=False`` to get the strict-no-matter-what
    behaviour — equivalent to calling :func:`enforce_or_die_strict`
    directly. Provided so the kwarg-only opt-out is visible at the
    callsite rather than requiring a separate import.

    The message is structured for direct embedding in operator-
    facing banners. Callers that want colour should use
    :func:`format_banner` instead of the bare exception text."""
    report = status()
    if report.state == SIPState.FULLY_ENABLED:
        return report
    if allow_dev_mode:
        # Lazy import to avoid a hard dependency from the security
        # subpackage on common when sip_check is imported as a
        # standalone CLI module (``python -m local_scribe.security.sip_check``).
        from local_scribe.common import dev_mode as _dev
        if _dev.is_enabled():
            import sys as _sys
            if not _dev.emit_banner_once(_sys.stderr):
                # Subsequent gate invocations within the same
                # process get the short one-line indicator so the
                # operator sees that SIP was bypassed *here* even
                # though the long banner already scrolled past.
                _sys.stderr.write(
                    f"{_dev.short_indicator(color=_sys.stderr.isatty())} "
                    f"sip_check.enforce_or_die: bypassed "
                    f"(state={report.state.value})\n"
                )
            return report
    detail = format_banner(report, color=False)
    raise SIPDisabledError(detail)


# ---------- presentation -------------------------------------------


def format_banner(report: SIPReport, *, color: bool = True) -> str:
    """Render a multi-line operator-facing banner. Used by
    ``./run.sh`` (color=True via tty) and the service lifespans
    (color=False, plain text into the systemd-style log line)."""

    if color:
        red = "\033[31m"
        yellow = "\033[33m"
        bold = "\033[1m"
        dim = "\033[2m"
        reset = "\033[0m"
    else:
        red = yellow = bold = dim = reset = ""

    lines: list[str] = []
    lines.append(
        f"{red}{bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{reset}"
    )

    if report.state == SIPState.DISABLED:
        lines.append(
            f"{red}{bold}REFUSING TO RUN: System Integrity Protection is DISABLED.{reset}"
        )
    elif report.state == SIPState.PARTIALLY_DISABLED:
        lines.append(
            f"{red}{bold}REFUSING TO RUN: SIP is in a partially-disabled custom configuration.{reset}"
        )
    elif report.state == SIPState.UNKNOWN:
        lines.append(
            f"{red}{bold}REFUSING TO RUN: could not verify SIP state.{reset}"
        )
    else:
        lines.append(
            f"{bold}SIP fully enabled ✓{reset}"
        )

    if report.raw_top_line:
        lines.append(
            f"  {dim}csrutil reports:{reset} "
            f"{report.raw_top_line!r}"
        )
    if report.error:
        lines.append(f"  {dim}parse error:{reset} {report.error}")
    if report.missing_protections:
        lines.append(
            f"  {yellow}missing protections:{reset} "
            f"{', '.join(report.missing_protections)}"
        )

    if report.state != SIPState.FULLY_ENABLED:
        lines.extend([
            "",
            f"{bold}Why this matters{reset}",
            "  local_scribe's entire threat model assumes the kernel honours the",
            "  user-space process boundaries macOS normally enforces. With SIP off:",
            "    • task_for_pid() is unrestricted → another process can read the",
            "      reconstituted master key out of our heap after Touch ID unlock.",
            "    • DYLD_INSERT_LIBRARIES is no longer stripped from codesigned",
            "      binaries → arbitrary code can be injected into Char or our",
            "      Python services before any gate runs.",
            "    • dtrace -p attaches to anything, including the Keychain daemon.",
            "    • /usr/bin/codesign, /usr/bin/security, csrutil itself can be",
            "      replaced, defeating our integrity checks.",
            "    • Boot-args can be set in NVRAM to disable AMFI/code signing",
            "      entirely on the next boot.",
            "",
            f"{bold}How to fix{reset}",
            "  1. Reboot into macOS Recovery (hold the power button on Apple",
            "     Silicon, or ⌘-R during boot on Intel).",
            "  2. Open Utilities → Terminal.",
            "  3. Run: csrutil enable",
            "  4. Reboot.",
            "  5. Verify with: csrutil status   (must report 'enabled.')",
            "",
            f"{dim}See SECURITY.md § 'Defense layer 0 — System Integrity Protection'{reset}",
            f"{dim}for the full rationale. No operator override is provided — this is{reset}",
            f"{dim}a non-negotiable prerequisite of every other security control in{reset}",
            f"{dim}the project.{reset}",
        ])
    lines.append(
        f"{red}{bold}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{reset}"
    )
    return "\n".join(lines)


# ---------- CLI entry points ---------------------------------------


def _cli_status(_args: list[str]) -> int:
    """``python -m sip_check status`` — print the JSON report. Adds
    a ``dev_mode_active`` boolean so consumers (the inspector's
    integrity-status tile, ``./run.sh doctor``, the CLI status
    command) can render the bypass state without re-reading the
    env var themselves."""
    import json as _json
    import sys as _sys
    from local_scribe.common import dev_mode as _dev
    rep = status()
    payload = rep.to_dict()
    payload["dev_mode_active"] = _dev.is_enabled()
    _sys.stdout.write(_json.dumps(payload, indent=2) + "\n")
    return 0


def _cli_check(_args: list[str]) -> int:
    """``python -m sip_check check`` — exit 0 if fully enabled (or
    if dev mode is on, in which case the bypass banner is printed
    to stderr first), nonzero otherwise. Prints the SIP-disabled
    banner to stderr on the strict-fail path so callers (run.sh)
    can let it pass through.

    Two distinct exit codes:
      0 — SIP fully on, OR LOCAL_SCRIBE_DEV_MODE=1 and the bypass
          banner was emitted.
      1 — SIP not fully on AND dev mode is off (the original
          fail-closed behaviour). ``run.sh::sip_gate`` translates
          this into the CLI's REFUSING TO RUN banner.
    """
    import sys as _sys
    try:
        enforce_or_die()
        return 0
    except SIPDisabledError as exc:
        # The banner is already inside the exception message; emit
        # it as-is to stderr.
        _sys.stderr.write(str(exc) + "\n")
        return 1


def _cli_banner(_args: list[str]) -> int:
    """``python -m sip_check banner`` — always prints the
    coloured banner to stderr regardless of state. Used by
    ``./run.sh doctor`` to render the SIP section consistently."""
    import sys as _sys
    rep = status()
    _sys.stderr.write(format_banner(rep, color=_sys.stderr.isatty()) + "\n")
    return 0 if rep.state == SIPState.FULLY_ENABLED else 1


def _cli_main(argv: list[str]) -> int:
    import sys as _sys
    table = {
        "status":   _cli_status,
        "check":    _cli_check,
        "banner":   _cli_banner,
    }
    if len(argv) < 2:
        _sys.stderr.write(
            f"usage: {argv[0]} <{'|'.join(sorted(table))}>\n"
        )
        return 2
    cmd = argv[1]
    fn = table.get(cmd)
    if fn is None:
        _sys.stderr.write(f"unknown subcommand: {cmd}\n")
        return 2
    return fn(argv[2:])


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv))
