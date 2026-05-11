"""Tests for secret_store.py.

We test the Python wrapper against a *fake* Swift helper -- a tiny shell
script that mimics the documented exit codes + stdin/stdout contract.
The real Swift binary requires a Touch ID prompt to load a key, so unit
tests can't drive it end-to-end without human interaction. The live
end-to-end path is exercised via ``./run.sh vault init`` (see the
README live-test procedure).

The fake helper covers:
    - `exists` returning 0/2
    - `store` reading hex from stdin, writing it to a state file
    - `load` printing hex from the state file
    - `delete` removing the state file
    - All four error exits (1/2/3/4/5)

Replacing the helper is done via ``LOCAL_SCRIBE_TOUCHID_HELPER`` env var,
which the module honours in ``helper_path()``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from local_scribe.security import secret_store
from local_scribe.security.secret_store import (
    ACCOUNT_KC_HALF_V2,
    ACCOUNT_LEGACY_V1,
    HelperMissingError,
    KEY_BYTES,
    MasterKey,
    SecretStoreError,
    UserCancelledError,
    delete_kc_half,
    delete_master_key,
    generate_master_key,
    has_kc_half,
    has_master_key,
    helper_path,
    load_kc_half,
    load_master_key,
    store_kc_half,
    store_master_key,
)


# ---------- fake helper ----------------------------------------------


def _write_fake_helper(tmp: Path, *, behavior: str = "happy") -> Path:
    """Write a shell script that emulates the documented Swift helper
    contract. ``behavior`` selects which failure mode (if any) to
    simulate -- "happy" implements the full contract; "missing_item"
    always returns 2 on load; "cancelled" returns 3; etc.

    State (the "stored key") lives in ``$STATE_FILE``, a sibling of the
    script. So the wrapper tests can verify round-trip behaviour."""
    script = tmp / "touchid-keychain"
    state = tmp / "stored.hex"

    # NOTE: we deliberately avoid `set -e`. The real Swift helper reads
    # stdin via `readLine(strippingNewline: true)` which tolerates input
    # without a trailing newline (which is what secret_store.py sends).
    # Bash's `read` returns 1 on EOF-without-newline, which under `set -e`
    # kills the script before we get a chance to validate. Using a plain
    # `read || true` reproduces Swift's tolerance.
    #
    # The fake helper supports the same ``[--account NAME] <cmd>``
    # surface as the real Swift binary: when ``--account NAME`` is
    # present we map to a per-account state file
    # (``stored.<NAME>.hex``); otherwise we fall back to a default
    # ``stored.hex`` for backward-compatibility with pre-Option-C tests.
    bodies = {
        "happy": f"""
            DEFAULT_STATE={state}
            STATE_DIR=$(dirname "$DEFAULT_STATE")
            ACCOUNT=master_key
            if [ "$1" = "--account" ]; then
              ACCOUNT="$2"; shift 2
              STATE_FILE="$STATE_DIR/stored.$ACCOUNT.hex"
            else
              STATE_FILE="$DEFAULT_STATE"
            fi
            case "$1" in
              exists)
                if [ -f "$STATE_FILE" ]; then exit 0; else exit 2; fi
                ;;
              store)
                LINE=$(cat)
                [ -n "$LINE" ] || {{ echo "error: no input on stdin" >&2; exit 1; }}
                if ! printf "%s" "$LINE" | grep -Eq '^[0-9a-fA-F]+$'; then
                  echo "error: invalid hex on stdin" >&2; exit 5
                fi
                if [ $(( ${{#LINE}} % 2 )) -ne 0 ]; then
                  echo "error: odd-length hex on stdin" >&2; exit 5
                fi
                printf "%s" "$LINE" > "$STATE_FILE"
                exit 0
                ;;
              load)
                if [ ! -f "$STATE_FILE" ]; then
                  echo "error: key not stored" >&2; exit 2
                fi
                cat "$STATE_FILE"
                printf "\\n"
                exit 0
                ;;
              delete)
                rm -f "$STATE_FILE"; exit 0
                ;;
              *)
                echo "error: unknown command" >&2; exit 1
                ;;
            esac
        """,
        "missing_item": """
            if [ "$1" = "--account" ]; then shift 2; fi
            case "$1" in
              exists) exit 2;;
              load) echo "error: key not stored" >&2; exit 2;;
              store) exit 0;;
              delete) exit 0;;
            esac
        """,
        "cancelled": """
            if [ "$1" = "--account" ]; then shift 2; fi
            case "$1" in
              exists) exit 0;;
              load)   echo "error: Touch ID cancelled" >&2; exit 3;;
              store)  exit 0;;
              delete) exit 0;;
            esac
        """,
        "oserror": """
            echo "error: OSStatus=-26" >&2
            exit 4
        """,
        "broken_output": """
            if [ "$1" = "--account" ]; then shift 2; fi
            case "$1" in
              load) echo "NOT-HEX-DATA"; exit 0;;
              exists) exit 0;;
              *) exit 0;;
            esac
        """,
        "short_output": """
            if [ "$1" = "--account" ]; then shift 2; fi
            case "$1" in
              load) printf "deadbeef\\n"; exit 0;;
              exists) exit 0;;
              *) exit 0;;
            esac
        """,
        "timeout": """
            sleep 130   # longer than our 120s timeout
        """,
    }

    body = textwrap.dedent(bodies[behavior]).strip()
    # No `set -e`: see note above the `bodies` dict.
    script.write_text(f"#!/usr/bin/env bash\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------- fixtures ------------------------------------------------


class _HelperEnvMixin:
    """Each test gets a fresh tmpdir + fake helper + env override."""
    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        helper = _write_fake_helper(tmp, behavior=getattr(self, "BEHAVIOR", "happy"))
        self._old_env = os.environ.get(secret_store.HELPER_ENV)
        os.environ[secret_store.HELPER_ENV] = str(helper)

    def tearDown(self) -> None:  # type: ignore[override]
        if self._old_env is None:
            os.environ.pop(secret_store.HELPER_ENV, None)
        else:
            os.environ[secret_store.HELPER_ENV] = self._old_env
        self._tmp.cleanup()


# ---------- round-trip tests ----------------------------------------


class RoundTripTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "happy"

    def test_helper_path_honours_env_var(self):
        self.assertEqual(helper_path(),
                         Path(os.environ[secret_store.HELPER_ENV]))

    def test_has_master_key_false_when_no_state(self):
        self.assertFalse(has_master_key())

    def test_generate_returns_32_bytes(self):
        k = generate_master_key()
        self.assertIsInstance(k, bytes)
        self.assertEqual(len(k), KEY_BYTES)

    def test_store_then_load_round_trips(self):
        k = generate_master_key()
        self.assertFalse(has_master_key())
        store_master_key(k)
        self.assertTrue(has_master_key())
        loaded = load_master_key()
        self.assertEqual(loaded, k)

    def test_store_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            store_master_key(b"\x00" * 16)
        with self.assertRaises(ValueError):
            store_master_key(b"")
        with self.assertRaises(ValueError):
            store_master_key("not bytes")  # type: ignore[arg-type]

    def test_delete_removes_state(self):
        store_master_key(generate_master_key())
        self.assertTrue(has_master_key())
        delete_master_key()
        self.assertFalse(has_master_key())

    def test_masterkey_unlock_after_generate_and_store(self):
        k1 = MasterKey.generate_and_store()
        try:
            self.assertEqual(len(k1.as_bytes()), KEY_BYTES)
            k2 = MasterKey.unlock(prompt="Test unlock")
            self.assertEqual(k1.as_bytes(), k2.as_bytes())
        finally:
            k1.forget()
            delete_master_key()

    def test_masterkey_forget_zeros_buffer(self):
        k = MasterKey.from_bytes(b"\xab" * 32)
        self.assertNotEqual(k.as_bytes(), b"\x00" * 32)
        k.forget()
        self.assertEqual(k.as_bytes(), b"\x00" * 32)

    def test_masterkey_repr_is_safe(self):
        k = MasterKey.from_bytes(b"\xab" * 32)
        # repr() must not leak the bytes (we don't want them in logs).
        self.assertNotIn("ab", repr(k))
        self.assertIn("32 bytes", repr(k))

    def test_masterkey_as_bytes_returns_copy(self):
        k = MasterKey.from_bytes(b"\xab" * 32)
        b1 = k.as_bytes()
        b1_bytes = bytearray(b1)
        b1_bytes[0] = 0
        # The wrapped buffer should be untouched.
        self.assertEqual(k.as_bytes()[0], 0xab)


# ---------- exit-code branches --------------------------------------


class MissingItemTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "missing_item"

    def test_has_master_key_false(self):
        self.assertFalse(has_master_key())

    def test_load_raises_secretstoreerror_on_missing(self):
        with self.assertRaises(SecretStoreError) as cm:
            load_master_key()
        self.assertIn("not stored", str(cm.exception).lower())


class CancelledTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "cancelled"

    def test_load_raises_usercancelled_on_exit3(self):
        with self.assertRaises(UserCancelledError):
            load_master_key()


class OSStatusErrorTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "oserror"

    def test_load_surfaces_oserror_message(self):
        with self.assertRaises(SecretStoreError) as cm:
            load_master_key()
        # The error message should include the OSStatus number for
        # diagnosis.
        self.assertIn("OSStatus", str(cm.exception))


class BrokenOutputTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "broken_output"

    def test_load_raises_on_non_hex_stdout(self):
        with self.assertRaises(SecretStoreError) as cm:
            load_master_key()
        self.assertIn("hex", str(cm.exception).lower())


class ShortOutputTests(_HelperEnvMixin, unittest.TestCase):
    BEHAVIOR = "short_output"

    def test_load_raises_on_short_key(self):
        with self.assertRaises(SecretStoreError) as cm:
            load_master_key()
        # We require exactly 32 bytes.
        self.assertIn("32", str(cm.exception))


# ---------- helper-missing ------------------------------------------


class HelperMissingTests(unittest.TestCase):
    def test_missing_helper_raises_HelperMissingError(self):
        old = os.environ.get(secret_store.HELPER_ENV)
        os.environ[secret_store.HELPER_ENV] = "/this/does/not/exist"
        try:
            with self.assertRaises(HelperMissingError):
                has_master_key()
        finally:
            if old is None:
                os.environ.pop(secret_store.HELPER_ENV, None)
            else:
                os.environ[secret_store.HELPER_ENV] = old

    def test_non_executable_helper_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "touchid-keychain"
            path.write_text("#!/usr/bin/env bash\nexit 0\n")  # NOT chmod +x
            old = os.environ.get(secret_store.HELPER_ENV)
            os.environ[secret_store.HELPER_ENV] = str(path)
            try:
                with self.assertRaises(HelperMissingError):
                    has_master_key()
            finally:
                if old is None:
                    os.environ.pop(secret_store.HELPER_ENV, None)
                else:
                    os.environ[secret_store.HELPER_ENV] = old


# ---------- split-key (Option C) Keychain API ------------------------


class KcHalfRoundTripTests(_HelperEnvMixin, unittest.TestCase):
    """The Option C split-key API stores ``kc_half`` under a separate
    Keychain account (``master_key_kc_half_v2``). These tests exercise
    the same round-trip as the legacy API but on the new account, and
    additionally verify the two namespaces don't bleed into each other
    when both are populated at once.
    """
    BEHAVIOR = "happy"

    def test_account_constants_distinct(self):
        self.assertEqual(ACCOUNT_LEGACY_V1, "master_key")
        self.assertEqual(ACCOUNT_KC_HALF_V2, "master_key_kc_half_v2")
        self.assertNotEqual(ACCOUNT_LEGACY_V1, ACCOUNT_KC_HALF_V2)

    def test_has_kc_half_false_when_unset(self):
        self.assertFalse(has_kc_half())

    def test_kc_half_round_trip(self):
        half = generate_master_key()  # OS-CSPRNG 32 bytes — fine for kc_half
        store_kc_half(half)
        self.assertTrue(has_kc_half())
        self.assertEqual(load_kc_half(prompt="Test kc_half"), half)

    def test_kc_half_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            store_kc_half(b"\x00" * 16)
        with self.assertRaises(ValueError):
            store_kc_half(b"")

    def test_kc_half_delete_removes(self):
        store_kc_half(generate_master_key())
        self.assertTrue(has_kc_half())
        delete_kc_half()
        self.assertFalse(has_kc_half())

    def test_legacy_and_kc_half_are_isolated(self):
        legacy = b"\xaa" * KEY_BYTES
        kc = b"\xbb" * KEY_BYTES
        store_master_key(legacy)
        store_kc_half(kc)
        self.assertTrue(has_master_key())
        self.assertTrue(has_kc_half())
        self.assertEqual(load_master_key(prompt="leg"), legacy)
        self.assertEqual(load_kc_half(prompt="kch"), kc)
        delete_master_key()
        self.assertFalse(has_master_key())
        # Deleting one must leave the other intact — this is the
        # property the split-key flow depends on (migration deletes
        # the legacy item *after* writing kc_half).
        self.assertTrue(has_kc_half())
        delete_kc_half()
        self.assertFalse(has_kc_half())


if __name__ == "__main__":
    unittest.main()
