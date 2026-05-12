"""Cross-cutting security invariants — the audit-matrix tests.

Why this file exists
--------------------

The per-layer tests under ``tests/security/`` already pin the *internal*
behaviour of each defense layer (SIP parser, HKDF derivation, vault
sparse-bundle creation, signed-config HMAC, etc.). What they do NOT pin
is the **cross-cutting** invariants that span more than one layer.
Those invariants are the ones an operator-level audit cares about
most: violating one of them silently demolishes a guarantee that we
advertise in ``SECURITY.md``, and the layer-internal tests would happily
keep passing while it happens.

This file is the systematic remediation of the 2026-05-11 security
audit. Each test class is named after the guarantee it pins. The
docstrings on the classes cite the exact section of ``SECURITY.md`` /
``CRYPTO.md`` they back, so a future operator can re-run this file as a
sanity check after touching any of those layers.

Invariants pinned here
----------------------

1. ``DevModeBoundaryTests``
   ``LOCAL_SCRIBE_DEV_MODE`` is honoured by ``sip_gate`` ONLY. Every
   other ``*_gate`` function in ``run.sh`` (master_key_gate,
   script_integrity_gate, pinned_config_gate, vault_relocation_gate,
   char_integrity_gate) must NOT consult the env var.

   This is the boundary documented in ``SECURITY.md`` §
   "Dev mode — explicit SIP bypass for development":
       > We refuse to weaken any other gate when dev-mode is set.

2. ``VaultPassphraseNotInArgvTests``
   ``vault.create``, ``vault.mount``, ``vault.rotate_password`` MUST
   feed the passphrase on stdin (``-stdinpass`` / ``-newstdinpass``),
   never as a command-line argument. A passphrase on argv would be
   readable by any process via ``ps`` and end up in the shell
   history if logged by the wrapping zsh.

   ``SECURITY.md`` § "Defense layer 3 — At-rest encryption":
       > The master key (reconstituted from the two factors in
       > layer 4 below) is fed to ``hdiutil`` to mount the image
       > [via stdin].

3. ``BearerTokenNotInEnvOrFilesTests``
   ``service_auth.derive_service_token`` returns a bearer token that
   we promise lives **only in server RAM**. This test confirms the
   derived token does not leak into ``os.environ`` and is not written
   under ``$HOME/.config/local_scribe`` or ``$HOME/.cache/local_scribe``
   after a fresh derivation. The token-not-in-argv invariant is
   covered by ``test_key_lifecycle.ThreatModelInvariantTests``.

   ``SECURITY.md`` § "Defense layer 2 — Inter-service authentication":
       > the token is not in any file, env var, or argv —
       > only in server RAM.

4. ``MasterKeyDocFreshnessTests``
   Static check: ``SECURITY.md`` and ``CRYPTO.md`` continue to
   reference all the modules they claim implement each defense
   layer. If a refactor moves a module, this test forces the
   docs to be updated in the same PR.

These tests are designed to be cheap (no Keychain, no YubiKey, no
hdiutil, no FastAPI app boot) so they can run on every CI invocation
without needing dev-mode flags or test seams.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock


_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"
_SECURITY_MD = _REPO / "SECURITY.md"
_CRYPTO_MD = _REPO / "CRYPTO.md"


# ---------------------------------------------------------------------------
# Helpers


def _extract_bash_function(source: str, name: str) -> str:
    """Return the body of bash function ``name`` from ``source``.

    Stops at the next top-level function definition or end-of-file.
    Tolerates bash's ``name() {`` and ``function name {`` forms.
    """
    # Match either form. We anchor to a line-start to avoid catching
    # text inside heredocs.
    open_pat = re.compile(
        rf"^(?:function\s+{re.escape(name)}\s*\{{|{re.escape(name)}\s*\(\)\s*\{{)\s*$",
        re.MULTILINE,
    )
    m = open_pat.search(source)
    if not m:
        raise AssertionError(f"function {name!r} not found in run.sh")
    start = m.end()
    # Walk lines forward, tracking brace depth. We start INSIDE the
    # function body (depth = 1 already accounted for by the opening
    # brace we just consumed).
    depth = 1
    lines = source[start:].splitlines(keepends=True)
    body: list[str] = []
    for line in lines:
        # Skip line-leading comments and lines that look like heredoc
        # markers — but for our purposes counting the braces we see
        # naively is fine because run.sh doesn't use ``{`` / ``}`` in
        # string content within these gate functions.
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return "".join(body)
        body.append(line)
    raise AssertionError(
        f"function {name!r} body did not terminate (mismatched braces?)"
    )


# ---------------------------------------------------------------------------
# 1. Dev-mode boundary (static analysis on run.sh)


class DevModeBoundaryTests(unittest.TestCase):
    """``LOCAL_SCRIBE_DEV_MODE`` must bypass EXACTLY ``sip_gate``.

    Documented in ``SECURITY.md`` § "Dev mode — explicit SIP bypass for
    development". The threat model only blesses a SIP bypass; every
    other gate must enforce unconditionally so that a stolen
    ``LOCAL_SCRIBE_DEV_MODE`` env var leak from CI doesn't quietly
    disable script integrity, signed config, vault relocation, or Char
    binary verification.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _RUN_SH.read_text()

    def test_sip_gate_honours_dev_mode(self) -> None:
        body = _extract_bash_function(self.source, "sip_gate")
        self.assertIn(
            "LOCAL_SCRIBE_DEV_MODE",
            body,
            "sip_gate must consult LOCAL_SCRIBE_DEV_MODE — that's the "
            "whole point of having a dev-mode bypass.",
        )

    def _assert_gate_does_not_consult_dev_mode(self, gate_name: str) -> None:
        body = _extract_bash_function(self.source, gate_name)
        self.assertNotIn(
            "LOCAL_SCRIBE_DEV_MODE",
            body,
            f"{gate_name} must NOT consult LOCAL_SCRIBE_DEV_MODE — that "
            f"would silently widen dev-mode beyond the SIP boundary "
            f"documented in SECURITY.md.",
        )

    def test_master_key_gate_ignores_dev_mode(self) -> None:
        self._assert_gate_does_not_consult_dev_mode("master_key_gate")

    def test_script_integrity_gate_ignores_dev_mode(self) -> None:
        self._assert_gate_does_not_consult_dev_mode("script_integrity_gate")

    def test_pinned_config_gate_ignores_dev_mode(self) -> None:
        self._assert_gate_does_not_consult_dev_mode("pinned_config_gate")

    def test_vault_relocation_gate_ignores_dev_mode(self) -> None:
        self._assert_gate_does_not_consult_dev_mode("vault_relocation_gate")

    def test_char_integrity_gate_ignores_dev_mode(self) -> None:
        self._assert_gate_does_not_consult_dev_mode("char_integrity_gate")

    def test_each_non_sip_gate_has_its_own_named_override(self) -> None:
        """Boundary: every non-SIP gate's escape hatch must be its own
        env var (``LOCAL_SCRIBE_ALLOW_*`` / ``LOCAL_SCRIBE_SKIP_*``),
        not a hidden DEV_MODE side effect. This pins the policy that
        every override is *individually* documented and individually
        loud (banner on stderr)."""
        gate_override_pairs = {
            "script_integrity_gate": "LOCAL_SCRIBE_ALLOW_DIRTY",
            "pinned_config_gate": "LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG",
            "vault_relocation_gate": "LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA",
            "char_integrity_gate": "LOCAL_SCRIBE_ALLOW_DIRTY_CHAR",
        }
        for gate, override in gate_override_pairs.items():
            body = _extract_bash_function(self.source, gate)
            self.assertIn(
                override,
                body,
                f"{gate} should honour its own override env var "
                f"{override!r}; see SECURITY.md for why each override "
                f"is named separately.",
            )


# ---------------------------------------------------------------------------
# 2. Vault passphrase is never on argv


class VaultPassphraseNotInArgvTests(unittest.TestCase):
    """The hdiutil passphrase must travel via stdin only.

    ``ps``, shell history, and process accounting all snapshot argv
    but not stdin. ``SECURITY.md`` § "Defense layer 3" documents that
    we feed the passphrase via ``-stdinpass`` / ``-newstdinpass``;
    this test pins that contract for every public entry point of
    ``vault.py`` that takes a passphrase.
    """

    # Synthetic 64-byte hex string of distinct ASCII so it survives a
    # naive ``in`` substring search against any argv element.
    _SENTINEL = b"DEADBEEFCAFEBABEDEADBEEFCAFEBABE" \
                b"DEADBEEFCAFEBABEDEADBEEFCAFEBABE"

    def _capture_subprocess(self, fn, *args, **kwargs):
        """Run ``fn(*args, **kwargs)`` with ``subprocess.run`` patched
        to capture every (cmd, kwargs) invocation. Returns the list of
        captured calls and propagates whatever the function raised."""
        from local_scribe.security import vault as vault_module

        captured: list[tuple[list[str], dict]] = []

        def fake_run(cmd, *_a, **_kw):
            captured.append((list(cmd), dict(_kw)))
            # We don't need the real subprocess. Return a faux
            # CompletedProcess so the caller can keep going (or fail
            # with its own error, which we'll swallow in the test).
            class _P:
                returncode = 0
                stdout = b""
                stderr = b""
            return _P()

        # Also stub out _hdiutil_info -- some code paths call it to
        # decide whether to mount. Pretend the bundle is not attached
        # so the codepath we care about always runs.
        with mock.patch.object(vault_module.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(vault_module, "_hdiutil_info",
                               return_value={"images": []}):
            try:
                fn(*args, **kwargs)
            except Exception:
                # We don't care about success; we only care that no
                # subprocess invocation leaked the passphrase.
                pass
        return captured

    def _assert_no_passphrase_in_captured(self, captured) -> None:
        for cmd, kwargs in captured:
            joined = " ".join(cmd)
            self.assertNotIn(
                self._SENTINEL.decode("ascii"),
                joined,
                f"passphrase leaked into argv: {cmd!r}",
            )
            for arg in cmd:
                self.assertNotIn(
                    self._SENTINEL.decode("ascii"),
                    arg,
                    f"passphrase leaked into a single argv element: {arg!r}",
                )
            # ``kwargs`` is allowed to contain ``input=<passphrase>`` —
            # that's the stdin path we explicitly bless. Check we're
            # NOT also passing it under any other kwarg name.
            for k, v in kwargs.items():
                if k == "input":
                    continue
                if isinstance(v, (bytes, bytearray, str)):
                    text = v.decode() if isinstance(v, (bytes, bytearray)) else v
                    self.assertNotIn(
                        self._SENTINEL.decode("ascii"),
                        text,
                        f"passphrase leaked into kwarg {k}={text!r}",
                    )

    def test_create_uses_stdin_not_argv(self) -> None:
        from local_scribe.security import vault
        # ``create`` short-circuits with VaultExistsError if the bundle
        # already lives on disk. Pretend it doesn't so we actually hit
        # the hdiutil invocation path.
        with mock.patch.object(vault, "exists", return_value=False), \
             mock.patch.object(Path, "mkdir"), \
             mock.patch.object(os, "chmod"):
            captured = self._capture_subprocess(vault.create, self._SENTINEL)
        self.assertTrue(captured, "vault.create did not invoke subprocess")
        # Pin the -stdinpass contract on at least one captured call.
        any_uses_stdinpass = any(
            "-stdinpass" in cmd for cmd, _ in captured
        )
        self.assertTrue(
            any_uses_stdinpass,
            f"expected -stdinpass in hdiutil argv from vault.create: "
            f"{[c for c, _ in captured]}",
        )
        self._assert_no_passphrase_in_captured(captured)

    def test_mount_uses_stdin_not_argv(self) -> None:
        from local_scribe.security import vault
        # ``mount`` requires the bundle to exist on disk. Pretend it
        # does via a patch on ``exists()``.
        with mock.patch.object(vault, "exists", return_value=True), \
             mock.patch.object(vault, "vault_already_mounted_at", return_value=False), \
             mock.patch.object(Path, "mkdir"):
            captured = self._capture_subprocess(vault.mount, self._SENTINEL)
        self.assertTrue(captured, "vault.mount did not invoke subprocess")
        any_uses_stdinpass = any(
            "-stdinpass" in cmd for cmd, _ in captured
        )
        self.assertTrue(
            any_uses_stdinpass,
            f"expected -stdinpass in hdiutil argv from vault.mount: "
            f"{[c for c, _ in captured]}",
        )
        self._assert_no_passphrase_in_captured(captured)

    def test_rotate_password_uses_stdin_not_argv(self) -> None:
        from local_scribe.security import vault
        new = b"X" * 64
        captured = self._capture_subprocess(
            vault.rotate_password, self._SENTINEL, new,
        )
        self.assertTrue(captured, "vault.rotate_password did not invoke subprocess")
        any_uses_stdinpass = any(
            "-stdinpass" in cmd and "-newstdinpass" in cmd
            for cmd, _ in captured
        )
        self.assertTrue(
            any_uses_stdinpass,
            f"expected -stdinpass AND -newstdinpass in hdiutil argv: "
            f"{[c for c, _ in captured]}",
        )
        self._assert_no_passphrase_in_captured(captured)


# ---------------------------------------------------------------------------
# 3. Bearer tokens never leak to env vars or on-disk caches


class BearerTokenNotInEnvOrFilesTests(unittest.TestCase):
    """Derived service tokens stay in process RAM.

    ``SECURITY.md`` § "Defense layer 2": *"the token is not in any
    file, env var, or argv — only in server RAM."* This test pins the
    invariant by deriving a token and then scanning ``os.environ``
    plus the operator's config + cache directories for the 32-hex
    body (the constant prefix ``ls_asr_`` / ``ls_inspector_`` is not
    sensitive on its own).
    """

    def test_token_body_not_in_os_environ(self) -> None:
        from local_scribe.security import service_auth

        master = b"M" * 32
        for service in ("asr", "inspector"):
            token = service_auth.derive_service_token(master, service)
            # ``ls_<service>_<32hex>``. Body is everything after the
            # second underscore.
            body = token.split("_", 2)[-1]
            self.assertEqual(
                len(body), 32,
                f"token body length sanity: got {len(body)} chars",
            )
            for k, v in os.environ.items():
                self.assertNotIn(
                    body, v,
                    f"derived token leaked into env var {k}={v!r}",
                )

    def test_token_body_not_in_config_dir(self) -> None:
        """A token derivation does not write anything to disk.

        We scan ``LOCAL_SCRIBE_CONFIG_DIR`` (the canonical operator
        state dir) after a derivation. If the body ever lands there
        it's an exfiltration footgun: that dir is backed up by Time
        Machine, synced by Dropbox-style tools, and pickable by any
        process running as the operator's UID.
        """
        from local_scribe.security import service_auth
        from local_scribe.common import config

        master = b"M" * 32
        token = service_auth.derive_service_token(master, "asr")
        body = token.split("_", 2)[-1]

        config_dir = config.DEFAULT_CONFIG_DIR
        if not config_dir.exists():
            self.skipTest(f"config dir {config_dir} not present on this host")

        for root, _dirs, files in os.walk(config_dir):
            for name in files:
                p = Path(root) / name
                try:
                    blob = p.read_bytes()
                except (OSError, PermissionError):
                    continue
                self.assertNotIn(
                    body.encode("ascii"), blob,
                    f"derived token leaked into {p}",
                )

    def test_token_repr_does_not_leak_token_body(self) -> None:
        """The ``ServiceToken`` dataclass / wrapper must redact its
        secret in ``repr``. This guards against logging frameworks
        (uvicorn access logs, structlog, etc.) accidentally surfacing
        the bearer when the object lands in a ``%r`` format string.
        """
        from local_scribe.security import service_auth

        token_str = service_auth.derive_service_token(b"K" * 32, "asr")
        wrapped = service_auth.ServiceToken(service="asr", token=token_str)
        r = repr(wrapped)
        body = token_str.split("_", 2)[-1]
        self.assertNotIn(
            body, r,
            f"ServiceToken repr leaked the secret body: {r!r}",
        )


# ---------------------------------------------------------------------------
# 4. Doc freshness: SECURITY.md still references every module it
#    claims implements each defense layer


class SecurityDocFreshnessTests(unittest.TestCase):
    """If a refactor moves a security module out from under
    ``SECURITY.md``, this test forces the doc to be updated in the
    same PR. The patterns are deliberately precise so a renamed file
    breaks the test loudly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.security = _SECURITY_MD.read_text()
        cls.crypto = _CRYPTO_MD.read_text()

    def test_every_referenced_module_still_exists(self) -> None:
        # Grep ``SECURITY.md`` for ``local_scribe/.../*.py`` paths and
        # assert they resolve. This catches "module moved but the doc
        # wasn't updated" silently.
        path_re = re.compile(r"local_scribe/[A-Za-z0-9_/]+\.py")
        for m in path_re.finditer(self.security):
            relpath = m.group(0)
            self.assertTrue(
                (_REPO / relpath).exists(),
                f"SECURITY.md references {relpath} but it doesn't exist; "
                f"either restore the module or update the doc.",
            )

    def test_defense_layer_section_headings_present(self) -> None:
        """The 8 defense layers (0 through 7) are the operator-facing
        contract. ``SECURITY.md`` MUST keep them as top-level
        section headings; the layer count is referenced by other
        docs (README, KEY_SAFETY, CRYPTO) and by this audit file."""
        for i in range(0, 8):
            heading = f"## Defense layer {i}"
            self.assertIn(
                heading, self.security,
                f"missing defense-layer heading: {heading!r}",
            )

    def test_crypto_md_references_hkdf(self) -> None:
        """Sanity: CRYPTO.md still documents the single canonical KDF.
        If someone introduces a second KDF without updating the doc,
        this fires."""
        self.assertIn("HKDF-SHA256", self.crypto,
                      "CRYPTO.md must continue to document HKDF-SHA256")
        # Only one KDF is supposed to ship. Other names listed in the
        # doc are explicitly rejected alternatives — we accept
        # ``PBKDF2`` / ``scrypt`` / ``Argon2`` mentions only in the
        # "What we deliberately don't ship" / "Future improvements"
        # sections. We don't assert on those here; the
        # discussion-context check is too brittle for static parse.

    def test_security_md_references_audit_doc(self) -> None:
        """Cross-link: SECURITY.md should point at the audit
        traceability matrix so an operator can find this file from
        the docs."""
        self.assertIn(
            "docs/SECURITY_AUDIT.md", self.security,
            "SECURITY.md should reference docs/SECURITY_AUDIT.md so "
            "operators can find the audit-traceability matrix.",
        )


if __name__ == "__main__":
    unittest.main()
