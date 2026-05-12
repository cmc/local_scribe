"""macOS TCC attribution probe for the running Char.app.

Background
==========

macOS Transparency, Consent, and Control (TCC) decides which app
bundle is *responsible* for a given resource request (microphone,
system audio capture, screen recording, ...). TCC walks the process-
launch chain and picks a "responsible" bundle. Permissions are checked
against THAT bundle, not necessarily the one issuing the syscall.

For the 2026-05 audio-silent-fail bug in :mod:`run.sh`'s
``cmd_char_launch``, every Char permission request was being
attributed to the launching terminal (``com.googlecode.iterm2`` /
``com.apple.Terminal``) because Char was started via
``exec /usr/bin/sandbox-exec ... hyprnote`` from inside a terminal.
Terminals don't have ``kTCCServiceAudioCapture`` consent, so
system audio capture (other speakers) was silently denied even
though Char.app's own ``NSAudioCaptureUsageDescription`` was granted.

The fix in run.sh (``open -a Char.app --env ...``) makes Char.app
its own responsible bundle. This module reads recent ``tccd`` logs
to verify that the fix actually held, and to surface a regression
loudly if a future change re-introduces the bug.

What it actually does
=====================

Calls

    log show --predicate 'process == "tccd"' --info --debug \\
             --last <N>m --style ndjson

and walks the JSON event stream for the most recent
``AttributionChain`` referring to Char's bundle identifier
(``com.hyprnote.stable``). The chain looks like

    [
      {"identifier":"com.hyprnote.stable","reason":"requesting"},
      {"identifier":"com.hyprnote.stable","reason":"instigating"},
      {"identifier":"com.googlecode.iterm2","reason":"responsible"}  # ← bug
    ]

We extract the "responsible" entry and report ``ok`` if it's
Char.app, ``terminal`` if it's a known terminal emulator, and
``no_events`` / ``no_logs`` / ``unknown`` for the diagnostic
states. The CLI form prints a tiny JSON blob that ``run.sh``'s
``cmd_char_firewall_status`` consumes.

Limitations
-----------

* Requires ``/usr/bin/log`` (always present on macOS).
* Only inspects the last 30 minutes by default; if Char hasn't asked
  for any TCC-gated resource recently the result is ``no_events``.
* The ``responsible`` field exists on every macOS where TCC has
  process attribution (10.15+). On older / nonstandard log shapes we
  fall back to ``unknown`` and never assert failure.
* The probe is read-only and runs in <2s in the common case; it's
  designed to be cheap enough to call from every
  ``./run.sh char firewall-status`` and from the inspector audit panel.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger("local_scribe.char_tcc_probe")


# Char's Tauri-app bundle identifier. Stable across the version pins
# we ship; lives next to all the other Char-recognising magic
# strings (char_audit / char_integrity).
CHAR_BUNDLE_ID = "com.hyprnote.stable"


# Known terminal emulator bundle IDs we want to flag as a
# "responsible_path is a terminal" regression. macOS doesn't enumerate
# these for us; this is the empirically-observed set on Apple Silicon
# Macs as of 2026-05. Operators on niche terminals (Warp, Alacritty,
# kitty, WezTerm, etc.) hit the same TCC attribution bug, so we accept
# any bundle whose ID starts with a known "this is a terminal" prefix
# OR whose name matches a terminal-emulator regex.
TERMINAL_BUNDLE_IDS = {
    "com.googlecode.iterm2",
    "com.apple.Terminal",
    "co.zeit.hyperterm",          # Hyper
    "co.zeit.hyper",
    "dev.warp.Warp",
    "dev.warp.Warp-Stable",
    "io.alacritty",
    "net.kovidgoyal.kitty",
    "com.github.wez.wezterm",
    "com.tabby.Tabby",
}

_TERMINAL_NAME_RE = re.compile(
    r"(iterm|terminal|hyper|warp|alacritty|kitty|wezterm|tabby)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Outcome of a single :func:`probe` call. Mirrors the JSON shape
    consumed by run.sh's ``cmd_char_firewall_status``."""

    # State machine:
    #   "ok"        - responsible == Char.app (system audio capture
    #                 will be granted directly to Char's bundle)
    #   "terminal"  - responsible IS a terminal emulator (the May
    #                 2026 regression); other-speaker capture WILL
    #                 be silently denied
    #   "no_events" - no tccd events for Char in the lookback window
    #                 (Char hasn't asked for a TCC-gated resource
    #                 recently; can't tell either way)
    #   "no_logs"   - /usr/bin/log unavailable or returned no output
    #   "unknown"   - log format didn't match what we expect; safest
    #                 to neither pass nor fail
    state: str
    responsible: str = "-"
    # Number of distinct tccd events we examined; useful for
    # operators triaging "why does this say no_events?" -- 0 means
    # no Char events at all.
    events_seen: int = 0
    # Lookback window in minutes that was queried. Echoed back so
    # callers can adjust if they want a longer history.
    lookback_minutes: int = 0
    # Free-text diagnostic for tooling; never an assertion key.
    note: str = ""
    # Raw responsible-chain entry retained for ``--verbose`` mode and
    # for the bundled unit tests; not stable enough to be a public
    # contract.
    raw_chain: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "responsible": self.responsible,
            "events_seen": self.events_seen,
            "lookback_minutes": self.lookback_minutes,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without running `log show`)
# ---------------------------------------------------------------------------


def is_terminal_identifier(identifier: str | None) -> bool:
    """Return True if ``identifier`` belongs to a known terminal
    emulator. Used to classify the ``responsible`` slot of a tccd
    AttributionChain. The match is intentionally loose so niche
    terminals not in the exact set still trip the regression
    detector."""
    if not identifier:
        return False
    if identifier in TERMINAL_BUNDLE_IDS:
        return True
    return bool(_TERMINAL_NAME_RE.search(identifier))


def classify_chain(chain: list[dict[str, Any]]) -> tuple[str, str]:
    """Given a single tccd AttributionChain (a list of
    ``{identifier, reason}`` dicts), return ``(state, responsible)``.

    The contract:
      * If the chain has a ``reason=responsible`` entry pointing at
        Char's bundle -> ``("ok", "com.hyprnote.stable")``.
      * If the responsible entry is a terminal emulator
        -> ``("terminal", "<their bundle id>")`` -- this is the May
        2026 regression signature.
      * Anything else (including missing fields, unknown bundles)
        -> ``("unknown", responsible_or_'-')``.

    Never raises. Malformed chains return ``("unknown", "-")``.
    """
    if not chain:
        return "unknown", "-"
    # Scan all entries and prefer the LAST ``reason=responsible`` slot
    # that has a non-empty ``identifier``. Real tccd chains only ever
    # carry one such entry, so this matches the canonical case
    # exactly; the "prefer last with a non-empty identifier" rule is
    # the defensive degradation path for the rare case where macOS
    # emits a partially-populated chain (observed under heavy load
    # during XPC reconnects, where the first responsible entry can be
    # a placeholder with no identifier).
    responsible_entry = None
    for entry in chain:
        if not isinstance(entry, dict):
            continue
        if entry.get("reason") != "responsible":
            continue
        if not entry.get("identifier"):
            # Keep the placeholder around in case it's the only one
            # we find, but keep looking for one with an identifier.
            if responsible_entry is None:
                responsible_entry = entry
            continue
        responsible_entry = entry
    if responsible_entry is None:
        return "unknown", "-"
    ident = responsible_entry.get("identifier") or ""
    if ident == CHAR_BUNDLE_ID:
        return "ok", ident
    if is_terminal_identifier(ident):
        return "terminal", ident
    # Some other bundle -- could be SystemUIServer / Spotlight /
    # ScreenCaptureKit helper / etc. We don't classify as ok, but we
    # also don't fire the regression alarm. Mark as unknown so
    # firewall-status surfaces a neutral '•' rather than a red ✗.
    return "unknown", ident or "-"


# The ``message`` field of a tccd event embeds the AttributionChain
# inline as a python-repr / printf-formatted string rather than a
# JSON sub-document. Pattern shape:
#
#   "AttributionChain={
#       [identifier=com.hyprnote.stable,
#        pid=42861,
#        auid=501,
#        euid=501,
#        binary_path=/Applications/Char.app/...]
#       [identifier=com.googlecode.iterm2,
#        pid=2741,
#        ...
#        reason=responsible]
#   }"
#
# We pull each ``[ ... ]`` block out and look for the ``reason=...``
# slot inside it. Robust against subtle format changes between macOS
# versions because we never assume a fixed column ordering.
_BLOCK_RE = re.compile(r"\[([^\[\]]+)\]")
_KV_RE = re.compile(r"(\w+)=([^,\s]+(?:[^,]*[^,\s])?)")


def parse_attribution_chain(message: str) -> list[dict[str, Any]]:
    """Extract a list of ``{identifier, reason}`` dicts from a raw
    tccd log ``message`` string. Returns an empty list if no
    AttributionChain block is found.

    Tolerant of:
      * key order
      * extra whitespace / newlines (log show wraps long lines)
      * missing fields (entries without a reason are still returned
        so :func:`classify_chain` can decide what to do with them)
    """
    if "AttributionChain" not in message:
        return []
    chain: list[dict[str, Any]] = []
    for block in _BLOCK_RE.findall(message):
        entry: dict[str, Any] = {}
        for k, v in _KV_RE.findall(block):
            entry[k] = v
        if entry:
            chain.append(entry)
    return chain


# ---------------------------------------------------------------------------
# log show driver
# ---------------------------------------------------------------------------


def _log_binary_available() -> bool:
    return os.path.isfile("/usr/bin/log") and os.access("/usr/bin/log", os.X_OK)


def _run_log_show(lookback_minutes: int, timeout: float) -> str | None:
    """Invoke ``/usr/bin/log show`` and return its stdout, or ``None``
    on any failure. Errors are logged at INFO so an operator can
    follow up but never raise -- the probe must degrade to
    ``no_logs`` rather than blow up firewall-status."""
    if not _log_binary_available():
        return None
    cmd = [
        "/usr/bin/log", "show",
        "--predicate", 'process == "tccd"',
        "--info", "--debug",
        "--last", f"{lookback_minutes}m",
        "--style", "ndjson",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("log show failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.info("log show rc=%s stderr=%r",
                    proc.returncode, proc.stderr[:400])
        return None
    return proc.stdout


def probe(*, lookback_minutes: int = 30,
          timeout: float = 5.0) -> ProbeResult:
    """Query the unified log for the most recent tccd
    AttributionChain referring to Char and classify it.

    Returns a :class:`ProbeResult` -- never raises. Callers that need
    a JSON blob to embed in run.sh can use :func:`main`.
    """
    raw = _run_log_show(lookback_minutes, timeout)
    if raw is None:
        return ProbeResult(
            state="no_logs",
            lookback_minutes=lookback_minutes,
            note="/usr/bin/log unavailable or returned non-zero",
        )

    events_seen = 0
    last_chain: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = evt.get("eventMessage") or evt.get("message") or ""
        if CHAR_BUNDLE_ID not in msg:
            continue
        chain = parse_attribution_chain(msg)
        if not chain:
            continue
        events_seen += 1
        # Keep the LATEST chain (ndjson is chronological). We need the
        # most recent attribution because operators may have launched
        # Char both ways during a debugging session.
        last_chain = chain

    if events_seen == 0:
        return ProbeResult(
            state="no_events",
            lookback_minutes=lookback_minutes,
            note=f"no tccd events mentioning {CHAR_BUNDLE_ID} in the "
                 f"last {lookback_minutes}m",
        )

    state, responsible = classify_chain(last_chain)
    return ProbeResult(
        state=state,
        responsible=responsible,
        events_seen=events_seen,
        lookback_minutes=lookback_minutes,
        raw_chain=last_chain,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m local_scribe.egress.char_tcc_probe`` entry point.

    Emits the probe result as a single-line JSON object on stdout so
    ``run.sh`` can parse it with the existing python -c json.loads
    pattern. Exit status:

      0  ``state`` is one of (ok, no_events, no_logs, unknown)
      1  ``state`` is "terminal" -- the May 2026 regression has
         resurfaced and is actively breaking system audio capture.
    """
    if argv is None:
        argv = sys.argv[1:]
    lookback = 30
    if argv:
        try:
            lookback = max(1, int(argv[0]))
        except ValueError:
            print(f"usage: python -m local_scribe.egress.char_tcc_probe "
                  f"[LOOKBACK_MINUTES]", file=sys.stderr)
            return 2
    res = probe(lookback_minutes=lookback)
    print(json.dumps(res.to_dict()))
    return 1 if res.state == "terminal" else 0


if __name__ == "__main__":
    raise SystemExit(main())
