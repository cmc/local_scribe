"""ASR server: ``LOCAL_SCRIBE_ASR_TOKEN`` env override.

Background
----------

Until 2026-05-11, every spawn of the ASR server called
``service_auth.ServiceToken.unlock("asr")`` during its FastAPI
lifespan hook. That meant a fresh Touch ID modal + YubiKey tap
every time ``./run.sh start`` brought the server up — and because
the spawn redirects stdout/stderr to a log file, the explanatory
banners never made it to the operator's terminal. The 2026-05-11
operator-facing audit ("when the services are loading it needs to
print out the instruction to accept the touchid press") prompted
this fix.

Fix:

* ``run.sh cmd_start`` now runs ``service_auth warm asr inspector``
  ONCE in the foreground shell (where Touch ID + YubiKey banners
  are visible), captures the JSON output, and spawns each service
  with its token in its PER-SUBPROCESS environ.

* The ASR server's lifespan now reads ``LOCAL_SCRIBE_ASR_TOKEN``
  FIRST and short-circuits the unlock if it's set. Bit-identical
  to what ``service_auth.warm_tokens`` produces.

This file pins the contract from the *worker's* side.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from local_scribe.asr import asr_server
from local_scribe.security import service_auth


async def _fake_run_asr_async(audio_path, on_segment=None, on_start=None):
    """Stub ASR worker so the auth-gate tests don't need a real model.
    Same shape as ``tests/asr/test_asr_server.py``'s helper: returns
    the (text, words, lang, duration) 4-tuple the FastAPI handler
    unpacks."""
    return "stub transcript", [], "en", 0.0


class AsrServerEnvTokenOverrideTests(unittest.TestCase):
    """The pre-warmed env path is checked BEFORE the test-master-key
    path and BEFORE the unlock path."""

    TEST_TOKEN = "ls_asr_" + "a" * 32

    @classmethod
    def setUpClass(cls):
        # Clear every conflicting env var; otherwise the lifespan
        # might pick up a leftover ``LOCAL_SCRIBE_TEST_MASTER_KEY_HEX``
        # from a sibling test class and we'd never exercise our path.
        cls._saved = {
            k: os.environ.get(k) for k in (
                service_auth.BYPASS_ENV,
                "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
                "LOCAL_SCRIBE_ASR_TOKEN",
            )
        }
        for k in cls._saved:
            os.environ.pop(k, None)
        # Install our pre-warmed token before the lifespan fires.
        os.environ["LOCAL_SCRIBE_ASR_TOKEN"] = cls.TEST_TOKEN

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        # Patch out unlock_master_key so a regression that bypasses
        # our env check would FAIL LOUDLY (rather than silently
        # popping a Touch ID modal during pytest).
        self._unlock_patch = mock.patch(
            "local_scribe.security.key_lifecycle.unlock_master_key",
            side_effect=AssertionError(
                "asr_server lifespan must not call unlock_master_key "
                "when LOCAL_SCRIBE_ASR_TOKEN is in env"
            ),
        )
        self._unlock_patch.start()
        self.addCleanup(self._unlock_patch.stop)
        self._cm = TestClient(asr_server.app)
        self.client = self._cm.__enter__()
        self.addCleanup(lambda: self._cm.__exit__(None, None, None))
        self._asr_patcher = mock.patch.object(
            asr_server, "_run_asr_async", side_effect=_fake_run_asr_async,
        )
        self._asr_patcher.start()
        self.addCleanup(self._asr_patcher.stop)

    def test_lifespan_picks_up_env_token(self):
        """The smoking gun: ``_asr_token`` is the env-provided value,
        verbatim, not something derived inside the worker."""
        holder = asr_server._asr_token
        self.assertIsNotNone(holder)
        self.assertEqual(holder.service, "asr")
        self.assertEqual(holder.token, self.TEST_TOKEN)

    def test_endpoint_accepts_env_token_as_bearer(self):
        """End-to-end: a client presenting the pre-warmed token gets
        past the 401 gate."""
        r = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.m4a", io.BytesIO(b"FAKE" * 100), "audio/m4a")},
            data={"model": "gpt-4o-transcribe-diarize"},
            headers={"Authorization": f"Bearer {self.TEST_TOKEN}"},
        )
        # 200 (we stubbed the model) — definitely not 401.
        self.assertEqual(r.status_code, 200, msg=f"body={r.text[:300]}")

    def test_endpoint_rejects_wrong_token(self):
        r = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.m4a", io.BytesIO(b"FAKE" * 100), "audio/m4a")},
            data={"model": "gpt-4o-transcribe-diarize"},
            headers={"Authorization": "Bearer ls_asr_wrong"},
        )
        self.assertEqual(r.status_code, 401)


class AsrServerEnvTokenStrippingTests(unittest.TestCase):
    """``LOCAL_SCRIBE_ASR_TOKEN`` is consumed by ``.strip()`` so a
    trailing newline from ``$(python -m service_auth warm ...)`` (which
    ``print``s with a newline) doesn't poison the comparison.

    This is the single most likely shell footgun for this code path:
    bash command-substitution strips trailing newlines but a stray
    leading space wouldn't be caught. We pin both."""

    BASE_TOKEN = "ls_asr_" + "f" * 32

    def _run_with_token(self, raw: str) -> str:
        saved = {
            k: os.environ.get(k) for k in (
                service_auth.BYPASS_ENV,
                "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
                "LOCAL_SCRIBE_ASR_TOKEN",
            )
        }
        for k in saved:
            os.environ.pop(k, None)
        os.environ["LOCAL_SCRIBE_ASR_TOKEN"] = raw
        try:
            with mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                side_effect=AssertionError("must not unlock"),
            ):
                with TestClient(asr_server.app):
                    return asr_server._asr_token.token
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_trailing_newline_stripped(self):
        self.assertEqual(self._run_with_token(self.BASE_TOKEN + "\n"),
                         self.BASE_TOKEN)

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(self._run_with_token("  " + self.BASE_TOKEN + "  "),
                         self.BASE_TOKEN)


if __name__ == "__main__":
    unittest.main()
