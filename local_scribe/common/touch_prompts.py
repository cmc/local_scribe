"""Loud terminal banners that fire right before a Touch ID modal or
a YubiKey blocking-wait.

Why this module exists
----------------------

The split-key unlock flow (``key_lifecycle.unlock_master_key``)
blocks on two physical events back-to-back:

  1. A macOS Touch ID modal pops asking for the operator's
     fingerprint. The modal is a system window, NOT a terminal prompt.
     If the operator isn't watching their screen, the terminal looks
     frozen.

  2. The age plugin starts a YubiKey decrypt and the YubiKey's
     touch sensor begins flashing. The flash is small and easy to
     miss in daylight or with the key plugged in behind the laptop.
     If the operator doesn't tap within 60 s, the decrypt times out
     and the whole pipeline-start fails with an opaque error.

Before this module existed, the *only* signal the operator got was
"the terminal is frozen for a few seconds, then nothing happens for
60 s, then a YubiKey-touch-timeout error". This caused six
self-reported "is it hung?" moments in a single afternoon on
2026-05-11. Each of those was a fast-feedback gap, not a real bug.

This module is the fix: an explicit, multi-line, color-coded banner
printed to stderr immediately before each blocking step. The
banners describe WHAT the operator should look for and on WHICH
device. They're loud on purpose (matching the dev-mode banner
style in ``local_scribe/common/dev_mode.py``) so a glance at the
terminal is enough to know what's needed next.

What this module does NOT do
----------------------------

* It does not modify the underlying blocking primitives. The Touch
  ID modal still pops where it always did; the YubiKey-tap-wait
  still uses ``age -d -i identity.txt`` with a 60 s subprocess
  timeout. Only the *human-facing pre-flight* gets a banner.

* It does not interact with the OS Keychain or with
  ``age-plugin-yubikey``. It's stderr printing, nothing more.

* It does not retry, swallow errors, or change the failure modes
  callers see if the operator declines Touch ID or fails to tap.
  Those errors continue to propagate as ``SecretStoreError`` and
  ``YubiKeyTouchTimeoutError`` exactly as before.

Test seam
---------

* ``LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS=1`` suppresses banner output
  entirely. Used by integration tests that exercise the unlock flow
  against a fake Keychain — the banners would just spam pytest
  output without conveying anything useful.

* ``color`` defaults to ``stream.isatty()`` so terminal output is
  coloured but capture-into-string consumers (tests, CI log
  pipelines) stay plain. The literal phrases ``"TOUCH ID"`` and
  ``"TAP YOUR YUBIKEY"`` are stable and grep-able from CI logs.

Wiring
------

The canonical wire-up is in
``local_scribe.security.key_lifecycle.unlock_master_key``:

* ``print_touch_id_imminent(...)`` fires immediately before
  ``secret_store.load_kc_half(...)``.

* The default ``on_touch_prompt`` argument is wired to
  ``cli_yubikey_prompt``, so the YubiKey banner fires right when
  the age subprocess begins its decrypt-with-tap.

Callers that drive the unlock primitives directly (tests, the
key-init / key-rotate / DR paths) get the same banners by virtue
of routing through ``unlock_master_key``. Lower-level call sites
that want their own prompt can pass ``on_touch_prompt=`` explicitly
to override.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, TextIO


# Env var that suppresses ALL output from this module. Used by
# integration tests that exercise the unlock primitives against
# fakes. Production callers should not set this — the banners are
# the entire point.
QUIET_ENV_VAR: str = "LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS"

# Quiet values match dev_mode.py's convention so the operator only
# has to remember one set of "this is off" strings.
_OFF_VALUES: frozenset[str] = frozenset({"", "0", "false", "no", "off"})


def _is_quiet() -> bool:
    return os.environ.get(QUIET_ENV_VAR, "").strip().lower() not in _OFF_VALUES


def _is_tty(stream: TextIO) -> bool:
    """Defensive ``isatty()`` — some stream wrappers raise or return
    non-bool. Always returns a bool."""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False


def _colors(enabled: bool) -> dict[str, str]:
    if not enabled:
        return {k: "" for k in (
            "yellow", "yellow_bg", "red", "red_bg", "bold", "dim", "reset"
        )}
    return {
        "yellow": "\033[33m",
        "yellow_bg": "\033[43m\033[30m",  # yellow background, black text
        "red": "\033[31m",
        "red_bg": "\033[41m\033[97m",     # red background, bright white text
        "bold": "\033[1m",
        "dim": "\033[2m",
        "reset": "\033[0m",
    }


# ---------------------------------------------------------------------------
# Banner formatters (pure — easy to unit-test).

def format_touch_id_banner(reason: str, *, color: bool = True) -> str:
    """Render the Touch ID pre-flight banner.

    ``reason`` is a short phrase ("Unlock local_scribe vault",
    "Sign local_scribe pinned config", etc.) that goes into the
    banner's "We need Touch ID to: ..." line so the operator knows
    WHY a modal is about to interrupt them.
    """
    c = _colors(color)
    bar = (
        f"{c['yellow']}{c['bold']}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        f"{c['reset']}"
    )
    return "\n".join([
        "",
        bar,
        f"{c['yellow_bg']}{c['bold']}  ▶  TOUCH ID PROMPT INCOMING  ◀  {c['reset']}",
        bar,
        f"{c['bold']}Look at your screen.{c['reset']} macOS is about to show a "
        f"Touch ID dialog.",
        f"  We need Touch ID to: {c['bold']}{reason}{c['reset']}",
        f"  Rest your finger on the sensor (or click {c['bold']}Use Password…"
        f"{c['reset']} to type your login password).",
        f"{c['dim']}If you don't see a dialog: check behind other windows. "
        f"It may be on a different Space.{c['reset']}",
        bar,
        "",
    ])


def format_yubikey_banner(message: str, *, color: bool = True) -> str:
    """Render the YubiKey-tap pre-flight banner.

    ``message`` is the human-readable phrase passed by the caller
    (typically ``"Please touch your YubiKey to decrypt the
    local_scribe yk_half."``). We embed it verbatim under the
    banner heading so the operator can still see WHICH key half is
    being decrypted if they care.
    """
    c = _colors(color)
    bar = (
        f"{c['red']}{c['bold']}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        f"{c['reset']}"
    )
    return "\n".join([
        "",
        bar,
        f"{c['red_bg']}{c['bold']}  ▶  TAP YOUR YUBIKEY NOW  ◀  {c['reset']}",
        bar,
        f"{c['bold']}Look at your YubiKey.{c['reset']} The metal contact / "
        f"touch sensor should be {c['bold']}flashing green{c['reset']}.",
        f"  Press it now (a brief touch is enough).",
        f"  {c['dim']}{message}{c['reset']}",
        "",
        f"{c['dim']}If nothing is flashing: confirm the YubiKey is inserted "
        f"and re-seat it.{c['reset']}",
        f"{c['dim']}Timeout: 60 s. After that the unlock fails and you'll "
        f"need to re-run the command.{c['reset']}",
        bar,
        "",
    ])


# ---------------------------------------------------------------------------
# Side-effecting helpers (the ones callers actually invoke).

def print_touch_id_imminent(
    reason: str,
    *,
    stream: Optional[TextIO] = None,
    color: Optional[bool] = None,
) -> None:
    """Write the Touch ID banner to ``stream`` (default stderr).

    Callers invoke this immediately before any code path that
    blocks on a macOS Touch ID modal — typically
    ``secret_store.load_kc_half(...)`` or anything that proxies
    through it. The banner contains no actionable command, only
    a heads-up for the operator.

    Silent if ``LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS`` is set
    (integration tests, fake-Keychain harnesses).
    """
    if _is_quiet():
        return
    out = stream if stream is not None else sys.stderr
    use_color = color if color is not None else _is_tty(out)
    try:
        out.write(format_touch_id_banner(reason, color=use_color))
        out.flush()
    except Exception:  # noqa: BLE001 — stderr may not support flush
        pass


def cli_yubikey_prompt(
    message: str,
    *,
    stream: Optional[TextIO] = None,
    color: Optional[bool] = None,
) -> None:
    """Default ``on_touch_prompt`` for the YubiKey-tap blocking step.

    Signature matches what
    ``yubikey_backup._age_decrypt(..., on_touch_prompt=...)``
    expects: a single-string callable that takes the human-readable
    "please touch your YubiKey" line composed inside the unlock
    primitive and surfaces it to the operator however the
    embedding UI wants.

    For CLI / terminal callers (which is everyone right now) this
    means: print the red banner to stderr with ``message`` embedded.

    Silent if ``LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS`` is set.
    """
    if _is_quiet():
        return
    out = stream if stream is not None else sys.stderr
    use_color = color if color is not None else _is_tty(out)
    try:
        out.write(format_yubikey_banner(message, color=use_color))
        out.flush()
    except Exception:  # noqa: BLE001
        pass
