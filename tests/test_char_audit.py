"""Tests for char_audit.py — audit() recognises clean/dirty Char state,
configure_char rewrites settings + store, secrets are masked."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

import char_audit
import config
import firewall


def _firewall_all_blocked_status() -> firewall.Status:
    """Synthetic ``firewall.Status`` representing "block list installed,
    full coverage". Used by the clean-state tests so they don't depend on
    the host machine's /etc/hosts."""
    expected_total = sum(
        1 for e in firewall.BLOCK_CATALOG
        if e.category in firewall.DEFAULT_ENABLED_CATEGORIES
    )
    return firewall.Status(
        installed=True,
        blocked_hostnames=[e.hostname for e in firewall.BLOCK_CATALOG
                           if e.category in firewall.DEFAULT_ENABLED_CATEGORIES],
        coverage_by_category={
            cat: {"blocked": expected_total, "expected": expected_total}
            for cat in sorted(firewall.DEFAULT_ENABLED_CATEGORIES)
        },
        missing_by_category={},
    )


def _make_cfg(data_dir: Path) -> config.Config:
    raw = copy.deepcopy(config.DEFAULT_CONFIG)
    raw["char"]["data_dir"] = str(data_dir)
    return config.Config(raw=raw)


# A well-formed ASR token (ls_asr_<32hex>). The new audit code accepts
# any value with this shape as OK when we don't have a master key to
# compare against (the strong-match case is exercised by
# ``CharAuditTokenDriftTests`` below).
_FAKE_ASR_TOKEN = "ls_asr_" + "a" * 32


def _write_settings(path: Path, **overrides) -> None:
    """Helper: build a minimal settings.json that looks like Char's
    real one, with the supplied overrides patched in. The default
    api_key is a token-shaped string so the audit's default state is
    OK (used to be the legacy "local" placeholder, now a warn-level
    finding)."""
    base = {
        "ai": {
            "current_stt_provider": "openai",
            "current_stt_model": "gpt-4o-transcribe",
            "stt": {
                "openai": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": _FAKE_ASR_TOKEN,
                },
            },
        },
    }
    for dotted, value in overrides.items():
        cur = base
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base, indent=2))


def _write_store(path: Path, analytics_disabled: bool, last_seen: str | None = None) -> None:
    payload: dict = {
        "analytics": json.dumps({"Disabled": analytics_disabled}),
    }
    if last_seen:
        payload["updater2"] = json.dumps({"LastSeenVersion": last_seen})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4))


class CleanStateTests(unittest.TestCase):
    def test_audit_all_ok_when_settings_match_expected(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(data_dir / "settings.json")
            _write_store(data_dir / "store.json", analytics_disabled=True,
                         last_seen="1.0.24")
            # Pretend the firewall block list is fully installed -- otherwise
            # the test's idea of "clean" depends on the host machine's
            # /etc/hosts. The firewall-specific drift assertions live in
            # ``FirewallIntegrationTests`` below.
            with mock.patch.object(firewall, "status",
                                   return_value=_firewall_all_blocked_status()):
                report = char_audit.audit(cfg)
            statuses = {c.key: c.status for c in report.checks}
            self.assertEqual(statuses["ai.current_stt_provider"], char_audit.OK)
            self.assertEqual(statuses["ai.current_stt_model"], char_audit.OK)
            self.assertEqual(statuses["ai.stt.openai.base_url"], char_audit.OK)
            self.assertEqual(statuses["ai.stt.openai.api_key"], char_audit.OK)
            self.assertEqual(statuses["store.analytics.Disabled"], char_audit.OK)
            # The updater2 entry is informational (no toggle exists)
            self.assertEqual(statuses["store.updater2.LastSeenVersion"], char_audit.INFO)
            self.assertEqual(statuses["firewall.block_list"], char_audit.OK)
            self.assertEqual(report.summary[char_audit.WARN], 0)


class FirewallIntegrationTests(unittest.TestCase):
    """``audit()`` should surface firewall coverage as its own check so
    the inspector's Char Audit tab and ``./run.sh doctor`` both report
    drift in one place."""

    def _audit_with_firewall(self, fw_status):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(data_dir / "settings.json")
            _write_store(data_dir / "store.json", analytics_disabled=True)
            with mock.patch.object(firewall, "status", return_value=fw_status):
                return char_audit.audit(cfg)

    def test_warns_when_block_list_not_installed(self):
        report = self._audit_with_firewall(firewall.Status(
            installed=False, blocked_hostnames=[],
            coverage_by_category={}, missing_by_category={},
        ))
        fw_check = next(c for c in report.checks if c.key == "firewall.block_list")
        self.assertEqual(fw_check.status, char_audit.WARN)
        self.assertIn("./run.sh firewall enable", fw_check.note)

    def test_ok_when_block_list_fully_installed(self):
        report = self._audit_with_firewall(_firewall_all_blocked_status())
        fw_check = next(c for c in report.checks if c.key == "firewall.block_list")
        self.assertEqual(fw_check.status, char_audit.OK)

    def test_warns_on_drift(self):
        """Installed but the catalog has grown since enable."""
        # Pretend the user enabled when the catalog had 18 hosts; now
        # there's 19, so one is missing.
        report = self._audit_with_firewall(firewall.Status(
            installed=True,
            blocked_hostnames=["a.example", "b.example"],
            coverage_by_category={
                "telemetry": {"blocked": 2, "expected": 3},
            },
            missing_by_category={"telemetry": ["c.example"]},
        ))
        fw_check = next(c for c in report.checks if c.key == "firewall.block_list")
        self.assertEqual(fw_check.status, char_audit.WARN)
        self.assertIn("missing", fw_check.note.lower())


class DirtyStateTests(unittest.TestCase):
    def test_warns_when_base_url_points_at_real_openai(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(
                data_dir / "settings.json",
                **{"ai.stt.openai.base_url": "https://api.openai.com/v1"},
            )
            _write_store(data_dir / "store.json", analytics_disabled=True)
            report = char_audit.audit(cfg)
            base_url = next(c for c in report.checks if c.key == "ai.stt.openai.base_url")
            self.assertEqual(base_url.status, char_audit.WARN)
            self.assertIn("api.openai.com", base_url.current)
            self.assertIn("not the local asr server", base_url.note.lower())

    def test_warns_when_analytics_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(data_dir / "settings.json")
            _write_store(data_dir / "store.json", analytics_disabled=False)
            report = char_audit.audit(cfg)
            ana = next(c for c in report.checks if c.key == "store.analytics.Disabled")
            self.assertEqual(ana.status, char_audit.WARN)
            self.assertFalse(ana.current)

    def test_warns_when_wrong_model_breaks_streaming_path(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(
                data_dir / "settings.json",
                **{"ai.current_stt_model": "gpt-4o-transcribe-diarize"},
            )
            _write_store(data_dir / "store.json", analytics_disabled=True)
            report = char_audit.audit(cfg)
            m = next(c for c in report.checks if c.key == "ai.current_stt_model")
            self.assertEqual(m.status, char_audit.WARN)
            self.assertIn("60s", m.note.lower().replace(" ", ""))

    def test_real_openai_key_is_masked_and_flagged_warn(self):
        # The audit now warns on real-looking OpenAI keys (used to be
        # INFO) because the new ASR auth model requires our derived
        # token, not a real OpenAI secret. A leftover sk-... is both
        # a wrong-token problem AND a privacy red flag.
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(
                data_dir / "settings.json",
                **{"ai.stt.openai.api_key": "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA"},
            )
            _write_store(data_dir / "store.json", analytics_disabled=True)
            report = char_audit.audit(cfg)
            k = next(c for c in report.checks if c.key == "ai.stt.openai.api_key")
            self.assertEqual(k.status, char_audit.WARN)
            # Full key must NOT appear in the rendered current value
            self.assertNotIn("AAAAAAAAAAAAAAAAAAAAAAAA", k.current)
            self.assertIn("...", k.current)


class CharAuditTokenDriftTests(unittest.TestCase):
    """When the master key is reachable (env var), audit() upgrades
    its api_key check from "looks-shaped-right" to a strong match
    against the HKDF-derived ASR token."""

    MK_HEX = "ab" * 32

    def setUp(self):
        # Inject the master key via env var so audit() can derive the
        # expected ASR token without prompting Touch ID.
        self._old = os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
        os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = self.MK_HEX
        # Bypass must be off so the drift check actually runs.
        import service_auth
        self._old_bypass = os.environ.pop(service_auth.BYPASS_ENV, None)

    def tearDown(self):
        import service_auth
        if self._old is None:
            os.environ.pop("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX", None)
        else:
            os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = self._old
        if self._old_bypass is not None:
            os.environ[service_auth.BYPASS_ENV] = self._old_bypass

    def _expected(self):
        import service_auth
        return service_auth.derive_service_token(bytes.fromhex(self.MK_HEX), "asr")

    def test_matching_token_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(
                data_dir / "settings.json",
                **{"ai.stt.openai.api_key": self._expected()},
            )
            _write_store(data_dir / "store.json", analytics_disabled=True)
            report = char_audit.audit(cfg)
            k = next(c for c in report.checks if c.key == "ai.stt.openai.api_key")
            self.assertEqual(k.status, char_audit.OK)
            self.assertIn("matches", k.note)

    def test_drifted_token_is_warn(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            # Right shape but wrong value.
            _write_settings(
                data_dir / "settings.json",
                **{"ai.stt.openai.api_key": "ls_asr_" + "b" * 32},
            )
            _write_store(data_dir / "store.json", analytics_disabled=True)
            report = char_audit.audit(cfg)
            k = next(c for c in report.checks if c.key == "ai.stt.openai.api_key")
            self.assertEqual(k.status, char_audit.WARN)
            self.assertIn("DRIFT", k.note)


class MissingFilesTests(unittest.TestCase):
    def test_settings_missing_yields_miss(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            report = char_audit.audit(cfg)
            self.assertFalse(report.settings_present)
            self.assertFalse(report.store_present)
            self.assertTrue(any(
                c.status == char_audit.MISS for c in report.checks
            ))


class ConfigureCharTests(unittest.TestCase):
    MK_HEX = "ab" * 32

    def setUp(self):
        # Inject a known master key so configure_char can derive the
        # ASR token without prompting Touch ID. Without this, the
        # function would either prompt or fall through to bypass mode.
        self._old = os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
        os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = self.MK_HEX
        import service_auth
        self._old_bypass = os.environ.pop(service_auth.BYPASS_ENV, None)

    def tearDown(self):
        import service_auth
        if self._old is None:
            os.environ.pop("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX", None)
        else:
            os.environ["LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"] = self._old
        if self._old_bypass is not None:
            os.environ[service_auth.BYPASS_ENV] = self._old_bypass

    def test_rewrites_four_keys_and_sets_analytics_disabled(self):
        import service_auth
        expected_token = service_auth.derive_service_token(
            bytes.fromhex(self.MK_HEX), "asr",
        )
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(
                data_dir / "settings.json",
                **{
                    "ai.current_stt_provider": "deepgram",
                    "ai.current_stt_model": "nova-2",
                    "ai.stt.openai.base_url": "https://api.openai.com/v1",
                    "ai.stt.openai.api_key": "sk-test-value",
                },
            )
            result = char_audit.configure_char(cfg, backup_existing_key=False)
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["settings_backup"]).is_file())
            patched = json.loads((data_dir / "settings.json").read_text())
            self.assertEqual(patched["ai"]["current_stt_provider"], "openai")
            self.assertEqual(patched["ai"]["current_stt_model"], "gpt-4o-transcribe")
            self.assertEqual(
                patched["ai"]["stt"]["openai"]["base_url"],
                "http://127.0.0.1:8000/v1",
            )
            # The api_key field is now the HKDF-derived ASR token.
            self.assertEqual(
                patched["ai"]["stt"]["openai"]["api_key"], expected_token,
            )
            # The response masks the token so the inspector UI doesn't
            # flash it.
            self.assertIn("...", result["after"]["openai_api_key"])
            self.assertNotIn(expected_token, result["after"]["openai_api_key"])
            store = json.loads((data_dir / "store.json").read_text())
            inner = json.loads(store["analytics"])
            self.assertTrue(inner["Disabled"])

    def test_refuses_when_settings_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(Path(td))
            result = char_audit.configure_char(cfg)
            self.assertFalse(result["ok"])
            self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
