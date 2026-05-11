#!/usr/bin/env python3
"""Test fake of ``age-plugin-yubikey`` mirroring the 0.5.x CLI contract.

This script is used by integration tests that need to exercise the real
``subprocess`` boundary of ``local_scribe.security.yubikey_backup.enroll()``
without a physical YubiKey.

CLI surface modelled (matches the real ``age-plugin-yubikey --help`` for
the 0.5.x line we pin in CI):

  -g, --generate                    Generate a new YubiKey identity.
  -i, --identity                    Print identities stored in connected YubiKeys.
  -l, --list                        List recipients for identities in connected YubiKeys.
  --list-all                        List recipients for all keys on connected YubiKeys.
  -f, --force                       Force --generate to overwrite a filled slot.
  --name NAME                       Name for the generated identity.
  --pin-policy {always,once,never}  Defaults to 'once'.
  --serial SERIAL                   Specify which YubiKey to use.
  --slot SLOT                       Specify which slot (1..20). Defaults to first usable.
  --touch-policy {always,cached,never}  Defaults to 'always'.
  -V, --version                     Print version info and exit.
  -h, --help                        Print help.

Flags REJECTED with rc=2 (regression catchers — these don't exist in
0.5.x, despite stale docs that mentioned them):

  --identity-output PATH

Identity stub format (mirrors real 0.5.x output captured 2026-05-11
from a live YubiKey, including the variable internal whitespace that
broke our prefix-based parser):

    Recipient: age1yubikey1...
    #       Serial: 16366413, Slot: 1
    #         Name: local_scribe
    #      Created: Mon, 11 May 2026 22:24:55 +0000
    #   PIN policy: Never  (A PIN is NOT required to decrypt)
    # Touch policy: Always (A physical touch is required for every decryption)
    #    Recipient: age1yubikey1...
    AGE-PLUGIN-YUBIKEY-1FKALJQYZVLYJ4JCGWZLHT

Slot-state tracking (optional, enables recovery-path tests):
If ``LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR`` is set, ``--generate``
writes a ``slot_<N>.json`` file with the enrollment metadata. A
second ``--generate --slot N`` without ``--force`` then fails with
the real plugin's "Slot N is not empty" error. ``--identity --slot N``
reads from the same state to reproduce a populated slot.

Knobs (via env vars, all optional):
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT
        Override the emitted ``age1yubikey1...`` string. Default is a
        valid-looking 60-char placeholder.
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_SERIAL
        Override the ``# Serial:`` value. Default ``16366413``.
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_REQUIRE_TTY
        If ``1``, require stdin.isatty() and emit the real-plugin's
        "Error: Failed to get input from user: IO error: not a terminal"
        + exit 1 otherwise. Used by the TTY-inheritance regression test.
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR
        Directory for persistent slot state. Enables the "Slot N is not
        empty" rc=1 error on duplicate --generate, and lets --identity
        return whatever --generate previously wrote.
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_TRACE
        If set, append the argv (one per line) plus ``[stdin_tty=...]``
        to this file. Lets tests assert on what we invoked the plugin with.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_VERSION = "age-plugin-yubikey 0.5.1"
_DEFAULT_RECIPIENT = (
    "age1yubikey1qf3s28pdpz2gv0kpqcfvfn3a59lnzlnzgn5z9nzxqjvgg7cssyru06lpts9"
)
_DEFAULT_SERIAL = "16366413"

_REJECTED_FLAGS = {
    # These were never in 0.5.x. If we re-add them on the caller side,
    # the fake errors exactly like the real plugin does — which is
    # what bit us in production on 2026-05-11.
    "--identity-output",
}


def _trace(argv: list[str]) -> None:
    path = os.environ.get("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fp:
            for a in argv:
                fp.write(a + "\n")
            fp.write(f"[stdin_tty={sys.stdin.isatty()}]\n")
            fp.write("---\n")
    except OSError:
        pass


def _state_path(slot: str) -> Path | None:
    base = os.environ.get("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR")
    if not base:
        return None
    return Path(base) / f"slot_{slot}.json"


def _read_slot(slot: str) -> dict | None:
    path = _state_path(slot)
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_slot(slot: str, data: dict) -> None:
    path = _state_path(slot)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _format_stub(slot: str, recipient: str, serial: str,
                 name: str, pin_policy: str, touch_policy: str) -> str:
    """Emit a realistic 0.5.x identity stub.

    The whitespace columns are *significant* — they are what tripped
    up the prefix-based parser on 2026-05-11. The real plugin
    right-aligns the field labels in a column, producing e.g.
    ``#    Recipient:`` (four spaces) rather than ``# Recipient:``
    (one space). Any regression in the upstream alignment would be
    caught by parsers that look for substring ``Recipient:`` instead
    of an exact prefix.
    """
    pin_human = {
        "always": "Always (A PIN is required for every decryption)",
        "once":   "Once   (A PIN is required once per session, if set)",
        "never":  "Never  (A PIN is NOT required to decrypt)",
    }.get(pin_policy, pin_policy)
    touch_human = {
        "always": "Always (A physical touch is required for every decryption)",
        "cached": "Cached (A physical touch is cached for 15 seconds)",
        "never":  "Never  (A physical touch is NOT required)",
    }.get(touch_policy, touch_policy)
    return (
        f"Recipient: {recipient}\n"
        f"#       Serial: {serial}, Slot: {slot}\n"
        f"#         Name: {name}\n"
        f"#      Created: Mon, 11 May 2026 22:24:55 +0000\n"
        f"#   PIN policy: {pin_human}\n"
        f"# Touch policy: {touch_human}\n"
        f"#    Recipient: {recipient}\n"
        f"AGE-PLUGIN-YUBIKEY-1QWERTYUIOPASDFGHJKLZXCVBNM\n"
    )


def _print_help() -> None:
    sys.stdout.write(
        "Usage: age-plugin-yubikey [OPTIONS]\n\n"
        "Optional arguments:\n"
        "  -h, --help                  Print this help message and exit.\n"
        "  -V, --version               Print version info and exit.\n"
        "  -f, --force                 Force --generate to overwrite a filled slot.\n"
        "  -g, --generate              Generate a new YubiKey identity.\n"
        "  -i, --identity              Print identities stored in connected YubiKeys.\n"
        "  -l, --list                  List recipients for age identities in connected YubiKeys.\n"
        "  --list-all                  List recipients for all keys on connected YubiKeys.\n"
        "  --name NAME                 Name for the generated identity.\n"
        "  --pin-policy PIN-POLICY     One of [always, once, never].\n"
        "  --serial SERIAL             Specify which YubiKey to use.\n"
        "  --slot SLOT                 Specify which slot to use.\n"
        "  --touch-policy POLICY       One of [always, cached, never].\n"
    )


def _parse_args(argv: list[str]) -> dict:
    out = {
        "slot": "1",
        "pin_policy": "once",
        "touch_policy": "always",
        "name": "age identity",
        "force": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--slot" and i + 1 < len(argv):
            out["slot"] = argv[i + 1]
            i += 2
            continue
        if a == "--pin-policy" and i + 1 < len(argv):
            out["pin_policy"] = argv[i + 1]
            i += 2
            continue
        if a == "--touch-policy" and i + 1 < len(argv):
            out["touch_policy"] = argv[i + 1]
            i += 2
            continue
        if a == "--name" and i + 1 < len(argv):
            out["name"] = argv[i + 1]
            i += 2
            continue
        if a in ("--force", "-f"):
            out["force"] = True
            i += 1
            continue
        i += 1
    return out


def main(argv: list[str]) -> int:
    _trace(argv)

    if not argv:
        _print_help()
        return 2

    for bad in _REJECTED_FLAGS:
        if bad in argv:
            sys.stderr.write(
                f"age-plugin-yubikey: unrecognized option `{bad}`\n"
            )
            return 2

    if "--version" in argv or "-V" in argv:
        sys.stdout.write(_VERSION + "\n")
        return 0
    if "--help" in argv or "-h" in argv:
        _print_help()
        return 0

    args = _parse_args(argv)
    default_recipient = os.environ.get(
        "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", _DEFAULT_RECIPIENT
    )
    default_serial = os.environ.get(
        "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_SERIAL", _DEFAULT_SERIAL
    )

    if "--identity" in argv or "-i" in argv:
        existing = _read_slot(args["slot"])
        if existing is None:
            # If state tracking is off, fall through to a default
            # identity stub. If state tracking is on and the slot
            # is empty, mimic the real plugin's empty-output exit.
            if _state_path(args["slot"]) is not None:
                return 0
            sys.stdout.write(
                _format_stub(
                    slot=args["slot"],
                    recipient=default_recipient,
                    serial=default_serial,
                    name="age identity",
                    pin_policy="never",
                    touch_policy="always",
                )
            )
            return 0
        sys.stdout.write(
            _format_stub(
                slot=existing["slot"],
                recipient=existing["recipient"],
                serial=existing["serial"],
                name=existing.get("name", "age identity"),
                pin_policy=existing.get("pin_policy", "never"),
                touch_policy=existing.get("touch_policy", "always"),
            )
        )
        return 0

    if "--list" in argv or "-l" in argv or "--list-all" in argv:
        existing = _read_slot(args["slot"])
        recipient = existing["recipient"] if existing else default_recipient
        sys.stdout.write(recipient + "\n")
        return 0

    if "--generate" in argv or "-g" in argv:
        sys.stderr.write("🎲 Generating key...\n")

        if os.environ.get("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_REQUIRE_TTY") == "1":
            if not sys.stdin.isatty():
                sys.stderr.write(
                    "\nError: Failed to get input from user: IO error: not a terminal\n"
                    "\n"
                    "[ Did this not do what you expected? Could an error be more useful? ]\n"
                    "[ Tell us: https://str4d.xyz/age-plugin-yubikey/report              ]\n"
                )
                return 1

        # Slot-state collision: replicate the real plugin's
        # "Slot N is not empty. Use --force to overwrite the slot." rc=1
        # so the recovery path in yubikey_backup.enroll() gets exercised.
        existing = _read_slot(args["slot"])
        if existing is not None and not args["force"]:
            sys.stderr.write(
                f"Error: Slot {args['slot']} is not empty. "
                "Use --force to overwrite the slot.\n"
            )
            return 1

        recipient = default_recipient
        serial = default_serial
        stub = _format_stub(
            slot=args["slot"],
            recipient=recipient,
            serial=serial,
            name=args["name"],
            pin_policy=args["pin_policy"],
            touch_policy=args["touch_policy"],
        )
        _write_slot(args["slot"], {
            "slot": args["slot"],
            "recipient": recipient,
            "serial": serial,
            "name": args["name"],
            "pin_policy": args["pin_policy"],
            "touch_policy": args["touch_policy"],
        })
        sys.stdout.write(stub)
        return 0

    _print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
