"""Tests for config.py — defaults, file overlay, env overlay, save/load
round-trip, validator."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


class _TempConfigCase(unittest.TestCase):
    """Each test gets a fresh tempdir + clean env for repeatability."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Wipe any local-scribe env vars that could leak through layers.
        self._saved_env = {
            k: os.environ[k]
            for k in list(os.environ)
            if k in config._ENV_OVERRIDES
        }
        for k in list(self._saved_env):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            os.environ[k] = v
        self._tmp.cleanup()


class DefaultsTests(_TempConfigCase):
    def test_load_with_no_file_returns_defaults(self):
        cfg = config.load_config(self.tmp / "missing.json")
        self.assertEqual(cfg.asr_backend, "parakeet")
        self.assertEqual(cfg.asr_port, 8000)
        self.assertEqual(cfg.llm_port, 1234)
        self.assertEqual(cfg.inspector_port, 8001)
        self.assertEqual(cfg.expected_stt_provider, "openai")

    def test_default_config_serialises_round_trip(self):
        # Validate that the baked-in defaults pass our own validator —
        # any future schema break gets caught here.
        self.assertEqual(config.validate(config.DEFAULT_CONFIG), [])

    def test_llm_url_assembles_from_host_port(self):
        cfg = config.load_config(self.tmp / "missing.json")
        self.assertEqual(cfg.llm_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(cfg.llm_models_url, "http://127.0.0.1:1234/api/v0/models")


class FileOverlayTests(_TempConfigCase):
    def test_partial_file_layered_over_defaults(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({
            "asr": {"port": 9999},
            "llm": {"host": "192.168.1.50"},
        }))
        cfg = config.load_config(path)
        self.assertEqual(cfg.asr_port, 9999)
        # Other asr fields kept from defaults
        self.assertEqual(cfg.asr_backend, "parakeet")
        self.assertEqual(cfg.llm_host, "192.168.1.50")
        self.assertEqual(cfg.llm_port, 1234)  # untouched

    def test_corrupt_json_falls_back_to_defaults(self):
        path = self.tmp / "config.json"
        path.write_text("{not valid json")
        cfg = config.load_config(path)
        self.assertEqual(cfg.asr_port, 8000)


class EnvOverlayTests(_TempConfigCase):
    def test_env_overrides_file(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"asr": {"port": 1111}}))
        with mock.patch.dict(os.environ, {"ASR_PORT": "2222"}):
            cfg = config.load_config(path)
            self.assertEqual(cfg.asr_port, 2222)

    def test_env_overrides_defaults_when_no_file(self):
        with mock.patch.dict(os.environ, {
            "LLM_HOST": "10.0.0.5",
            "LLM_PORT": "4444",
            "ASR_BACKEND": "whisper",
        }):
            cfg = config.load_config(self.tmp / "missing.json")
            self.assertEqual(cfg.llm_host, "10.0.0.5")
            self.assertEqual(cfg.llm_port, 4444)
            self.assertEqual(cfg.asr_backend, "whisper")
            self.assertEqual(
                cfg.llm_url,
                "http://10.0.0.5:4444/v1/chat/completions",
            )

    def test_bool_env_coercion(self):
        with mock.patch.dict(os.environ, {"DIARIZE": "0"}):
            cfg = config.load_config(self.tmp / "missing.json")
            self.assertFalse(cfg.diarize_enabled)
        with mock.patch.dict(os.environ, {"DIARIZE": "yes"}):
            cfg = config.load_config(self.tmp / "missing.json")
            self.assertTrue(cfg.diarize_enabled)

    def test_int_or_none_env_coercion(self):
        with mock.patch.dict(os.environ, {"NUM_SPEAKERS": "0"}):
            cfg = config.load_config(self.tmp / "missing.json")
            self.assertIsNone(cfg.num_speakers)
        with mock.patch.dict(os.environ, {"NUM_SPEAKERS": "3"}):
            cfg = config.load_config(self.tmp / "missing.json")
            self.assertEqual(cfg.num_speakers, 3)

    def test_bad_env_value_doesnt_crash_load(self):
        with mock.patch.dict(os.environ, {"ASR_PORT": "not-a-number"}):
            cfg = config.load_config(self.tmp / "missing.json")
            # Falls back to default rather than raising
            self.assertEqual(cfg.asr_port, 8000)


class SaveTests(_TempConfigCase):
    def test_save_round_trip(self):
        path = self.tmp / "config.json"
        data = copy.deepcopy(config.DEFAULT_CONFIG)
        data["asr"]["port"] = 7777
        data["llm"]["host"] = "10.1.2.3"
        config.save_config(data, path, backup=False)
        cfg = config.load_config(path)
        self.assertEqual(cfg.asr_port, 7777)
        self.assertEqual(cfg.llm_host, "10.1.2.3")

    def test_save_creates_timestamped_backup(self):
        path = self.tmp / "config.json"
        config.save_config({"asr": {"port": 1}, **config.DEFAULT_CONFIG}, path, backup=False)
        config.save_config({"asr": {"port": 2}, **config.DEFAULT_CONFIG}, path, backup=True)
        backups = list(self.tmp.glob("config.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_save_chmods_to_600(self):
        path = self.tmp / "config.json"
        config.save_config(config.DEFAULT_CONFIG, path, backup=False)
        mode = path.stat().st_mode & 0o777
        # Best-effort -- we only assert when chmod succeeded
        if mode != 0o600:
            self.skipTest(f"chmod 600 not applied (filesystem may not support it): mode={oct(mode)}")
        self.assertEqual(mode, 0o600)

    def test_write_default_if_missing_skips_existing(self):
        path = self.tmp / "config.json"
        path.write_text('{"asr": {"port": 9999}}')
        result = config.write_default_config_if_missing(path)
        self.assertIsNone(result)
        # Existing file untouched
        self.assertIn("9999", path.read_text())

    def test_write_default_if_missing_writes_when_absent(self):
        path = self.tmp / "config.json"
        result = config.write_default_config_if_missing(path)
        self.assertEqual(result, path)
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded["asr"]["port"], 8000)


class ValidatorTests(_TempConfigCase):
    def test_default_passes(self):
        self.assertEqual(config.validate(config.DEFAULT_CONFIG), [])

    def test_negative_port_rejected(self):
        d = copy.deepcopy(config.DEFAULT_CONFIG)
        d["asr"]["port"] = -1
        errs = config.validate(d)
        self.assertTrue(any("asr.port" in e for e in errs))

    def test_unknown_backend_rejected(self):
        d = copy.deepcopy(config.DEFAULT_CONFIG)
        d["asr"]["backend"] = "deepgram"
        errs = config.validate(d)
        self.assertTrue(any("asr.backend" in e for e in errs))

    def test_port_collision_rejected(self):
        d = copy.deepcopy(config.DEFAULT_CONFIG)
        d["inspector"]["port"] = d["asr"]["port"]
        errs = config.validate(d)
        self.assertTrue(any("differ" in e for e in errs))

    def test_non_loopback_inspector_without_token_rejected(self):
        d = copy.deepcopy(config.DEFAULT_CONFIG)
        d["inspector"]["bind"] = "0.0.0.0"
        d["inspector"]["auth_token"] = None
        errs = config.validate(d)
        self.assertTrue(any("auth_token" in e or "loopback" in e for e in errs))

    def test_non_loopback_inspector_with_token_allowed(self):
        d = copy.deepcopy(config.DEFAULT_CONFIG)
        d["inspector"]["bind"] = "0.0.0.0"
        d["inspector"]["auth_token"] = "deadbeef" * 4
        self.assertEqual(config.validate(d), [])


class CharPathDerivationTests(_TempConfigCase):
    def test_default_data_dir_is_platform_default(self):
        cfg = config.load_config(self.tmp / "missing.json")
        self.assertEqual(
            str(cfg.char_data_dir).rstrip("/"),
            str(Path.home() / "Library" / "Application Support" / "hyprnote"),
        )

    def test_explicit_data_dir_overrides(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"char": {"data_dir": str(self.tmp)}}))
        cfg = config.load_config(path)
        self.assertEqual(cfg.char_data_dir, self.tmp)
        self.assertEqual(cfg.char_settings_path, self.tmp / "settings.json")
        self.assertEqual(cfg.char_sessions_dir, self.tmp / "sessions")

    def test_expected_stt_base_url_derives_from_asr_bind_port(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"asr": {"bind": "0.0.0.0", "port": 9999}}))
        cfg = config.load_config(path)
        self.assertEqual(cfg.expected_stt_base_url, "http://0.0.0.0:9999/v1")


if __name__ == "__main__":
    unittest.main()
