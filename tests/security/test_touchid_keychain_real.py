"""Real-Keychain integration test for ``bin/touchid-keychain``.

The unit tests in ``test_secret_store.py`` use a bash fake of the Swift
helper and therefore cannot catch macOS-API-level breakage like:

  * 2026-05-11 bug A: ``SecItemAdd failed: OSStatus=-50`` (errSecParam).
    The Swift helper passed ``kSecUseAuthenticationUI:UISkip`` alongside
    a ``kSecAttrAccessControl(.userPresence)``. macOS 15 (Sequoia)
    rejects that combination at parameter validation.

  * 2026-05-11 bug B: ``SecItemAdd failed: OSStatus=-34018``
    (errSecMissingEntitlement). Once the flag conflict was fixed, the
    same call hit Apple's modern requirement that biometric-ACL items
    be added by binaries holding a ``keychain-access-groups``
    entitlement bound to a Developer Team ID. An ad-hoc-codesigned
    swiftc binary cannot obtain that.

Both bugs would have been caught by ANY real-Keychain smoke test on a
post-Sequoia laptop. This file is that test.

How it works
------------

We invoke the actual ``bin/touchid-keychain`` binary (compiled by
``ensure_touchid_helper`` during bootstrap) against the operator's real
login Keychain, using a throwaway ``--account ls_swift_smoketest_safe_to_delete``
so the test never touches the production ``master_key`` /
``master_key_kc_half_v2`` items. Each test stores 32 random bytes,
checks existence, then deletes — at no point do we call ``load``,
because that would pop a Touch ID sheet and require human interaction
(which is fine for the README live test, but unacceptable in CI / pre-
commit hooks).

What's NOT covered here
-----------------------

  * The biometric path (``load`` → LAContext.evaluatePolicy). That's
    intentional — see above. It's exercised by the README live test
    and by ``./run.sh start`` itself on first launch.

  * Behaviour when the helper binary is missing or unsigned. Covered
    by unit tests in ``test_secret_store.py`` via the fake.

  * Cross-account isolation. The fake covers this in unit tests.

Skip conditions
---------------

  * Non-macOS platforms: ``unittest.SkipTest`` early.
  * Missing helper binary (no bootstrap has run): skip with a clear
    message pointing at ``./run.sh bootstrap``.
  * ``LOCAL_SCRIBE_SKIP_KEYCHAIN_TESTS=1``: belt-and-braces for any
    environment where touching the real Keychain is undesirable.
"""

from __future__ import annotations

import os
import platform
import secrets
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "bin" / "touchid-keychain"

# Throwaway account name. Must pass the helper's identifier regex
# (alnum + ``_-`` only) and clearly indicate it's safe to delete if
# left over from a crashed test run.
TEST_ACCOUNT = "ls_swift_smoketest_safe_to_delete"


def _should_skip() -> str | None:
    if platform.system() != "Darwin":
        return f"real-Keychain test only runs on macOS (got {platform.system()})"
    if os.environ.get("LOCAL_SCRIBE_SKIP_KEYCHAIN_TESTS") == "1":
        return "LOCAL_SCRIBE_SKIP_KEYCHAIN_TESTS=1 set"
    if not HELPER.exists():
        return (
            f"helper binary missing at {HELPER} — "
            "run `./run.sh bootstrap` first"
        )
    if not os.access(HELPER, os.X_OK):
        return f"helper binary not executable at {HELPER}"
    return None


@unittest.skipIf(_should_skip() is not None, _should_skip() or "")
class RealKeychainIntegrationTests(unittest.TestCase):
    """End-to-end against the real macOS Keychain.

    Exercises ``store`` (the SecItemAdd path that broke twice in a row
    on 2026-05-11), ``exists`` (SecItemCopyMatching with UI skip), and
    ``delete`` (SecItemDelete). The biometric ``load`` path is
    deliberately NOT exercised here — see file docstring.
    """

    def setUp(self) -> None:
        # Defensive: remove any leftover test item from a previous
        # interrupted run so we always start clean.
        self._run("delete", check=False)

    def tearDown(self) -> None:
        self._run("delete", check=False)

    def _run(self, *subcmd: str, stdin: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
        cmd = [str(HELPER), "--account", TEST_ACCOUNT, *subcmd]
        proc = subprocess.run(
            cmd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if check and proc.returncode not in (0, 2):
            self.fail(
                f"helper {' '.join(subcmd)} failed: rc={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        return proc

    def test_store_exists_delete_round_trip(self) -> None:
        """Smoke test: store 32 random bytes, confirm exists=True,
        delete, confirm exists=False. This is the simplest possible
        end-to-end check that ``SecItemAdd`` is not rejecting our
        attribute dict — both the -50 and -34018 regressions would
        surface here on store.
        """
        data = secrets.token_bytes(32)
        store = self._run("store", stdin=data.hex())
        self.assertEqual(
            store.returncode, 0,
            f"store failed: stderr={store.stderr!r} "
            "(if you see OSStatus=-50: the SecItemAdd attribute dict "
            "regressed to the conflicting access-control/UI-flag "
            "combination — see bin/touchid_keychain.swift; if -34018: "
            "the keychain ACL regressed to one of .userPresence / "
            ".biometryCurrentSet which now require a developer-team "
            "entitlement on macOS 15+)",
        )

        exists = self._run("exists", check=False)
        self.assertEqual(
            exists.returncode, 0,
            f"exists should be 0 after store: stderr={exists.stderr!r}",
        )

        delete = self._run("delete")
        self.assertEqual(delete.returncode, 0, delete.stderr)

        exists_after = self._run("exists", check=False)
        self.assertEqual(
            exists_after.returncode, 2,
            "exists should be 2 (not found) after delete",
        )

    def test_store_then_overwrite(self) -> None:
        """The helper documents replace-on-add semantics. A second
        store must succeed (no errSecDuplicateItem)."""
        first = secrets.token_bytes(32)
        second = secrets.token_bytes(32)
        self._run("store", stdin=first.hex())
        proc = self._run("store", stdin=second.hex())
        self.assertEqual(
            proc.returncode, 0,
            f"replace-on-add failed: {proc.stderr!r}",
        )

    def test_invalid_hex_returns_code_5(self) -> None:
        """Pre-SecItemAdd validation: bogus hex should exit 5, NOT
        leak through to a confusing OSStatus failure."""
        proc = self._run("store", stdin="not-hex", check=False)
        self.assertEqual(proc.returncode, 5)
        self.assertIn("invalid hex", proc.stderr)

    def test_account_flag_isolation(self) -> None:
        """Storing under our test account must not affect the
        production accounts. We probe exists for the real account
        names and assert their state is whatever it was before the
        test ran. This is a paranoia check that --account isolation
        actually works in the real Keychain layer.
        """
        from local_scribe.security.secret_store import (
            ACCOUNT_KC_HALF_V2,
            ACCOUNT_LEGACY_V1,
        )

        def _probe(account: str) -> int:
            proc = subprocess.run(
                [str(HELPER), "--account", account, "exists"],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode

        before_legacy = _probe(ACCOUNT_LEGACY_V1)
        before_v2 = _probe(ACCOUNT_KC_HALF_V2)
        data = secrets.token_bytes(32)
        self._run("store", stdin=data.hex())
        after_legacy = _probe(ACCOUNT_LEGACY_V1)
        after_v2 = _probe(ACCOUNT_KC_HALF_V2)
        self.assertEqual(
            before_legacy, after_legacy,
            f"legacy v1 account state changed: {before_legacy}→{after_legacy}",
        )
        self.assertEqual(
            before_v2, after_v2,
            f"v2 account state changed: {before_v2}→{after_v2}",
        )


# ---------------------------------------------------------------------------
# Source-level regression catchers. These run on every platform — they
# do not invoke the binary; they grep the .swift source for known-bad
# patterns. They catch regressions even on Linux CI where the real-
# Keychain integration tests skip.

class SwiftSourceRegressionTests(unittest.TestCase):
    """The Swift source must not contain the patterns that broke
    bootstrap on 2026-05-11. These guards are cheap and platform-
    independent — they run even when the real-Keychain integration
    suite is skipped.
    """

    def _source(self) -> str:
        path = REPO / "bin" / "touchid_keychain.swift"
        if not path.exists():
            self.skipTest(f"Swift source missing at {path}")
        return path.read_text()

    def test_no_use_authentication_ui_skip_on_storekey(self) -> None:
        """Bug A: combining ``kSecUseAuthenticationUI`` (any value) with
        ``kSecAttrAccessControl`` is rejected by SecItemAdd on
        macOS 15+ with errSecParam. The fix is to omit the flag from
        SecItemAdd's attribute dict.
        """
        src = self._source()
        # The helper has both storeKey and loadKey; we only care about
        # storeKey. Pull the function body and check.
        before_func = src.split("func storeKey(", 1)
        self.assertEqual(
            len(before_func), 2,
            "couldn't find storeKey() in Swift source — refactor?",
        )
        # Take the body up to the next func declaration so we don't
        # falsely match references in unrelated funcs.
        body = before_func[1].split("\nfunc ", 1)[0]
        self.assertNotIn(
            "kSecUseAuthenticationUI",
            body,
            "storeKey() must NOT pass kSecUseAuthenticationUI — "
            "combining it with kSecAttrAccessControl fails with "
            "errSecParam (-50) on macOS 15+ (2026-05-11 regression)",
        )

    def test_no_biometric_acl_on_storekey(self) -> None:
        """Bug B: ``.userPresence`` / ``.biometryCurrentSet`` /
        ``.biometryAny`` flags on ``SecAccessControlCreateWithFlags``
        require a developer-team entitlement on macOS 15+ that an
        ad-hoc-signed swiftc binary cannot get; SecItemAdd fails with
        errSecMissingEntitlement (-34018). The fix is to gate
        biometric in loadKey() via LAContext and add the keychain
        item with only an accessibility class.
        """
        src = self._source()
        before_func = src.split("func storeKey(", 1)
        if len(before_func) != 2:
            self.skipTest("storeKey not in expected form")
        body = before_func[1].split("\nfunc ", 1)[0]
        for forbidden in [
            ".userPresence",
            ".biometryCurrentSet",
            ".biometryAny",
            "SecAccessControlCreateWithFlags",
        ]:
            self.assertNotIn(
                forbidden,
                body,
                f"storeKey() must NOT use {forbidden!r} — biometric "
                "ACLs require a developer-team entitlement on "
                "macOS 15+. Move the biometric check into loadKey() "
                "via LAContext.evaluatePolicy instead.",
            )

    def test_loadkey_uses_lacontext_evaluatepolicy(self) -> None:
        """Positive guard: loadKey must actually perform a biometric
        check via LAContext. If we ever silently drop it, this
        catches the regression — the keychain item has no ACL so the
        check is now load-bearing on this code path.
        """
        src = self._source()
        before_func = src.split("func loadKey(", 1)
        self.assertEqual(
            len(before_func), 2,
            "couldn't find loadKey() in Swift source — refactor?",
        )
        body = before_func[1].split("\nfunc ", 1)[0]
        self.assertIn(
            "evaluatePolicy",
            body,
            "loadKey() must call LAContext.evaluatePolicy — the "
            "biometric check moved here when we dropped the keychain "
            "ACL; dropping it would silently disable Touch ID gating",
        )
        self.assertIn(
            ".deviceOwnerAuthentication",
            body,
            "loadKey() should evaluate .deviceOwnerAuthentication "
            "(Touch ID with passcode fallback), not just biometry",
        )


if __name__ == "__main__":
    unittest.main()
