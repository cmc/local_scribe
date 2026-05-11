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

Behaviour on ``--generate`` happy path:
  * Emits "🎲 Generating key...\\n" to stderr (the real plugin does this).
  * Emits a 0.5.x-shaped identity stub to stdout:
        # Created: 2026-05-11 14:00:00 UTC
        # Serial: <serial>, Slot: <slot>
        # Access policy: pin policy=<p>, touch policy=<t>
        # Recipient: age1yubikey1...
        AGE-PLUGIN-YUBIKEY-1QWERTYUIOPASDFGHJKLZXCVBNM
  * Exits 0.

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
  LOCAL_SCRIBE_FAKE_AGE_PLUGIN_TRACE
        If set, append the argv (one per line) plus ``[stdin_tty=...]``
        to this file. Lets tests assert on what we invoked the plugin with.
"""

from __future__ import annotations

import os
import sys

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

    if "--identity" in argv or "-i" in argv:
        recipient = os.environ.get(
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", _DEFAULT_RECIPIENT
        )
        serial = os.environ.get(
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_SERIAL", _DEFAULT_SERIAL
        )
        sys.stdout.write(f"# Serial: {serial}, Slot: 1\n")
        sys.stdout.write(f"# Recipient: {recipient}\n")
        sys.stdout.write("AGE-PLUGIN-YUBIKEY-1QWERTYUIOPASDFGHJKLZXCVBNM\n")
        return 0

    if "--list" in argv or "-l" in argv or "--list-all" in argv:
        recipient = os.environ.get(
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", _DEFAULT_RECIPIENT
        )
        sys.stdout.write(recipient + "\n")
        return 0

    if "--generate" in argv or "-g" in argv:
        # Find the slot / policies (default to plugin's defaults if not
        # specified — the real CLI is permissive about ordering).
        slot = "1"
        pin_policy = "once"
        touch_policy = "always"
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--slot" and i + 1 < len(argv):
                slot = argv[i + 1]
                i += 2
                continue
            if a == "--pin-policy" and i + 1 < len(argv):
                pin_policy = argv[i + 1]
                i += 2
                continue
            if a == "--touch-policy" and i + 1 < len(argv):
                touch_policy = argv[i + 1]
                i += 2
                continue
            i += 1

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

        recipient = os.environ.get(
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", _DEFAULT_RECIPIENT
        )
        serial = os.environ.get(
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_SERIAL", _DEFAULT_SERIAL
        )

        sys.stdout.write("# Created: 2026-05-11 14:00:00 UTC\n")
        sys.stdout.write(f"# Serial: {serial}, Slot: {slot}\n")
        sys.stdout.write(
            f"# Access policy: pin policy={pin_policy}, touch policy={touch_policy}\n"
        )
        sys.stdout.write(f"# Recipient: {recipient}\n")
        sys.stdout.write("AGE-PLUGIN-YUBIKEY-1QWERTYUIOPASDFGHJKLZXCVBNM\n")
        return 0

    _print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
