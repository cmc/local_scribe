"""Unit + integration tests for ``launch_session.py``.

Covers the bearer-suffix helpers, the lock-file lifecycle, the gate
decision matrix, and end-to-end integration with
``service_auth.make_token_dependency`` (a 403 when the lock is
stale).
"""

from __future__ import annotations

import json
import os
import time
import tempfile
import unittest
from pathlib import Path

from local_scribe.common import launch_session
from local_scribe.security import service_auth


class _SessionEnv(unittest.TestCase):
    """Base — isolates the LOCAL_SCRIBE_LAUNCH_ID / disable env vars
    and resets the module-level cache. Conftest defaults
    LOCAL_SCRIBE_DISABLE_LAUNCH_GATE=1, so tests that need to
    exercise the gate must clear it explicitly."""

    def setUp(self):
        self._env_snap = {
            launch_session.DISABLE_ENV: os.environ.pop(launch_session.DISABLE_ENV, None),
            launch_session.LAUNCH_ID_ENV: os.environ.pop(launch_session.LAUNCH_ID_ENV, None),
        }
        launch_session._reset_cache()

    def tearDown(self):
        for k, v in self._env_snap.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        launch_session._reset_cache()


# --- suffix helpers ----------------------------------------------


class SuffixHelperTests(_SessionEnv):

    def test_attach_then_extract_roundtrip(self):
        base = "ls_asr_" + "a" * 32
        launch_id = "1234567890abcdef" + "0" * 16
        bound = launch_session.attach_suffix(base, launch_id)
        self.assertTrue(bound.endswith(".ls" + launch_id[:16]))
        self.assertEqual(
            launch_session.extract_suffix(bound), launch_id[:16],
        )
        self.assertEqual(launch_session.strip_suffix(bound), base)

    def test_attach_is_idempotent(self):
        base = "ls_asr_" + "b" * 32
        launch_id = "deadbeefcafef00d" + "f" * 16
        bound1 = launch_session.attach_suffix(base, launch_id)
        bound2 = launch_session.attach_suffix(bound1, launch_id)
        self.assertEqual(bound1, bound2)

    def test_attach_replaces_old_suffix(self):
        base = "ls_asr_" + "c" * 32
        old = "0" * 32
        new = "1" * 32
        bound = launch_session.attach_suffix(base, old)
        rebound = launch_session.attach_suffix(bound, new)
        self.assertEqual(launch_session.extract_suffix(rebound), new[:16])
        self.assertEqual(launch_session.strip_suffix(rebound), base)

    def test_extract_returns_none_on_unbound(self):
        self.assertIsNone(launch_session.extract_suffix("ls_asr_abc"))
        self.assertIsNone(launch_session.extract_suffix("plain_token"))

    def test_extract_returns_none_on_wrong_suffix_form(self):
        # ``.xy<...>`` (not ``.ls``) is not our suffix.
        self.assertIsNone(launch_session.extract_suffix("ls_asr_abc.xyz1234567890abcd"))
        # Wrong length.
        self.assertIsNone(launch_session.extract_suffix("ls_asr_abc.ls12"))
        # Non-hex.
        self.assertIsNone(launch_session.extract_suffix("ls_asr_abc.lsZZZZZZZZZZZZZZZZ"))


# --- lock-file lifecycle ----------------------------------------


class LockLifecycleTests(_SessionEnv):

    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=123)
            launch_session.write_lock(s, p)
            self.assertTrue(p.is_file())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            loaded = launch_session._maybe_reload(p)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.launch_id, s.launch_id)
            self.assertEqual(loaded.parent_pid, 123)

    def test_close_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, p)
            self.assertTrue(p.is_file())
            launch_session.close_lock(p)
            self.assertFalse(p.is_file())

    def test_close_when_missing_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            # Must not raise.
            launch_session.close_lock(p)


# --- gate decision matrix --------------------------------------


class CheckBearerTests(_SessionEnv):

    def test_disabled_env_always_allows(self):
        os.environ[launch_session.DISABLE_ENV] = "1"
        outcome = launch_session.check_bearer("anything")
        self.assertTrue(outcome.allowed)
        self.assertFalse(outcome.bearer_was_bound)

    def test_unbound_bearer_always_allows(self):
        # No DISABLE_LAUNCH_GATE; no launch.lock; bearer carries no
        # ``.ls`` suffix → unbound → allowed (script invocation path).
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.lock"
            outcome = launch_session.check_bearer("ls_asr_xyz", p)
            self.assertTrue(outcome.allowed)
            self.assertFalse(outcome.bearer_was_bound)

    def test_bound_bearer_without_lock_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.lock"
            bound = launch_session.attach_suffix("ls_asr_x", "f" * 32)
            outcome = launch_session.check_bearer(bound, p)
            self.assertFalse(outcome.allowed)
            self.assertIn("launch.lock not present", outcome.reason)

    def test_bound_bearer_with_active_matching_lock_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, p)
            bound = launch_session.attach_suffix("ls_asr_x", s.launch_id)
            outcome = launch_session.check_bearer(bound, p)
            self.assertTrue(outcome.allowed, msg=outcome.reason)

    def test_bound_bearer_with_wrong_suffix_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, p)
            bound = launch_session.attach_suffix("ls_asr_x", "0" * 32)
            outcome = launch_session.check_bearer(bound, p)
            self.assertFalse(outcome.allowed)
            self.assertIn("stale launch", outcome.reason)

    def test_bound_bearer_after_close_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, p)
            bound = launch_session.attach_suffix("ls_asr_x", s.launch_id)
            self.assertTrue(launch_session.check_bearer(bound, p).allowed)
            launch_session.close_lock(p)
            outcome = launch_session.check_bearer(bound, p)
            self.assertFalse(outcome.allowed)

    def test_bound_bearer_with_expected_id_mismatch_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, p)
            bound = launch_session.attach_suffix("ls_asr_x", s.launch_id)
            # Service was started against a DIFFERENT launch_id.
            outcome = launch_session.check_bearer(
                bound, p, expected_id="b" * 32,
            )
            self.assertFalse(outcome.allowed)


# --- service_auth integration ----------------------------------


class ServiceAuthIntegrationTests(_SessionEnv):
    """``make_token_dependency`` now consults launch_session.
    Verify the 403 path when the gate denies."""

    def setUp(self):
        super().setUp()
        # Tests in this class explicitly DO want the gate enabled.
        # conftest sets DISABLE_LAUNCH_GATE=1 by default, override.
        os.environ.pop(launch_session.DISABLE_ENV, None)
        # Auth itself must also be enabled to reach the gate check.
        self._prior_auth = os.environ.pop("LOCAL_SCRIBE_DISABLE_AUTH", None)

    def tearDown(self):
        if self._prior_auth is not None:
            os.environ["LOCAL_SCRIBE_DISABLE_AUTH"] = self._prior_auth
        super().tearDown()

    def _build_app(self, holder, lock_path):
        """Build a tiny FastAPI app whose gated route uses
        ``make_token_dependency``. Returns a starlette TestClient."""
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        # Point launch_session at our temp lock by monkey-patching
        # its default at the module level. The gate check inside
        # service_auth doesn't pass a path through, so this is the
        # cleanest seam.
        launch_session.DEFAULT_LOCK_PATH = lock_path

        dep = service_auth.make_token_dependency(holder)
        app = FastAPI()

        @app.get("/gated", dependencies=[Depends(dep)])
        async def gated():
            return {"ok": True}

        return TestClient(app)

    def test_unbound_bearer_passes_when_gate_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            # NO launch.lock present.
            lock_path = Path(td) / "launch.lock"
            holder = service_auth.ServiceToken(
                service="asr",
                token="ls_asr_" + "a" * 32,
            )
            client = self._build_app(holder, lock_path)
            r = client.get(
                "/gated",
                headers={"Authorization": "Bearer " + holder.token},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)

    def test_bound_bearer_without_lock_403(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "launch.lock"
            holder = service_auth.ServiceToken(
                service="asr",
                token="ls_asr_" + "a" * 32,
            )
            client = self._build_app(holder, lock_path)
            bound = launch_session.attach_suffix(holder.token, "9" * 32)
            r = client.get(
                "/gated",
                headers={"Authorization": "Bearer " + bound},
            )
            self.assertEqual(r.status_code, 403, msg=r.text)
            self.assertEqual(
                r.json()["detail"]["error"]["type"], "launch_session",
            )
            self.assertIn(
                "launch.lock not present",
                r.json()["detail"]["error"]["message"],
            )

    def test_bound_bearer_with_matching_lock_200(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, lock_path)
            holder = service_auth.ServiceToken(
                service="asr",
                token="ls_asr_" + "a" * 32,
            )
            client = self._build_app(holder, lock_path)
            bound = launch_session.attach_suffix(holder.token, s.launch_id)
            r = client.get(
                "/gated",
                headers={"Authorization": "Bearer " + bound},
            )
            self.assertEqual(r.status_code, 200, msg=r.text)

    def test_bound_bearer_after_close_403(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "launch.lock"
            s = launch_session.new_session(parent_pid=1)
            launch_session.write_lock(s, lock_path)
            holder = service_auth.ServiceToken(
                service="asr",
                token="ls_asr_" + "a" * 32,
            )
            client = self._build_app(holder, lock_path)
            bound = launch_session.attach_suffix(holder.token, s.launch_id)
            r = client.get(
                "/gated", headers={"Authorization": "Bearer " + bound},
            )
            self.assertEqual(r.status_code, 200)
            launch_session.close_lock(lock_path)
            r2 = client.get(
                "/gated", headers={"Authorization": "Bearer " + bound},
            )
            self.assertEqual(r2.status_code, 403)

    def test_hkdf_compare_works_with_bound_token(self):
        # Sanity check: matches() correctly strips the suffix.
        holder = service_auth.ServiceToken(
            service="asr",
            token="ls_asr_" + "a" * 32,
        )
        bound = launch_session.attach_suffix(holder.token, "1" * 32)
        self.assertTrue(holder.matches(bound))
        # Wrong base under a valid-looking suffix is still rejected.
        self.assertFalse(holder.matches(
            launch_session.attach_suffix("ls_asr_" + "b" * 32, "1" * 32),
        ))


if __name__ == "__main__":
    unittest.main()
