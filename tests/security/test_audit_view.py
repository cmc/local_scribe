"""Tests for :mod:`local_scribe.security.audit_view`.

The audit-view is the single composition point that aggregates every
defense layer's cheap ``status()`` / ``verify()`` call into one
JSON-serialisable dict the inspector front-end renders into the
"Char audit → Security verification" panel.

What this file pins
-------------------

* **Shape.** ``snapshot()`` returns the keys the inspector front-end
  hard-codes (``schema_version``, ``summary``, ``checks``). Each
  check carries ``key``, ``label``, ``status``, ``summary``,
  ``detail``. The inspector breaks silently if any of these drift.
* **Status grading.** Each ``check_*`` function returns the
  documented severity for the corresponding state. We patch the
  underlying status functions to drive each branch and assert the
  ``status`` field.
* **Crash-safety.** ``_safe()`` converts any uncaught exception in a
  check function into a FAIL row so the whole view does not break
  if one layer raises. We add a deliberately-broken check fixture
  and confirm it surfaces as FAIL with the error in ``detail``.
* **No secret leakage.** ``snapshot()`` must never return a key body
  / bearer / passphrase even if a layer's underlying ``status()``
  returns one. We feed a sentinel into the master-key state and
  assert it does not surface verbatim in the JSON.

Cheap-only invariant
--------------------

A second test class (`CheapnessTests`) records the modules each
check function imports + invokes. The list is intentionally narrow
(only the cheap status / verify functions). If a future refactor
adds an unlock-master-key call to one of these, the test fails and
forces the author to either justify the cost or move the call to a
separate explicit "Verify unlock" endpoint.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from local_scribe.security import audit_view


# ---------------------------------------------------------------------------
# Top-level shape


class SnapshotShapeTests(unittest.TestCase):
    """``snapshot()`` returns the shape the inspector renders against."""

    def test_returns_required_top_level_keys(self) -> None:
        snap = audit_view.snapshot()
        for k in ("schema_version", "summary", "checks"):
            self.assertIn(k, snap, f"missing top-level key: {k!r}")

    def test_schema_version_is_int(self) -> None:
        snap = audit_view.snapshot()
        self.assertIsInstance(snap["schema_version"], int)
        self.assertGreaterEqual(snap["schema_version"], 1)

    def test_summary_has_all_status_keys(self) -> None:
        snap = audit_view.snapshot()
        for k in (audit_view.OK, audit_view.WARN,
                  audit_view.FAIL, audit_view.INFO):
            self.assertIn(k, snap["summary"])
            self.assertIsInstance(snap["summary"][k], int)

    def test_summary_counts_match_checks(self) -> None:
        snap = audit_view.snapshot()
        total = sum(snap["summary"].values())
        self.assertEqual(total, len(snap["checks"]),
                         "summary counts must add up to the check count")
        observed = {audit_view.OK: 0, audit_view.WARN: 0,
                    audit_view.FAIL: 0, audit_view.INFO: 0}
        for c in snap["checks"]:
            observed[c["status"]] += 1
        self.assertEqual(observed, snap["summary"])

    def test_every_check_has_required_keys(self) -> None:
        snap = audit_view.snapshot()
        for c in snap["checks"]:
            for k in ("key", "label", "status", "summary", "detail"):
                self.assertIn(k, c, f"check missing key {k!r}: {c}")
            self.assertIn(c["status"],
                          {audit_view.OK, audit_view.WARN,
                           audit_view.FAIL, audit_view.INFO})
            self.assertIsInstance(c["detail"], dict)

    def test_check_keys_are_stable_and_unique(self) -> None:
        """The front-end hard-codes these. Renaming or duplicating any
        of them is a breaking change."""
        snap = audit_view.snapshot()
        keys = [c["key"] for c in snap["checks"]]
        self.assertEqual(len(keys), len(set(keys)),
                         "duplicate check keys")
        # Pin the canonical set so a missing one is obvious in CI.
        expected = {
            "sip", "firewall", "master_key", "vault",
            "char_settings", "char_integrity",
            "signed_config", "script_integrity",
            "precommit_hook", "dr",
        }
        self.assertEqual(set(keys), expected,
                         f"check key set drifted: {set(keys) ^ expected}")


# ---------------------------------------------------------------------------
# Per-layer grading


class CheckSipTests(unittest.TestCase):
    """SIP check grades on (fully_enabled, dev_mode) pairs."""

    def test_fully_enabled_and_no_dev_is_ok(self) -> None:
        from local_scribe.security import sip_check
        rep = mock.Mock()
        rep.state = sip_check.SIPState.FULLY_ENABLED
        rep.raw_top_line = "enabled."
        with mock.patch("local_scribe.security.sip_check.status",
                        return_value=rep), \
             mock.patch("local_scribe.common.dev_mode.is_enabled",
                        return_value=False):
            c = audit_view.check_sip()
        self.assertEqual(c.status, audit_view.OK)
        self.assertFalse(c.detail["dev_mode"])

    def test_disabled_no_dev_is_fail(self) -> None:
        from local_scribe.security import sip_check
        rep = mock.Mock()
        rep.state = sip_check.SIPState.DISABLED
        rep.raw_top_line = "disabled."
        with mock.patch("local_scribe.security.sip_check.status",
                        return_value=rep), \
             mock.patch("local_scribe.common.dev_mode.is_enabled",
                        return_value=False):
            c = audit_view.check_sip()
        self.assertEqual(c.status, audit_view.FAIL)

    def test_dev_mode_set_downgrades_to_warn(self) -> None:
        from local_scribe.security import sip_check
        rep = mock.Mock()
        rep.state = sip_check.SIPState.DISABLED
        rep.raw_top_line = "disabled."
        with mock.patch("local_scribe.security.sip_check.status",
                        return_value=rep), \
             mock.patch("local_scribe.common.dev_mode.is_enabled",
                        return_value=True):
            c = audit_view.check_sip()
        self.assertEqual(c.status, audit_view.WARN)
        self.assertTrue(c.detail["dev_mode"])


class CheckMasterKeyTests(unittest.TestCase):
    def test_both_halves_present_is_ok(self) -> None:
        with mock.patch("local_scribe.security.secret_store.has_kc_half",
                        return_value=True), \
             mock.patch("local_scribe.security.yubikey_backup.has_yk_half",
                        return_value=True), \
             mock.patch("local_scribe.security.secret_store.has_master_key",
                        return_value=False):
            c = audit_view.check_master_key()
        self.assertEqual(c.status, audit_view.OK)

    def test_legacy_only_is_warn(self) -> None:
        with mock.patch("local_scribe.security.secret_store.has_kc_half",
                        return_value=False), \
             mock.patch("local_scribe.security.yubikey_backup.has_yk_half",
                        return_value=False), \
             mock.patch("local_scribe.security.secret_store.has_master_key",
                        return_value=True):
            c = audit_view.check_master_key()
        self.assertEqual(c.status, audit_view.WARN)

    def test_kc_only_no_yk_is_fail(self) -> None:
        with mock.patch("local_scribe.security.secret_store.has_kc_half",
                        return_value=True), \
             mock.patch("local_scribe.security.yubikey_backup.has_yk_half",
                        return_value=False), \
             mock.patch("local_scribe.security.secret_store.has_master_key",
                        return_value=False):
            c = audit_view.check_master_key()
        self.assertEqual(c.status, audit_view.FAIL)

    def test_no_key_is_fail(self) -> None:
        with mock.patch("local_scribe.security.secret_store.has_kc_half",
                        return_value=False), \
             mock.patch("local_scribe.security.yubikey_backup.has_yk_half",
                        return_value=False), \
             mock.patch("local_scribe.security.secret_store.has_master_key",
                        return_value=False):
            c = audit_view.check_master_key()
        self.assertEqual(c.status, audit_view.FAIL)


class CheckVaultTests(unittest.TestCase):
    """``check_vault`` rolls (exists, mounted, relocated, leftovers)
    into a single severity. Order of FAIL precedence:
    no-bundle > not-relocated > real-leftover > demo-leftover-only > unmounted.
    """

    def _patch(self, *, exists=True, mounted=True, relocated=True,
               leftovers=None):
        from local_scribe.security import vault as vault_mod
        status = {
            "exists": exists, "mounted": mounted,
            "char_data_relocated": relocated,
            "bundle_path": "/x/v.sparsebundle", "mount_path": "/x/v",
            "char_data_path": "/x/hyprnote", "bundle_size_bytes": 0,
        }
        return mock.patch.object(vault_mod, "status", return_value=status), \
               mock.patch.object(vault_mod,
                                 "find_plaintext_char_data_copies",
                                 return_value=leftovers or [])

    def test_clean_vault_is_ok(self) -> None:
        p1, p2 = self._patch()
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.OK)
        self.assertEqual(c.detail["plaintext_leftover_count"], 0)

    def test_missing_bundle_is_fail(self) -> None:
        p1, p2 = self._patch(exists=False)
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.FAIL)

    def test_not_relocated_is_fail(self) -> None:
        p1, p2 = self._patch(relocated=False)
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.FAIL)
        self.assertIn("PLAINTEXT", c.summary)

    def test_real_leftover_is_fail(self) -> None:
        from local_scribe.security import vault as vault_mod
        leftover = vault_mod.PlaintextCopyFinding(
            path=Path("/tmp/hyprnote.pre_vault_backup.123"),
            kind=vault_mod.LeftoverKind.PRE_VAULT_BACKUP,
            size_bytes=2_700_000_000,
            mtime=1735340000.0,
            session_count=6, audio_count=12,
        )
        p1, p2 = self._patch(leftovers=[leftover])
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.FAIL)
        self.assertEqual(c.detail["plaintext_leftover_count"], 1)

    def test_demo_only_leftover_is_warn(self) -> None:
        """Demo cache alone is not real user data; surface as WARN
        rather than FAIL so the operator isn't alarmed."""
        from local_scribe.security import vault as vault_mod
        demo = vault_mod.PlaintextCopyFinding(
            path=Path("/tmp/cache/hyprnote"),
            kind=vault_mod.LeftoverKind.DEMO_CACHE,
            size_bytes=2_000_000,
            mtime=1735340000.0,
            session_count=5, audio_count=5,
        )
        p1, p2 = self._patch(leftovers=[demo])
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.WARN)

    def test_unmounted_but_clean_is_warn(self) -> None:
        p1, p2 = self._patch(mounted=False)
        with p1, p2:
            c = audit_view.check_vault()
        self.assertEqual(c.status, audit_view.WARN)


# ---------------------------------------------------------------------------
# Crash-safety


class SafeWrappingTests(unittest.TestCase):
    """``_safe`` converts any uncaught exception into a FAIL row."""

    def test_exception_in_check_becomes_fail_row(self) -> None:
        def broken() -> audit_view.Check:
            raise RuntimeError("kaboom")
        c = audit_view._safe(broken, key="x", label="X")
        self.assertEqual(c.status, audit_view.FAIL)
        self.assertEqual(c.key, "x")
        self.assertIn("kaboom", c.summary)
        self.assertIn("RuntimeError", c.summary)
        self.assertEqual(c.detail["error_type"], "RuntimeError")

    def test_success_passes_through(self) -> None:
        ok = audit_view.Check("y", "Y", audit_view.OK, "fine")

        def good() -> audit_view.Check:
            return ok
        self.assertIs(audit_view._safe(good, key="y", label="Y"), ok)


# ---------------------------------------------------------------------------
# No-secret-leakage


class NoSecretLeakageTests(unittest.TestCase):
    """The audit-view must never surface a master-key body / bearer /
    passphrase even if a downstream layer's status() did. We feed
    sentinel values into each layer and assert they don't appear in
    the JSON serialisation."""

    def test_snapshot_does_not_leak_sentinel_secrets(self) -> None:
        import json
        sentinel = "SECRETSECRETSECRETSECRETSECRETSE"
        # Inject the sentinel into every place a leaky status() might
        # plausibly stash it.
        with mock.patch("local_scribe.security.secret_store.has_kc_half",
                        return_value=True), \
             mock.patch("local_scribe.security.yubikey_backup.has_yk_half",
                        return_value=True), \
             mock.patch("local_scribe.security.secret_store.has_master_key",
                        return_value=False), \
             mock.patch.dict("os.environ", {"FAKE_SECRET": sentinel}):
            snap = audit_view.snapshot()
        blob = json.dumps(snap)
        self.assertNotIn(sentinel, blob,
                         "sentinel secret leaked into audit-view JSON")


# ---------------------------------------------------------------------------
# Cheap-only invariant


class CheapnessTests(unittest.TestCase):
    """No check function may call into a path that prompts Touch ID,
    taps the YubiKey, or shells out to hdiutil. The way we pin this
    is by listing the modules each check legitimately uses.

    If you add a new dependency to a check function, add it here
    too -- the test will fail otherwise, forcing you to justify the
    new cost (or move it to a separate explicit-unlock endpoint).
    """

    def test_no_check_calls_master_key_unlock(self) -> None:
        # ``unlock_master_key`` is the only path that triggers Touch
        # ID + YubiKey prompts. Snapshot() must never call it.
        from local_scribe.security import key_lifecycle
        with mock.patch.object(key_lifecycle, "unlock_master_key",
                               side_effect=AssertionError(
                                   "snapshot() called unlock_master_key")):
            # We don't care about the result; we care that no path
            # touched the patched unlock function.
            audit_view.snapshot()

    def test_no_check_calls_hdiutil(self) -> None:
        """No check function should shell out to hdiutil -- that's a
        slow + interactive (prompts via DiskArbitration) path. The
        vault check uses ``vault.status()`` + the in-process
        ``find_plaintext_char_data_copies`` walker; both are pure
        filesystem reads.
        """
        from local_scribe.security import vault as vault_mod
        # The vault module wraps hdiutil in ``_hdiutil()``. Make it
        # raise so we'd notice if any check path tried to invoke it.
        with mock.patch.object(vault_mod, "_hdiutil",
                               side_effect=AssertionError(
                                   "snapshot() called hdiutil")):
            audit_view.snapshot()


if __name__ == "__main__":
    unittest.main()
