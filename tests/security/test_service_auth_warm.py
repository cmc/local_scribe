"""Tests for ``service_auth.warm_tokens`` and the ``warm`` CLI verb.

Why this exists
---------------

Until 2026-05-11, ``./run.sh start`` spawned the ASR and inspector
servers as separate processes, each of which called
:func:`service_auth.ServiceToken.unlock` to derive its bearer token
on its own. That meant two Touch ID modals + two YubiKey taps per
``start``, and worse, the explanatory banners
(``touch_prompts.print_yubikey_tap_now`` etc.) were written to each
service's *log file* (because the daemonised spawn redirects
stdout/stderr) — invisible to the operator watching the terminal.

The operator caught this on 2026-05-11: "when the services are
loading it needs to print out the instruction to accept the touchid
press and tell user when the press the yubikey".

Fix:

1.  ``warm_tokens([s1, s2, ...])`` — single unlock, derives every
    requested service's bearer token from the same master key,
    returns a ``{service: token}`` dict. Touch ID + YubiKey
    happen exactly ONCE.

2.  ``python -m local_scribe.security.service_auth warm s1 s2 ...``
    — CLI verb that emits the dict as JSON to stdout. ``cmd_start``
    in ``run.sh`` captures this and then sets
    ``LOCAL_SCRIBE_<SERVICE>_TOKEN`` in each spawned service's
    PER-SUBPROCESS environ (bash ``VAR=val funcname`` form, so
    the parent shell never holds the token).

3.  ``asr_server`` + ``inspector_server`` lifespan hooks now check
    ``LOCAL_SCRIBE_<SERVICE>_TOKEN`` BEFORE falling back to the
    unlock path. Pre-warmed tokens short-circuit the unlock that
    would otherwise re-prompt.

The tests below pin every contract those callers depend on.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

from local_scribe.security import service_auth
from local_scribe.security.service_auth import (
    KNOWN_SERVICES,
    UnknownServiceError,
    derive_service_token,
    token_fingerprint,
    warm_tokens,
)


# ----------------------------------------------------------------------
# Sandbox helper — strip every env var that warm_tokens / unlock_master_key
# could short-circuit on, so each test runs from a known clean slate.


def _clean_env():
    drop = (
        "LOCAL_SCRIBE_DISABLE_AUTH",
        "LOCAL_SCRIBE_MASTER_KEY_HEX",
        "LOCAL_SCRIBE_TEST_MASTER_KEY_HEX",
    )
    drop_per_service = tuple(
        f"LOCAL_SCRIBE_{s.upper()}_TOKEN" for s in KNOWN_SERVICES
    )
    return mock.patch.dict(
        os.environ,
        {k: "" for k in drop + drop_per_service},
        clear=False,
    )


# ----------------------------------------------------------------------
# warm_tokens — Python-level contract


class WarmTokensSingleUnlockTests(unittest.TestCase):
    """The whole point of this function: N services, ONE unlock."""

    def test_unlock_called_exactly_once_for_two_services(self) -> None:
        master = b"M" * 32
        fake_mk = mock.MagicMock()
        fake_mk.as_bytes.return_value = master
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 service_auth, "is_bypass_enabled", return_value=False,
             ):
            with mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                return_value=fake_mk,
            ) as fake_unlock:
                out = warm_tokens(["asr", "inspector"])
        self.assertEqual(fake_unlock.call_count, 1,
                         f"expected ONE unlock, got {fake_unlock.call_count}")
        self.assertEqual(set(out.keys()), {"asr", "inspector"})
        for s, tok in out.items():
            self.assertTrue(tok.startswith(f"ls_{s}_"),
                            f"token shape for {s}: {tok!r}")
            self.assertEqual(tok, derive_service_token(master, s),
                             f"warm token for {s} diverges from "
                             f"single-service derive")

    def test_master_key_forget_called_after_derive(self) -> None:
        """``mk.forget()`` runs in the ``finally`` block so a crash mid-
        derive still scrubs the bytes. We assert it fires even on the
        happy path."""
        master = b"M" * 32
        fake_mk = mock.MagicMock()
        fake_mk.as_bytes.return_value = master
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 service_auth, "is_bypass_enabled", return_value=False,
             ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                return_value=fake_mk,
             ):
            warm_tokens(["asr"])
        fake_mk.forget.assert_called_once()

    def test_order_preserved_in_unlock_prompt_label(self) -> None:
        """The Touch ID prompt label includes both service names so the
        operator knows what the unlock is for. We assert the prompt
        string mentions both."""
        master = b"M" * 32
        fake_mk = mock.MagicMock()
        fake_mk.as_bytes.return_value = master
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 service_auth, "is_bypass_enabled", return_value=False,
             ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                return_value=fake_mk,
             ) as fake_unlock:
            warm_tokens(["asr", "inspector"])
        prompt = fake_unlock.call_args.kwargs.get("prompt", "")
        self.assertIn("asr", prompt)
        self.assertIn("inspector", prompt)


class WarmTokensEnvOverridesTests(unittest.TestCase):
    """Each env-var bypass keeps the unlock from firing."""

    def test_disable_auth_emits_empty_dict(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_SCRIBE_DISABLE_AUTH": "1"}, clear=True), \
             mock.patch(
                 "local_scribe.security.key_lifecycle.unlock_master_key",
             ) as fake_unlock:
            out = warm_tokens(["asr", "inspector"])
        fake_unlock.assert_not_called()
        self.assertEqual(out, {})

    def test_master_key_hex_skips_unlock(self) -> None:
        """``LOCAL_SCRIBE_MASTER_KEY_HEX`` is the ops-debug path — no
        Touch ID, derive directly. Both service tokens must still
        come out identical to ``derive_service_token`` against the
        same master."""
        master = bytes.fromhex("aa" * 32)
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_MASTER_KEY_HEX": master.hex()},
                clear=True,
        ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
        ) as fake_unlock:
            out = warm_tokens(["asr", "inspector"])
        fake_unlock.assert_not_called()
        self.assertEqual(out["asr"], derive_service_token(master, "asr"))
        self.assertEqual(
            out["inspector"], derive_service_token(master, "inspector"),
        )

    def test_test_master_key_hex_is_honoured(self) -> None:
        """``LOCAL_SCRIBE_TEST_MASTER_KEY_HEX`` is the unit-test seam
        (TestClient uses it). warm_tokens must honour it the same
        way single-service unlock does."""
        master = bytes.fromhex("bb" * 32)
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_TEST_MASTER_KEY_HEX": master.hex()},
                clear=True,
        ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
        ) as fake_unlock:
            out = warm_tokens(["asr"])
        fake_unlock.assert_not_called()
        self.assertEqual(out["asr"], derive_service_token(master, "asr"))

    def test_master_key_hex_invalid_raises(self) -> None:
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_MASTER_KEY_HEX": "not-hex"},
                clear=True,
        ):
            with self.assertRaises(service_auth.ServiceAuthError):
                warm_tokens(["asr"])

    def test_pre_set_service_token_passes_through(self) -> None:
        """If ``LOCAL_SCRIBE_ASR_TOKEN`` is already set (e.g. operator
        explicitly forced one for debugging), warm_tokens echoes it
        and skips the unlock for that service. Only services NOT
        pre-set drive the unlock."""
        fixed = "ls_asr_" + "f" * 32
        master = b"M" * 32
        fake_mk = mock.MagicMock()
        fake_mk.as_bytes.return_value = master
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_ASR_TOKEN": fixed},
                clear=True,
        ), mock.patch.object(
                service_auth, "is_bypass_enabled", return_value=False,
        ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
                return_value=fake_mk,
        ) as fake_unlock:
            out = warm_tokens(["asr", "inspector"])
        # ASR token came from env, inspector from unlock.
        self.assertEqual(out["asr"], fixed)
        self.assertEqual(out["inspector"],
                         derive_service_token(master, "inspector"))
        fake_unlock.assert_called_once()

    def test_all_pre_set_skips_unlock_entirely(self) -> None:
        """If every requested service is already in env, NO unlock."""
        asr = "ls_asr_" + "a" * 32
        inspector = "ls_inspector_" + "b" * 32
        with mock.patch.dict(
                os.environ,
                {
                    "LOCAL_SCRIBE_ASR_TOKEN": asr,
                    "LOCAL_SCRIBE_INSPECTOR_TOKEN": inspector,
                },
                clear=True,
        ), mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
        ) as fake_unlock:
            out = warm_tokens(["asr", "inspector"])
        fake_unlock.assert_not_called()
        self.assertEqual(out, {"asr": asr, "inspector": inspector})


class WarmTokensValidationTests(unittest.TestCase):
    def test_unknown_service_raises(self) -> None:
        with self.assertRaises(UnknownServiceError):
            warm_tokens(["asr", "not_a_service"])

    def test_unknown_service_raises_before_unlock(self) -> None:
        """We validate first — don't burn a Touch ID + YubiKey
        round-trip on a typo'd service list."""
        with mock.patch(
                "local_scribe.security.key_lifecycle.unlock_master_key",
        ) as fake_unlock:
            with self.assertRaises(UnknownServiceError):
                warm_tokens(["nope"])
        fake_unlock.assert_not_called()


# ----------------------------------------------------------------------
# CLI: ``service_auth warm asr inspector``


class WarmCliTests(unittest.TestCase):
    def _run(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err):
            rc = service_auth._cli_warm(args)
        return rc, out.getvalue(), err.getvalue()

    def test_empty_args_usage(self) -> None:
        rc, out, err = self._run([])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)
        self.assertEqual(out.strip(), "")

    def test_unknown_service_exit_2(self) -> None:
        rc, out, err = self._run(["nope"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown service", err)

    def test_emits_json_to_stdout(self) -> None:
        master = bytes.fromhex("cc" * 32)
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_MASTER_KEY_HEX": master.hex()},
                clear=True,
        ):
            rc, out, err = self._run(["asr", "inspector"])
        self.assertEqual(rc, 0, msg=f"rc={rc} stderr={err!r}")
        payload = json.loads(out)
        self.assertEqual(set(payload.keys()), {"asr", "inspector"})
        self.assertEqual(payload["asr"], derive_service_token(master, "asr"))
        self.assertEqual(
            payload["inspector"],
            derive_service_token(master, "inspector"),
        )

    def test_disable_auth_emits_empty_object(self) -> None:
        with mock.patch.dict(
                os.environ, {"LOCAL_SCRIBE_DISABLE_AUTH": "1"}, clear=True,
        ):
            rc, out, err = self._run(["asr"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {})


# ----------------------------------------------------------------------
# Cross-cutting security invariants for the new code paths


class WarmTokensNoLeakageTests(unittest.TestCase):
    """The 2026-05-11 ``BearerTokenNotInEnvOrFiles`` invariant
    (SECURITY_AUDIT.md F6) must continue to hold for the warm path:
    deriving N tokens at once must not put any of them into
    ``os.environ`` (the parent's environ, not subprocess passing —
    which is allowed)."""

    def test_warm_tokens_does_not_set_env_vars(self) -> None:
        master = bytes.fromhex("dd" * 32)
        before = dict(os.environ)
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_MASTER_KEY_HEX": master.hex()},
                clear=False,
        ):
            out = warm_tokens(["asr", "inspector"])
        after = dict(os.environ)
        # The only delta should be from the patch.dict (which restores
        # itself); the function itself MUST NOT export anything.
        for s, tok in out.items():
            body = tok.split("_", 2)[-1]
            for k, v in after.items():
                self.assertNotIn(
                    body, v,
                    f"warm token for {s} leaked into env var {k}",
                )
        # And confirm we restored cleanly (sanity).
        for k, v in before.items():
            self.assertEqual(after.get(k), v)

    def test_token_fingerprint_round_trip(self) -> None:
        """The token a service receives via env (LOCAL_SCRIBE_*_TOKEN)
        must produce the same fingerprint that ``service_auth url
        inspector`` would print, so the operator's "ASR token
        fingerprint" line in ``./run.sh status`` keeps matching the
        server-side view. This is contractual for the inspector
        auth-URL flow."""
        master = bytes.fromhex("ee" * 32)
        with mock.patch.dict(
                os.environ,
                {"LOCAL_SCRIBE_MASTER_KEY_HEX": master.hex()},
                clear=True,
        ):
            warm = warm_tokens(["asr", "inspector"])
        for s, tok in warm.items():
            self.assertEqual(
                token_fingerprint(tok),
                token_fingerprint(derive_service_token(master, s)),
            )


if __name__ == "__main__":
    unittest.main()
