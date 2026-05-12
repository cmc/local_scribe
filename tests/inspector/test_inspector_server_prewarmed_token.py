"""Inspector server: ``LOCAL_SCRIBE_INSPECTOR_TOKEN`` env override.

See ``tests/asr/test_asr_server_prewarmed_token.py`` for the full
context — this file is the same shape, for the inspector worker
side of the warm-token handoff.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from local_scribe.inspector import inspector_server
from local_scribe.security import service_auth


class InspectorEnvTokenOverrideTests(unittest.TestCase):
    TEST_TOKEN = "ls_inspector_" + "b" * 32

    @classmethod
    def setUpClass(cls):
        cls._saved = {
            k: os.environ.get(k) for k in (
                service_auth.BYPASS_ENV,
                "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
                "LOCAL_SCRIBE_INSPECTOR_TOKEN",
            )
        }
        for k in cls._saved:
            os.environ.pop(k, None)
        os.environ["LOCAL_SCRIBE_INSPECTOR_TOKEN"] = cls.TEST_TOKEN

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        # Hard regression guard: if a refactor accidentally drops the
        # env-var short-circuit, the unlock_master_key call would
        # blow up here rather than silently popping a Touch ID modal
        # during pytest.
        self._unlock_patch = mock.patch(
            "local_scribe.security.key_lifecycle.unlock_master_key",
            side_effect=AssertionError(
                "inspector_server lifespan must not call unlock_master_key "
                "when LOCAL_SCRIBE_INSPECTOR_TOKEN is in env"
            ),
        )
        self._unlock_patch.start()
        self.addCleanup(self._unlock_patch.stop)

    def test_resolve_token_holder_uses_env_token(self):
        holder = inspector_server._resolve_token_holder_at_startup()
        self.assertIsNotNone(holder)
        assert holder is not None
        self.assertEqual(holder.service, "inspector")
        self.assertEqual(holder.token, self.TEST_TOKEN)

    def test_endpoint_accepts_env_token(self):
        """End-to-end through FastAPI — startup lifespan picks up the
        env token, the auth middleware compares against it, request
        passes. We probe ``/api/security/audit`` because it's
        contract-guaranteed auth-gated (see
        ``test_inspector_server::AuthTests::test_security_audit_requires_auth``)."""
        with TestClient(inspector_server.app) as client:
            r = client.get(
                "/api/security/audit",
                headers={"Authorization": f"Bearer {self.TEST_TOKEN}"},
            )
            self.assertNotEqual(r.status_code, 401, msg=f"body={r.text[:300]}")

    def test_endpoint_rejects_wrong_token(self):
        with TestClient(inspector_server.app) as client:
            r = client.get(
                "/api/security/audit",
                headers={"Authorization": "Bearer ls_inspector_wrong"},
            )
            self.assertEqual(r.status_code, 401)


class InspectorEnvTokenStrippingTests(unittest.TestCase):
    BASE_TOKEN = "ls_inspector_" + "f" * 32

    def _run_with_token(self, raw: str) -> str:
        saved = {
            k: os.environ.get(k) for k in (
                service_auth.BYPASS_ENV,
                "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
                "LOCAL_SCRIBE_INSPECTOR_TOKEN",
            )
        }
        for k in saved:
            os.environ.pop(k, None)
        os.environ["LOCAL_SCRIBE_INSPECTOR_TOKEN"] = raw
        try:
            with mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                side_effect=AssertionError("must not unlock"),
            ):
                holder = inspector_server._resolve_token_holder_at_startup()
                assert holder is not None
                return holder.token
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
