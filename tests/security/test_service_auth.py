"""Tests for service_auth.py.

Covers:
    - HKDF determinism + distinct-per-service derivation
    - Token format (``ls_<service>_<32hex>``) + length
    - ``ServiceToken`` constant-time match (positive, negative, empty)
    - ``extract_candidate_token`` over every supported transport
      (Authorization Bearer / Token / raw, X-API-Key, ?api_key,
      cookie)
    - ``LOCAL_SCRIBE_DISABLE_AUTH`` bypass switch
    - FastAPI dependency: 401 on missing/wrong token, pass on right
      token (exercises the real fastapi.HTTPException path).
"""

from __future__ import annotations

import os
import unittest

from local_scribe.security import service_auth
from local_scribe.security.service_auth import (
    BYPASS_ENV,
    HKDF_SALT,
    KNOWN_SERVICES,
    TOKEN_BYTES,
    ServiceAuthError,
    ServiceToken,
    UnknownServiceError,
    derive_service_token,
    extract_candidate_token,
    hkdf_sha256,
    is_bypass_enabled,
    make_token_dependency,
    random_token_for_tests,
    token_fingerprint,
)


# ---------- HKDF + derivation --------------------------------------


class HkdfTests(unittest.TestCase):
    """RFC 5869 sanity. We don't drag in the spec's test vectors (would
    bloat the test file); a few sanity properties are enough since the
    derivation is exercised end-to-end by the rest of the suite."""

    def test_length_respected(self):
        out = hkdf_sha256(ikm=b"k" * 32, salt=b"s", info=b"i", length=32)
        self.assertEqual(len(out), 32)

    def test_deterministic(self):
        a = hkdf_sha256(ikm=b"k" * 32, salt=b"s", info=b"i", length=32)
        b = hkdf_sha256(ikm=b"k" * 32, salt=b"s", info=b"i", length=32)
        self.assertEqual(a, b)

    def test_distinct_info_yields_distinct_output(self):
        a = hkdf_sha256(ikm=b"k" * 32, salt=b"s", info=b"foo", length=32)
        b = hkdf_sha256(ikm=b"k" * 32, salt=b"s", info=b"bar", length=32)
        self.assertNotEqual(a, b)

    def test_distinct_salt_yields_distinct_output(self):
        a = hkdf_sha256(ikm=b"k" * 32, salt=b"s1", info=b"i", length=32)
        b = hkdf_sha256(ikm=b"k" * 32, salt=b"s2", info=b"i", length=32)
        self.assertNotEqual(a, b)

    def test_distinct_ikm_yields_distinct_output(self):
        a = hkdf_sha256(ikm=b"a" * 32, salt=b"s", info=b"i", length=32)
        b = hkdf_sha256(ikm=b"b" * 32, salt=b"s", info=b"i", length=32)
        self.assertNotEqual(a, b)


# ---------- derive_service_token -----------------------------------


class DeriveServiceTokenTests(unittest.TestCase):

    MK = b"\x42" * 32

    def test_token_shape_and_length(self):
        t = derive_service_token(self.MK, "asr")
        # ls_<service>_<32 hex chars>
        self.assertTrue(t.startswith("ls_asr_"))
        hex_part = t[len("ls_asr_"):]
        self.assertEqual(len(hex_part), TOKEN_BYTES * 2)
        # All-hex (lowercase).
        int(hex_part, 16)  # raises if invalid
        self.assertEqual(hex_part, hex_part.lower())

    def test_deterministic(self):
        t1 = derive_service_token(self.MK, "asr")
        t2 = derive_service_token(self.MK, "asr")
        self.assertEqual(t1, t2)

    def test_distinct_per_service(self):
        a = derive_service_token(self.MK, "asr")
        i = derive_service_token(self.MK, "inspector")
        self.assertNotEqual(a, i)
        # Prefixes differ too (sanity).
        self.assertTrue(a.startswith("ls_asr_"))
        self.assertTrue(i.startswith("ls_inspector_"))

    def test_distinct_per_master_key(self):
        a = derive_service_token(b"\x01" * 32, "asr")
        b = derive_service_token(b"\x02" * 32, "asr")
        self.assertNotEqual(a, b)

    def test_unknown_service_raises(self):
        with self.assertRaises(UnknownServiceError):
            derive_service_token(self.MK, "lmstudio")
        with self.assertRaises(UnknownServiceError):
            derive_service_token(self.MK, "")
        with self.assertRaises(UnknownServiceError):
            derive_service_token(self.MK, "ASR")  # case sensitive

    def test_wrong_key_size_raises(self):
        with self.assertRaises(ValueError):
            derive_service_token(b"\x00" * 16, "asr")
        with self.assertRaises(ValueError):
            derive_service_token(b"", "asr")
        with self.assertRaises(ValueError):
            derive_service_token("not bytes", "asr")  # type: ignore[arg-type]

    def test_token_fingerprint_first_6_chars(self):
        t = derive_service_token(self.MK, "asr")
        fp = token_fingerprint(t)
        self.assertEqual(len(fp), 6)
        self.assertTrue(t.endswith(t.split("_")[-1]))
        self.assertEqual(t.split("_")[-1][:6], fp)

    def test_token_fingerprint_malformed(self):
        self.assertEqual(token_fingerprint("not-a-token"), "<malformed>")
        self.assertEqual(token_fingerprint("ls_asr_"), "<malformed>")

    def test_known_services_includes_asr_and_inspector(self):
        self.assertIn("asr", KNOWN_SERVICES)
        self.assertIn("inspector", KNOWN_SERVICES)


# ---------- ServiceToken.matches -----------------------------------


class ServiceTokenMatchTests(unittest.TestCase):

    MK = b"\x42" * 32

    def setUp(self):
        self.holder = ServiceToken.from_master_key(self.MK, "asr")

    def test_matches_self(self):
        self.assertTrue(self.holder.matches(self.holder.token))

    def test_rejects_different_token(self):
        bad = derive_service_token(b"\x00" * 32, "asr")
        self.assertNotEqual(bad, self.holder.token)
        self.assertFalse(self.holder.matches(bad))

    def test_rejects_empty(self):
        self.assertFalse(self.holder.matches(""))
        self.assertFalse(self.holder.matches(None))  # type: ignore[arg-type]

    def test_rejects_other_service(self):
        other = derive_service_token(self.MK, "inspector")
        self.assertFalse(self.holder.matches(other))

    def test_repr_does_not_leak_token(self):
        r = repr(self.holder)
        # Fingerprint is OK (it's only first 6 hex chars); the full
        # token must not appear in repr.
        self.assertNotIn(self.holder.token, r)


# ---------- extract_candidate_token --------------------------------


class _FakeRequest:
    """Minimal Request shim: headers, query_params, cookies."""

    class _Headers(dict):
        def get(self, key, default=None):  # type: ignore[override]
            # Case-insensitive
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            return default

    class _QP(dict):
        def get(self, key, default=None):  # type: ignore[override]
            return super().get(key, default)

    def __init__(self, *, headers=None, query_params=None, cookies=None):
        self.headers = self._Headers(headers or {})
        self.query_params = self._QP(query_params or {})
        self.cookies = cookies or {}


class ExtractCandidateTokenTests(unittest.TestCase):

    def test_authorization_bearer(self):
        r = _FakeRequest(headers={"Authorization": "Bearer abc123"})
        self.assertEqual(extract_candidate_token(r), "abc123")

    def test_authorization_token_scheme(self):
        # Deepgram-style — Char's Custom (live) provider uses this.
        r = _FakeRequest(headers={"Authorization": "Token xyz789"})
        self.assertEqual(extract_candidate_token(r), "xyz789")

    def test_authorization_bearer_case_insensitive(self):
        r = _FakeRequest(headers={"Authorization": "bearer abc"})
        self.assertEqual(extract_candidate_token(r), "abc")

    def test_authorization_raw_token_no_scheme(self):
        # Some clients omit the scheme. We permit long no-space values.
        r = _FakeRequest(headers={"Authorization": "ls_asr_" + "a" * 32})
        got = extract_candidate_token(r)
        self.assertEqual(got, "ls_asr_" + "a" * 32)

    def test_authorization_garbled_no_scheme_short(self):
        # Short / spaced values without a known scheme are NOT treated
        # as a raw token.
        r = _FakeRequest(headers={"Authorization": "X Y"})
        self.assertIsNone(extract_candidate_token(r))
        r2 = _FakeRequest(headers={"Authorization": "abc"})
        self.assertIsNone(extract_candidate_token(r2))

    def test_x_api_key_header(self):
        r = _FakeRequest(headers={"X-API-Key": "alpha"})
        self.assertEqual(extract_candidate_token(r), "alpha")

    def test_x_api_key_takes_priority_after_authorization(self):
        r = _FakeRequest(headers={
            "Authorization": "Bearer auth-tok",
            "X-API-Key": "header-tok",
        })
        # Authorization wins (it's more standard).
        self.assertEqual(extract_candidate_token(r), "auth-tok")

    def test_query_param_api_key(self):
        r = _FakeRequest(query_params={"api_key": "qpval"})
        self.assertEqual(extract_candidate_token(r), "qpval")

    def test_cookie_when_name_supplied(self):
        r = _FakeRequest(cookies={"ls_inspector": "cookieval"})
        got = extract_candidate_token(r, cookie_name="ls_inspector")
        self.assertEqual(got, "cookieval")

    def test_cookie_ignored_when_name_not_supplied(self):
        r = _FakeRequest(cookies={"ls_inspector": "cookieval"})
        self.assertIsNone(extract_candidate_token(r))

    def test_priority_order(self):
        # Bearer > Token > X-API-Key > query > cookie
        r = _FakeRequest(
            headers={"Authorization": "Bearer A", "X-API-Key": "B"},
            query_params={"api_key": "C"},
            cookies={"ls_inspector": "D"},
        )
        self.assertEqual(
            extract_candidate_token(r, cookie_name="ls_inspector"), "A",
        )

    def test_returns_none_when_no_source(self):
        r = _FakeRequest()
        self.assertIsNone(extract_candidate_token(r))


# ---------- bypass switch ------------------------------------------


class BypassTests(unittest.TestCase):

    def setUp(self):
        self._old = os.environ.get(BYPASS_ENV)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(BYPASS_ENV, None)
        else:
            os.environ[BYPASS_ENV] = self._old

    def test_default_off(self):
        os.environ.pop(BYPASS_ENV, None)
        self.assertFalse(is_bypass_enabled())

    def test_on_values(self):
        for v in ("1", "true", "yes", "on", "anything"):
            os.environ[BYPASS_ENV] = v
            self.assertTrue(is_bypass_enabled(),
                            f"value {v!r} should enable bypass")

    def test_off_values(self):
        for v in ("0", "false", "no", "off", ""):
            os.environ[BYPASS_ENV] = v
            self.assertFalse(is_bypass_enabled(),
                             f"value {v!r} should NOT enable bypass")


# ---------- FastAPI dependency (real fastapi.HTTPException path) ---


class FastAPIDependencyTests(unittest.TestCase):

    MK = b"\x42" * 32

    def setUp(self):
        # Some shells / CI runners set LOCAL_SCRIBE_DISABLE_AUTH=1 to
        # let the legacy FastAPI TestClient suites pass — clear it for
        # these tests so the *real* auth path is exercised.
        self._old_bypass = os.environ.pop(BYPASS_ENV, None)
        self.holder = ServiceToken.from_master_key(self.MK, "asr")
        self.dep = make_token_dependency(self.holder)
        import asyncio
        self.run = asyncio.run

    def tearDown(self):
        if self._old_bypass is not None:
            os.environ[BYPASS_ENV] = self._old_bypass

    def _call(self, req) -> Exception | None:
        """Run the dependency; return the HTTPException raised, or None."""
        try:
            self.run(self.dep(req))
            return None
        except Exception as exc:  # noqa: BLE001
            return exc

    def test_rejects_no_auth(self):
        from fastapi import HTTPException
        r = _FakeRequest()
        exc = self._call(r)
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 401)
        # Include the WWW-Authenticate header for spec compliance.
        self.assertIn("WWW-Authenticate", exc.headers or {})

    def test_rejects_wrong_token(self):
        from fastapi import HTTPException
        r = _FakeRequest(headers={"Authorization": "Bearer wrong"})
        exc = self._call(r)
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 401)

    def test_accepts_correct_bearer(self):
        r = _FakeRequest(headers={"Authorization": f"Bearer {self.holder.token}"})
        self.assertIsNone(self._call(r))

    def test_accepts_token_scheme(self):
        r = _FakeRequest(headers={"Authorization": f"Token {self.holder.token}"})
        self.assertIsNone(self._call(r))

    def test_accepts_x_api_key(self):
        r = _FakeRequest(headers={"X-API-Key": self.holder.token})
        self.assertIsNone(self._call(r))

    def test_accepts_query_param(self):
        r = _FakeRequest(query_params={"api_key": self.holder.token})
        self.assertIsNone(self._call(r))

    def test_accepts_cookie_when_enabled(self):
        dep = make_token_dependency(self.holder, cookie_name="ls_asr")
        r = _FakeRequest(cookies={"ls_asr": self.holder.token})
        import asyncio
        try:
            asyncio.run(dep(r))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"expected pass, got {exc!r}")

    def test_bypass_env_skips_check(self):
        old = os.environ.get(BYPASS_ENV)
        os.environ[BYPASS_ENV] = "1"
        try:
            r = _FakeRequest()  # no auth at all
            self.assertIsNone(self._call(r),
                              "bypass env should let unauth requests through")
        finally:
            if old is None:
                os.environ.pop(BYPASS_ENV, None)
            else:
                os.environ[BYPASS_ENV] = old


# ---------- callable token_holder (lifespan-init pattern) ----------


class CallableTokenHolderTests(unittest.TestCase):
    """The asr/inspector servers populate the token inside a FastAPI
    lifespan, *after* dependency decorators have been evaluated. To
    cope, ``make_token_dependency`` accepts a zero-arg callable that
    resolves the holder at request time."""

    MK = b"\x42" * 32

    def setUp(self):
        self._old_bypass = os.environ.pop(BYPASS_ENV, None)
        # Mutable cell so we can simulate "lifespan hasn't initialised
        # the token yet" → "lifespan completes; holder now non-None".
        self._holder_cell = {"v": None}
        self.dep = make_token_dependency(
            lambda: self._holder_cell["v"],
        )
        import asyncio
        self.run = asyncio.run

    def tearDown(self):
        if self._old_bypass is not None:
            os.environ[BYPASS_ENV] = self._old_bypass

    def _call(self, req):
        try:
            self.run(self.dep(req))
            return None
        except Exception as exc:  # noqa: BLE001
            return exc

    def test_returns_503_when_provider_returns_none(self):
        from fastapi import HTTPException
        r = _FakeRequest(headers={"Authorization": "Bearer anything"})
        exc = self._call(r)
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 503)
        detail = exc.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["error"]["type"], "auth_not_ready")

    def test_passes_through_after_provider_returns_holder(self):
        holder = ServiceToken.from_master_key(self.MK, "asr")
        self._holder_cell["v"] = holder
        r = _FakeRequest(headers={"Authorization": f"Bearer {holder.token}"})
        self.assertIsNone(self._call(r))

    def test_401_after_init_when_wrong_token(self):
        from fastapi import HTTPException
        holder = ServiceToken.from_master_key(self.MK, "asr")
        self._holder_cell["v"] = holder
        r = _FakeRequest(headers={"Authorization": "Bearer wrong"})
        exc = self._call(r)
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 401)


# ---------- random token + housekeeping ----------------------------


class MiscTests(unittest.TestCase):
    def test_random_token_for_tests_has_test_prefix(self):
        t = random_token_for_tests()
        self.assertTrue(t.startswith("ls_test_"))
        self.assertEqual(len(t[len("ls_test_"):]), TOKEN_BYTES * 2)

    def test_hkdf_salt_versioned(self):
        # Sanity: the salt embeds the derivation version so a bump
        # invalidates old tokens.
        from local_scribe.security.service_auth import DERIVATION_VERSION
        self.assertIn(str(DERIVATION_VERSION).encode(), HKDF_SALT)


# ---------- Client-side helper (transcribe_file / redo_session) ----


class ClientAuthHeaderTests(unittest.TestCase):
    """``client_auth_header_for`` is the helper that
    transcribe_file.py + redo_session.py use to send the bearer token
    to a gated ASR endpoint. It resolves a token from (env var, master
    key env var, or Keychain) and returns a ready-to-merge headers
    dict."""

    MK = b"\x42" * 32

    def setUp(self):
        # Snapshot + restore env so tests don't bleed state.
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "LOCAL_SCRIBE_DISABLE_AUTH",
                "LOCAL_SCRIBE_ASR_TOKEN",
                "LOCAL_SCRIBE_INSPECTOR_TOKEN",
                "LOCAL_SCRIBE_MASTER_KEY_HEX",
                "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
            )
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_bypass_returns_empty_header(self):
        os.environ["LOCAL_SCRIBE_DISABLE_AUTH"] = "1"
        h = service_auth.client_auth_header_for("asr")
        self.assertEqual(h, {})

    def test_token_env_var_wins(self):
        os.environ["LOCAL_SCRIBE_ASR_TOKEN"] = "ls_asr_explicitly_set"
        h = service_auth.client_auth_header_for("asr")
        self.assertEqual(h, {"Authorization": "Bearer ls_asr_explicitly_set"})

    def test_master_key_env_derives_correctly(self):
        os.environ["LOCAL_SCRIBE_MASTER_KEY_HEX"] = self.MK.hex()
        h = service_auth.client_auth_header_for("asr")
        # Authorization: Bearer ls_asr_<32 hex>
        self.assertTrue(h["Authorization"].startswith("Bearer ls_asr_"))
        expected = service_auth.derive_service_token(self.MK, "asr")
        self.assertEqual(h["Authorization"], f"Bearer {expected}")

    def test_token_style_token_uses_deepgram_scheme(self):
        os.environ["LOCAL_SCRIBE_ASR_TOKEN"] = "ls_asr_xyz"
        h = service_auth.client_auth_header_for("asr", style="token")
        self.assertEqual(h, {"Authorization": "Token ls_asr_xyz"})

    def test_unknown_service_raises(self):
        with self.assertRaises(service_auth.UnknownServiceError):
            service_auth.client_auth_header_for("does_not_exist")

    def test_bad_master_key_hex_raises(self):
        os.environ["LOCAL_SCRIBE_MASTER_KEY_HEX"] = "not hex at all"
        with self.assertRaises(service_auth.ServiceAuthError):
            service_auth.client_auth_header_for("asr")

    def test_env_var_priority_explicit_token_beats_master_key(self):
        # Explicit per-service token always wins over master-key-derived
        # so an ops admin can override a single service mid-deploy.
        os.environ["LOCAL_SCRIBE_ASR_TOKEN"] = "ls_asr_override"
        os.environ["LOCAL_SCRIBE_MASTER_KEY_HEX"] = self.MK.hex()
        h = service_auth.client_auth_header_for("asr")
        self.assertEqual(h, {"Authorization": "Bearer ls_asr_override"})


if __name__ == "__main__":
    unittest.main()
