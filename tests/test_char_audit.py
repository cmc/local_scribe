"""Tests for char_audit.py — audit() recognises clean/dirty Char state,
configure_char rewrites settings + store, secrets are masked."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import char_audit
import config


def _make_cfg(data_dir: Path) -> config.Config:
    raw = copy.deepcopy(config.DEFAULT_CONFIG)
    raw["char"]["data_dir"] = str(data_dir)
    return config.Config(raw=raw)


def _write_settings(path: Path, **overrides) -> None:
    """Helper: build a minimal settings.json that looks like Char's
    real one, with the supplied overrides patched in."""
    base = {
        "ai": {
            "current_stt_provider": "openai",
            "current_stt_model": "gpt-4o-transcribe",
            "stt": {
                "openai": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "local",
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
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            cfg = _make_cfg(data_dir)
            _write_settings(data_dir / "settings.json")
            _write_store(data_dir / "store.json", analytics_disabled=True,
                         last_seen="1.0.24")
            report = char_audit.audit(cfg)
            statuses = {c.key: c.status for c in report.checks}
            self.assertEqual(statuses["ai.current_stt_provider"], char_audit.OK)
            self.assertEqual(statuses["ai.current_stt_model"], char_audit.OK)
            self.assertEqual(statuses["ai.stt.openai.base_url"], char_audit.OK)
            self.assertEqual(statuses["ai.stt.openai.api_key"], char_audit.OK)
            self.assertEqual(statuses["store.analytics.Disabled"], char_audit.OK)
            # The updater2 entry is informational (no toggle exists)
            self.assertEqual(statuses["store.updater2.LastSeenVersion"], char_audit.INFO)
            self.assertEqual(report.summary[char_audit.WARN], 0)


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

    def test_real_openai_key_is_masked_and_flagged_info(self):
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
            self.assertEqual(k.status, char_audit.INFO)
            # Full key must NOT appear in the rendered current value
            self.assertNotIn("AAAAAAAAAAAAAAAAAAAAAAAA", k.current)
            self.assertIn("...", k.current)


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
    def test_rewrites_four_keys_and_sets_analytics_disabled(self):
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
            self.assertEqual(patched["ai"]["stt"]["openai"]["api_key"], "local")
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
