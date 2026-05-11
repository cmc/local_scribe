"""Dev-mode flag — explicit, loud, SIP-gate bypass for development.

Why this module exists
----------------------

The rest of the project treats System Integrity Protection (SIP) as
a *non-negotiable* prerequisite. See
[`local_scribe/security/sip_check.py`](sip_check.py) for the full
threat-model rationale: with SIP off, ``task_for_pid()`` is
unrestricted, ``DYLD_INSERT_LIBRARIES`` is no longer stripped from
codesigned binaries, ``dtrace -p`` attaches to anything (including
the Keychain daemon), and any one of those breaks every higher
layer of defense. We deliberately ship without an operator-facing
"just let me run" knob.

Dev mode is the *one* documented exception: when a developer needs
to iterate on the pipeline itself (debug a uvicorn handler, run
the full integration suite on a CI host that doesn't have SIP
configured, attach `lldb` to the ASR worker), the SIP gate is a
hard blocker. ``LOCAL_SCRIBE_DEV_MODE=1`` lets the gate pass — but
not silently:

* The CLI prints a multi-line red banner on every gated entry
  point so it cannot be missed in terminal output.
* The inspector renders a sticky red banner across the very top of
  every page so a browser-facing operator cannot forget the
  pipeline is running in degraded-security mode.
* ``./run.sh doctor`` flags dev mode prominently.
* ``./run.sh status`` flags dev mode prominently.
* The bypass is **never** the default; the env var must be set
  explicitly per invocation (or via ``./run.sh start --dev`` which
  exports it to the subprocesses it spawns).

What dev mode does NOT bypass
-----------------------------

Dev mode scope is **SIP-related gates only**. Specifically:

* ``./run.sh`` ``sip_gate`` (called from ``cmd_start``,
  ``cmd_bootstrap``, every ``cmd_key`` action, ``configure-char``,
  ``cmd_vault``, ``cmd_config sign``, and ``cmd_yubikey``) —
  bypassed.
* ``sip_check.enforce_or_die()`` (called from ``asr_server``'s
  FastAPI lifespan, ``inspector_server``'s FastAPI lifespan, and
  ``python -m local_scribe.security.sip_check check``) — bypassed.
* ``python -m local_scribe.security.sip_check check`` — exits 0
  with a warning banner instead of nonzero.

Dev mode does **not** affect:

* ``script_integrity_gate`` — our own scripts must still match
  their baseline.
* ``char_integrity_gate`` — Char.app CDHash, Team ID, Bundle ID,
  linked libraries must still match the pinned baseline.
* ``pinned_config_gate`` — the operator HMAC over ``pinned.json``
  and ``char_baseline.json`` must still verify.
* ``secret_store.unlock_master_key`` — Touch ID + YubiKey are
  still required to unlock the master key (the only thing that's
  different is the kernel boundary protecting our heap from a
  cohabiting process).
* ``service_auth`` — the HKDF bearer-token gate on every
  ``/api/*`` endpoint still applies. ``LOCAL_SCRIBE_DISABLE_AUTH``
  is a separate, also-loud, also-explicit knob.

Operators who want to bypass *several* gates can combine env vars
(`LOCAL_SCRIBE_DEV_MODE=1 LOCAL_SCRIBE_DISABLE_AUTH=1`), but every
bypass surfaces its own warning so the cumulative "how degraded is
this run?" picture is auditable from the doctor banner alone.

Test seam
---------

Tests that need to exercise dev-mode logic set
``LOCAL_SCRIBE_DEV_MODE=1`` for the duration of the test (see
``tests/common/test_dev_mode.py``). The banner-emission helper is
idempotent at the process level: ``emit_banner_once`` records its
first emission in a module-level flag, so subsequent calls within
the same process are silent. ``reset_for_tests()`` clears that
flag so tests can re-assert the banner.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, TextIO


ENV_VAR: str = "LOCAL_SCRIBE_DEV_MODE"

# Values that mean "off" when the env var is set to one of them.
# Anything else (including ``"1"``, ``"true"``, ``"on"``, ``"yes"``,
# any non-empty other string) means "on". This matches the
# permissive style used by ``LOCAL_SCRIBE_DISABLE_AUTH`` and
# ``LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG`` elsewhere in the project.
_OFF_VALUES: frozenset[str] = frozenset({"", "0", "false", "no", "off"})


def is_enabled() -> bool:
    """``True`` iff the dev-mode env var is set to anything other
    than an off-value. Cheap (single env read); safe to call from
    hot paths."""
    return os.environ.get(ENV_VAR, "").strip().lower() not in _OFF_VALUES


# Module-level "have we printed the long banner this process?" flag.
# Each gate-bypass call invokes ``emit_banner_once`` which sets this
# to True on first call; subsequent calls in the same process get a
# short one-line "dev mode active" indicator instead of the full
# red wall. Keeps the operator-facing output loud-but-not-spammy.
_banner_emitted: bool = False


def format_banner(*, color: bool = True) -> str:
    """Render the multi-line dev-mode warning banner.

    ``color=True`` emits ANSI escapes for an interactive terminal;
    ``color=False`` emits plain text suitable for log files or
    capture-into-string consumers. The shape and structure of the
    banner are stable; callers can grep for the literal phrase
    ``"DEV MODE ACTIVE"`` to detect bypass conditions in CI logs.
    """
    if color:
        red = "\033[31m"
        red_bg = "\033[41m\033[97m"  # red background, bright white text
        bold = "\033[1m"
        dim = "\033[2m"
        reset = "\033[0m"
    else:
        red = red_bg = bold = dim = reset = ""

    bar = (
        f"{red}{bold}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        f"{reset}"
    )
    return "\n".join([
        bar,
        f"{red_bg}{bold}  !!  DEV MODE ACTIVE — SIP GATES ARE BYPASSED  !!  {reset}",
        bar,
        f"{red}{bold}This is NOT a safe production configuration.{reset}",
        "",
        f"{bold}What this means right now:{reset}",
        f"  • System Integrity Protection (SIP) state is {bold}not enforced{reset}.",
        f"    The pipeline will run even if SIP is off, partially off, or",
        f"    unverifiable. Read SECURITY.md § 'Defense layer 0' for what",
        f"    that costs you.",
        f"  • ``task_for_pid()`` against our processes may be unrestricted.",
        f"  • ``DYLD_INSERT_LIBRARIES`` may not be stripped from codesigned",
        f"    binaries we shell out to.",
        f"  • The reconstituted master key is {bold}readable from our heap{reset}",
        f"    by any user-space process for the duration of a Touch ID",
        f"    unlock.",
        "",
        f"{bold}What this does NOT bypass{reset} (every other gate still applies):",
        f"  • script_integrity   — our scripts must match their baseline.",
        f"  • char_integrity     — Char.app CDHash + Team ID + linked libs",
        f"                         must match the pinned baseline.",
        f"  • pinned_config      — the operator HMAC over pinned.json +",
        f"                         char_baseline.json must verify.",
        f"  • service_auth       — /api/* bearer-token gate still applies.",
        f"  • master-key unlock  — Touch ID + YubiKey still required.",
        "",
        f"{bold}How to exit dev mode{reset}",
        f"  1. ``./run.sh stop``",
        f"  2. unset {ENV_VAR}     (or remove --dev from your start command)",
        f"  3. ``./run.sh start``  (SIP gate will now hard-fail until SIP",
        f"                         is fully on — see SECURITY.md § Defense",
        f"                         layer 0 → 'How to fix')",
        "",
        f"{dim}This banner is generated by local_scribe/common/dev_mode.py.{reset}",
        f"{dim}It is intentionally loud + non-dismissible.{reset}",
        bar,
        "",
    ])


def emit_banner_once(stream: Optional[TextIO] = None, *, color: Optional[bool] = None) -> bool:
    """Print the long banner to ``stream`` (default stderr) on first
    call within this process. Returns ``True`` if the banner was
    actually emitted by this call, ``False`` if a previous call had
    already done so.

    ``color`` defaults to ``stream.isatty()`` so terminal output is
    coloured but redirected output stays plain — matches the
    convention used by ``sip_check.format_banner``.
    """
    global _banner_emitted
    if _banner_emitted:
        return False
    out = stream if stream is not None else sys.stderr
    use_color = color if color is not None else _is_tty(out)
    out.write(format_banner(color=use_color))
    try:
        out.flush()
    except Exception:  # noqa: BLE001 — stream may not support flush
        pass
    _banner_emitted = True
    return True


def short_indicator(*, color: bool = True) -> str:
    """One-line indicator suitable for embedding inside an existing
    log line (e.g. ``"sip_gate bypassed (dev mode active)"``). Does
    NOT consult or update the ``_banner_emitted`` flag — callers
    use this when they want a per-gate marker independent of the
    long banner."""
    if color:
        return "\033[31m\033[1m[DEV MODE]\033[0m"
    return "[DEV MODE]"


def reset_for_tests() -> None:
    """Clear the module-level "have we emitted the banner?" flag so
    a test can re-assert first-emit behaviour. Tests must call this
    in their ``setUp`` (or use the fixture) if they care about the
    one-shot semantics; production code never calls this."""
    global _banner_emitted
    _banner_emitted = False


def _is_tty(stream: TextIO) -> bool:
    """Defensive ``isatty()`` — some stream wrappers raise or return
    non-bool. Always returns a bool."""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False
