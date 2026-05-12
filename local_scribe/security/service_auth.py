"""Per-service bearer-token authentication for the local_scribe HTTP APIs.

Threat model addressed
----------------------

Before this module, every HTTP service (ASR :8000, Inspector :8001) was
*loopback-only* — which keeps remote attackers out, but does nothing
against an attacker who has landed any kind of local code execution
(malicious browser extension making CORS requests, a different user on
the same Mac, a shell on a compromised account, a Tauri app that's not
Char). Anyone able to ``curl http://127.0.0.1:8000/...`` could submit
audio for transcription, read Char's session list out of the inspector,
or worse.

After this module, every gated endpoint demands a per-service Bearer
token. The tokens are *derived from the Keychain master key* using
HKDF-SHA256, so they share the same root of trust as the on-disk vault:

    master_key  (32 random bytes, Keychain item, Touch-ID gated)
        │
        ├─ HKDF(info=b"asr") ────────► asr token       ◄── ASR :8000
        ├─ HKDF(info=b"inspector") ──► inspector token ◄── Inspector :8001
        └─ HKDF(info=b"...") ────────► future services

Why HKDF + deterministic derivation rather than separate stored tokens:

  * Single secret to back up (master key → YubiKey-encrypted .age file).
    Recover the master key, recover every token.
  * Tokens don't change unless the master key rotates. Char's stored
    OpenAI api_key stays valid across reboots / re-mounts of the vault.
  * No extra ciphertext on disk = nothing for an attacker to scrape.
  * NIST SP 800-108 / RFC 5869 covered; HKDF is the textbook KDF for
    exactly this "one master, many use-specific subkeys" pattern.

Accepted headers
----------------

Servers accept any of these (case-insensitive comparison, constant-time
match against the derived token):

  * ``Authorization: Bearer <token>``  (OpenAI / standard)
  * ``Authorization: Token <token>``   (Deepgram / Char's Custom provider)
  * ``X-API-Key: <token>``             (legacy / curl-friendly)
  * ``?api_key=<token>``               (query string; **not** logged in
                                       FastAPI access logs by default,
                                       but still avoid for non-test use)
  * Cookie ``ls_<service>=<token>``    (inspector browser only)

Token format
------------

A token is a 32-character lowercase hex string (16 random bytes from
HKDF), prefixed for easy log-grepping:

    ls_asr_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6
    ls_inspector_9988776655443322112233445566778899

The prefix is informational only; verification is over the full string.

Bypass
------

For CI / scripted testing, set ``LOCAL_SCRIBE_DISABLE_AUTH=1``. The
service logs a *loud* warning on startup when the bypass is active. Do
not set this in production.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from local_scribe.security.secret_store import MasterKey


logger = logging.getLogger("local_scribe.service_auth")


# ---------------------------------------------------------------------------
# Constants
#
# Versioned so we can rotate the derivation parameters in the future
# (e.g. switch to BLAKE3 or a higher byte count) without invalidating
# tokens in flight for users who haven't redeployed yet — the salt
# embeds the version.

DERIVATION_VERSION = 1
HKDF_SALT = b"local_scribe.service_auth.v" + str(DERIVATION_VERSION).encode()

# 16 bytes = 128-bit token. Plenty for a localhost bearer — even a
# determined offline brute-force would need 2^127 average guesses, and
# every probe is a 401 from a single-threaded uvicorn worker.
TOKEN_BYTES = 16

# Known services. Adding a new one is "append here + use it from the
# new service module"; the verification side reads the service name
# from the caller, not this list, so this is purely documentation /
# allow-list for ``derive_service_token``.
KNOWN_SERVICES = ("asr", "inspector")

# Env-var that disables auth entirely. Surfaced + logged so users can't
# claim ignorance. Off by default.
BYPASS_ENV = "LOCAL_SCRIBE_DISABLE_AUTH"


# ---------------------------------------------------------------------------
# Exceptions

class ServiceAuthError(Exception):
    """Generic auth-layer failure."""


class UnknownServiceError(ServiceAuthError, ValueError):
    """Caller asked for a token for a service we don't recognise."""


# ---------------------------------------------------------------------------
# Derivation
#
# HKDF-SHA256 per RFC 5869. We do it by hand rather than pulling in the
# ``cryptography`` package since the construction is ~15 lines and the
# rest of the codebase already takes a hard line on minimal new deps
# (parakeet-mlx + sherpa-onnx are the heavy hitters; we don't want to
# add cryptography on top for one KDF call).


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 §2.2: PRK = HMAC-Hash(salt, IKM)."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 §2.3: T(N) blocks until ``length`` bytes accumulated."""
    if length > 255 * hashlib.sha256().digest_size:
        raise ValueError("length too large for HKDF-SHA256")
    out = b""
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out += t
        counter += 1
    return out[:length]


def hkdf_sha256(*, ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """Convenience wrapper. ``info`` is the per-purpose label
    ("service:asr", "service:inspector"); ``salt`` should encode the
    application + derivation version (see ``HKDF_SALT``)."""
    return _hkdf_expand(_hkdf_extract(salt, ikm), info, length)


def derive_service_token(master_key: bytes, service: str) -> str:
    """Derive the bearer token for ``service`` from ``master_key``.

    Same (key, service) tuple deterministically yields the same token,
    which is what lets Char hold onto its saved OpenAI api_key across
    server restarts.
    """
    if service not in KNOWN_SERVICES:
        # Soft-fail with a typed exception so callers can branch on it,
        # rather than letting a typo silently produce a token that
        # nothing will accept.
        raise UnknownServiceError(
            f"unknown service {service!r}; known: {KNOWN_SERVICES}"
        )
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != 32:
        raise ValueError(
            f"master_key must be 32 bytes, got {len(master_key)} "
            f"({type(master_key).__name__})"
        )
    raw = hkdf_sha256(
        ikm=bytes(master_key),
        salt=HKDF_SALT,
        info=f"service:{service}".encode("ascii"),
        length=TOKEN_BYTES,
    )
    return f"ls_{service}_{raw.hex()}"


def token_fingerprint(token: str) -> str:
    """First 6 hex chars after the prefix — safe to log."""
    parts = token.split("_", 2)
    if len(parts) != 3 or not parts[2]:
        return "<malformed>"
    return parts[2][:6]


def is_bypass_enabled() -> bool:
    """Auth-bypass switch. Reads ``LOCAL_SCRIBE_DISABLE_AUTH`` fresh
    every call so tests can flip it via ``os.environ``."""
    v = (os.environ.get(BYPASS_ENV) or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------------------
# In-process token holder
#
# Each service builds one of these at startup. The token (a hex string)
# isn't sensitive in the same way the master key is — it's only valid
# for the lifetime of the running service and is freely shared with
# Char as an "API key" — but we still hide it from ``repr`` to keep
# accidental log lines safe.


@dataclass
class ServiceToken:
    service: str
    token: str

    @classmethod
    def unlock(cls, service: str, *,
               prompt: Optional[str] = None) -> "ServiceToken":
        """Prompt Touch ID, fetch the master key, derive + cache the
        token for ``service``. The master key is zeroed immediately
        after derivation so it doesn't linger in memory longer than
        necessary."""
        if service not in KNOWN_SERVICES:
            raise UnknownServiceError(
                f"unknown service {service!r}; known: {KNOWN_SERVICES}"
            )
        if prompt is None:
            prompt = (
                f"Unlock local_scribe to start the {service} server"
            )
        # Route through key_lifecycle so we get the split-key (Option C)
        # flow on v2 installs and an implicit migration on v1 installs.
        # The legacy ``MasterKey.unlock`` direct path is kept around for
        # secret_store unit tests but is no longer used by production
        # code paths.
        from local_scribe.security import key_lifecycle  # local import: keep service_auth importable
                              # without key_lifecycle's dependencies
                              # (yubikey_backup) on bare-metal CI.
        mk = key_lifecycle.unlock_master_key(prompt=prompt)
        try:
            token = derive_service_token(mk.as_bytes(), service)
        finally:
            mk.forget()
        logger.info("service_auth: derived %s token (fingerprint=%s)",
                    service, token_fingerprint(token))
        return cls(service=service, token=token)

    @classmethod
    def from_master_key(cls, mk: bytes, service: str) -> "ServiceToken":
        """Test / advanced-use constructor: derive without unlocking
        the Keychain. ``mk`` must be 32 bytes."""
        return cls(service=service, token=derive_service_token(mk, service))

    def matches(self, candidate: Optional[str]) -> bool:
        """Constant-time match against ``candidate``. ``None``/empty
        always rejects.

        A candidate carrying a ``.ls<short_id>`` suffix (the
        launch-session-bound form produced by ``configure-char``) is
        first reduced to its base by stripping the suffix. The
        suffix itself is validated by the *launch gate* in
        :mod:`launch_session` -- :func:`make_token_dependency` calls
        that check separately, AFTER this method matches, so the
        constant-time guarantee on the HKDF compare is preserved
        even on a malformed suffix.
        """
        if not candidate:
            return False
        # Lazy import: keeps this module pure-Python for tests that
        # don't pull in launch_session.
        try:
            from local_scribe.common.launch_session import strip_suffix as _strip
            base = _strip(candidate)
        except Exception:  # noqa: BLE001
            base = candidate
        return hmac.compare_digest(self.token, base)

    def __repr__(self) -> str:  # noqa: D401
        return f"<ServiceToken {self.service} fp={token_fingerprint(self.token)}>"


# ---------------------------------------------------------------------------
# FastAPI dependency
#
# We defer the FastAPI import to call time so tests + non-FastAPI
# consumers of derive_service_token() don't take the import hit.


def extract_candidate_token(request, *,
                            cookie_name: Optional[str] = None) -> Optional[str]:
    """Pull the token from a starlette / fastapi Request. Searches, in
    order:

        1. ``Authorization: Bearer <t>``
        2. ``Authorization: Token <t>``       (Deepgram-style)
        3. ``X-API-Key: <t>``
        4. ``?api_key=<t>``  query param
        5. cookie ``cookie_name=<t>``  (when ``cookie_name`` is set)

    Returns the first non-empty candidate found, or ``None``."""
    # Lazy import to avoid making service_auth.py import-time depend on
    # starlette / fastapi. The unit tests build a tiny shim Request.
    auth = (request.headers.get("authorization") or "").strip()
    if auth:
        lower = auth.lower()
        for scheme in ("bearer ", "token "):
            if lower.startswith(scheme):
                tok = auth[len(scheme):].strip()
                if tok:
                    return tok
        # Some callers send the raw token in Authorization without a
        # scheme; permit that too.
        if " " not in auth and len(auth) > 16:
            return auth
    xapi = (request.headers.get("x-api-key") or "").strip()
    if xapi:
        return xapi
    try:
        qp = request.query_params.get("api_key") if hasattr(request, "query_params") else None
    except Exception:  # noqa: BLE001
        qp = None
    if qp:
        return qp
    if cookie_name:
        try:
            cookies = getattr(request, "cookies", {}) or {}
            cv = cookies.get(cookie_name)
            if cv:
                return cv
        except Exception:  # noqa: BLE001
            pass
    return None


def make_token_dependency(token_holder, *,
                          cookie_name: Optional[str] = None):
    """Return a FastAPI dependency callable that 401s on missing / wrong
    token. ``cookie_name`` enables cookie-based auth (used by the
    inspector web UI).

    ``token_holder`` may be either:

      * a ``ServiceToken`` instance (resolved at dependency-creation
        time -- works when the token is known up front), OR
      * a *zero-arg callable* returning a ``ServiceToken`` or ``None``
        (resolved at request time -- works when the token is populated
        inside a FastAPI ``lifespan`` *after* the route decorators have
        already been evaluated).

    The callable form returns 503 when the provider returns ``None``
    (auth not initialised yet -- request hit during startup).

    Importing fastapi inside the closure keeps this module pure-Python
    for unit tests that don't have fastapi available."""
    from fastapi import HTTPException, Request, status as http_status  # type: ignore

    if callable(token_holder) and not isinstance(token_holder, ServiceToken):
        token_provider = token_holder
        _service_label = None  # determined lazily below
    else:
        const = token_holder
        token_provider = lambda: const  # noqa: E731
        _service_label = token_holder.service

    # IMPORTANT: this module has ``from __future__ import annotations``,
    # which means the ``request: Request`` annotation below is a *string*
    # at runtime, not the actual class. FastAPI's parameter classifier
    # can't resolve the string from inside this closure's scope, so it
    # falls back to treating ``request`` as a query parameter — every
    # gated POST then returns 422 "missing query param 'request'".
    #
    # We patch it after the fact by writing the real Request class into
    # ``_dep.__annotations__`` so ``inspect.signature(_dep)`` reports the
    # resolved type. Verified against fastapi >= 0.110 in the e2e test
    # suite.
    async def _dep(request: Request) -> None:
        if is_bypass_enabled():
            return
        holder = token_provider()
        if holder is None:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {
                    "type": "auth_not_ready",
                    "message": ("authentication not initialised yet — "
                                "if this persists, the Touch ID prompt "
                                "may have been dismissed during startup; "
                                "restart with `./run.sh restart`"),
                }},
            )
        service_label = _service_label or holder.service
        candidate = extract_candidate_token(request, cookie_name=cookie_name)
        if not candidate or not holder.matches(candidate):
            # 401 with a generic message; we don't reveal whether the
            # token was missing vs. wrong (mostly to avoid noise — for a
            # localhost-only service it makes no real difference).
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail={"error": {
                    "type": "auth",
                    "service": service_label,
                    "message": "missing or invalid bearer token",
                    "hint": (
                        "Char users: this is normal on first run — re-run "
                        "`./run.sh configure-char` to write the current "
                        "ASR token into Char's OpenAI api_key field. "
                        "Browser users: open the URL printed by "
                        "`./run.sh status` to set the inspector cookie."
                    ),
                }},
                headers={"WWW-Authenticate": f"Bearer realm={service_label!r}"},
            )

        # Layer C — launch-session gate. The HKDF-derived bearer is
        # otherwise valid; we additionally require that a
        # ``.ls<short_id>``-bound bearer matches the active
        # ``launch.lock`` written by ``./run.sh start``. This is
        # what makes Char's saved api_key invalid the moment the
        # run.sh that wrote it has exited — even if the master key
        # and HKDF derivation haven't changed.
        try:
            from local_scribe.common import launch_session  # local lazy import
        except ImportError:
            launch_session = None  # type: ignore[assignment]
        if launch_session is not None and not launch_session.is_gate_disabled():
            outcome = launch_session.check_bearer(
                candidate,
                expected_id=launch_session.expected_launch_id(),
            )
            if not outcome.allowed:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail={"error": {
                        "type": "launch_session",
                        "service": service_label,
                        "message": outcome.reason,
                        "hint": (
                            "This token is bound to a specific "
                            "`./run.sh start` invocation that is no "
                            "longer active. Run `./run.sh start` to "
                            "begin a new session, then "
                            "`./run.sh configure-char` to refresh "
                            "Char's saved api_key."
                        ),
                    }},
                )

    # See the IMPORTANT comment above the def.
    _dep.__annotations__["request"] = Request
    return _dep


# ---------------------------------------------------------------------------
# Test / introspection helpers


def random_token_for_tests() -> str:
    """Generate a random fake token for tests that want to exercise the
    "wrong token" path without setting up a full Keychain mock."""
    return f"ls_test_{secrets.token_hex(TOKEN_BYTES)}"


# ---------------------------------------------------------------------------
# Client-side helper: read a token for outbound requests
#
# transcribe_file.py + redo_session.py POST to the ASR server from the
# same machine. They need the bearer token to pass the gate. Rather
# than each script reimplementing the unlock-Keychain-and-derive flow,
# we centralise it here. Resolution order:
#
#   1. ``LOCAL_SCRIBE_<SERVICE>_TOKEN`` env var, e.g.
#      ``LOCAL_SCRIBE_ASR_TOKEN=ls_asr_...`` (full token, prefixed).
#      Used by remote / cross-machine setups where the script doesn't
#      have access to the *server's* Keychain.
#
#   2. ``LOCAL_SCRIBE_MASTER_KEY_HEX`` env var (64 hex chars). Derive
#      via HKDF. Less common; mainly for ops debugging.
#
#   3. Keychain lookup via ``MasterKey.unlock()`` (Touch ID prompt).
#      This is the default path on the same Mac as the ASR server.
#
#   4. ``LOCAL_SCRIBE_DISABLE_AUTH=1`` → return an empty header (don't
#      send anything; server will accept anyway because it's also
#      bypassed).
#
# ``client_auth_header_for("asr")`` returns a dict suitable for
# ``requests.post(headers=...)``.


def client_token_for(service: str, *,
                     prompt: Optional[str] = None) -> Optional[str]:
    """Return the token to use when CALLING ``service``. See module
    docstring for the resolution order. ``None`` means bypass-mode."""
    if service not in KNOWN_SERVICES:
        raise UnknownServiceError(
            f"unknown service {service!r}; known: {KNOWN_SERVICES}"
        )
    if is_bypass_enabled():
        return None
    # Env var: full token (most explicit / least magic).
    env_var = f"LOCAL_SCRIBE_{service.upper()}_TOKEN"
    val = os.environ.get(env_var)
    if val:
        return val.strip()
    # Env var: master key hex (for ops debugging without touching the
    # actual Keychain).
    mk_hex = os.environ.get("LOCAL_SCRIBE_MASTER_KEY_HEX") \
        or os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
    if mk_hex:
        try:
            mk = bytes.fromhex(mk_hex.strip())
        except ValueError as exc:
            raise ServiceAuthError(
                f"LOCAL_SCRIBE_MASTER_KEY_HEX not valid hex: {exc}"
            ) from exc
        return derive_service_token(mk, service)
    # Default: split-key unlock (Touch ID + YubiKey tap).
    from local_scribe.security import key_lifecycle
    mk = key_lifecycle.unlock_master_key(
        prompt=prompt or f"Authenticate local_scribe to call the {service} server"
    )
    try:
        return derive_service_token(mk.as_bytes(), service)
    finally:
        mk.forget()


def client_auth_header_for(service: str, *,
                           prompt: Optional[str] = None,
                           style: str = "bearer") -> dict[str, str]:
    """Return an ``Authorization`` header dict for outbound HTTP calls.

    ``style`` selects between:

      * ``bearer`` (default) — ``Authorization: Bearer <token>``
        (OpenAI / Char's batch / transcribe_file.py)

      * ``token`` — ``Authorization: Token <token>``
        (Deepgram / Char's Custom / live recording)

    Returns ``{}`` when auth is bypassed."""
    tok = client_token_for(service, prompt=prompt)
    if not tok:
        return {}
    scheme = "Bearer" if style.lower() == "bearer" else "Token"
    return {"Authorization": f"{scheme} {tok}"}


__all__ = [
    "DERIVATION_VERSION",
    "HKDF_SALT",
    "TOKEN_BYTES",
    "KNOWN_SERVICES",
    "BYPASS_ENV",
    "ServiceAuthError",
    "UnknownServiceError",
    "hkdf_sha256",
    "derive_service_token",
    "token_fingerprint",
    "is_bypass_enabled",
    "extract_candidate_token",
    "make_token_dependency",
    "ServiceToken",
    "random_token_for_tests",
    "client_token_for",
    "client_auth_header_for",
    "warm_tokens",
]


# ---------------------------------------------------------------------------
# CLI entry point
#
# Lets run.sh fetch / display tokens with a single shell line:
#
#   ASR_TOKEN="$($VENV_PY -m service_auth token asr)"   # prompts Touch ID
#   $VENV_PY -m service_auth fingerprint asr            # safe-to-log first 6
#   $VENV_PY -m service_auth url inspector              # auth URL for browser
#   $VENV_PY -m service_auth health                     # are deps OK?
#
# All commands honour ``LOCAL_SCRIBE_DISABLE_AUTH`` and the test env
# vars so CI / scripted setups don't need a real Keychain.


def _cli_token(args: list[str]) -> int:
    if len(args) != 1 or args[0] not in KNOWN_SERVICES:
        print(f"usage: token <{'|'.join(KNOWN_SERVICES)}>", file=__import__("sys").stderr)
        return 2
    try:
        tok = client_token_for(
            args[0],
            prompt=f"Authenticate local_scribe to print the {args[0]} token",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 1
    if tok is None:
        # Bypass mode → nothing to print but exit 0 so the caller can
        # branch on empty output.
        return 0
    print(tok)
    return 0


def _cli_fingerprint(args: list[str]) -> int:
    if len(args) != 1 or args[0] not in KNOWN_SERVICES:
        print(f"usage: fingerprint <{'|'.join(KNOWN_SERVICES)}>",
              file=__import__("sys").stderr)
        return 2
    try:
        tok = client_token_for(args[0])
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 1
    if tok is None:
        print("<bypass>")
        return 0
    print(token_fingerprint(tok))
    return 0


def _cli_url(args: list[str]) -> int:
    """Build a clickable auth URL for the inspector. Used by run.sh
    start / status so the user can click once to authenticate the
    browser without copy-pasting the token."""
    if len(args) != 1 or args[0] not in ("inspector",):
        print("usage: url inspector", file=__import__("sys").stderr)
        return 2
    # Resolve bind/port from config so the URL matches what the user's
    # inspector is actually listening on.
    try:
        from local_scribe.common.config import load_config
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"error loading config: {exc}", file=__import__("sys").stderr)
        return 1
    try:
        tok = client_token_for("inspector",
                               prompt="Authenticate local_scribe to print the inspector auth URL")
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 1
    if tok is None:
        print(f"http://{cfg.inspector_bind}:{cfg.inspector_port}/  (auth bypass)")
        return 0
    print(f"http://{cfg.inspector_bind}:{cfg.inspector_port}/auth?token={tok}")
    return 0


def warm_tokens(services: list[str], *,
                prompt: Optional[str] = None) -> dict[str, str]:
    """Derive bearer tokens for ``services`` from a SINGLE master-key
    unlock.

    Each entry in :data:`KNOWN_SERVICES` is HKDF-derived from the
    same master key, so we can amortise the (Touch ID + YubiKey)
    cost across all services that need a token at startup. Without
    this primitive, ``cmd_start`` ends up calling
    :func:`ServiceToken.unlock` once per service, which means N
    Touch ID modals + N YubiKey taps before the pipeline is up — a
    UX disaster surfaced by an operator on 2026-05-11 ("when the
    services are loading it needs to print out the instruction to
    accept the touchid press and tell user when the press the
    yubikey").

    Honours the same env-var overrides as
    :func:`client_token_for`:

    * ``LOCAL_SCRIBE_DISABLE_AUTH=1`` → returns ``{}``.
    * ``LOCAL_SCRIBE_<SERVICE>_TOKEN`` → already-set tokens pass
      through unchanged for that service.
    * ``LOCAL_SCRIBE_MASTER_KEY_HEX`` / ``LOCAL_SCRIBE_TEST_MASTER_KEY_HEX``
      → derive without prompting (ops debugging / tests).

    Returns a ``{service: token}`` mapping in the same order as
    ``services``. Raises :class:`UnknownServiceError` on an unknown
    service name (we deliberately fail fast rather than silently
    drop the bad entry — the caller is run.sh and an empty token
    map would surface as a confusing "auth failure" downstream).
    """
    for s in services:
        if s not in KNOWN_SERVICES:
            raise UnknownServiceError(
                f"unknown service {s!r}; known: {KNOWN_SERVICES}"
            )
    if is_bypass_enabled():
        return {}

    out: dict[str, str] = {}

    # Phase 1: collect anything pre-set via per-service env. We do
    # this BEFORE the unlock so a partial-set environment (e.g. ASR
    # token is in env but inspector isn't) only prompts once for the
    # missing one. If EVERY service is pre-set, we never unlock at
    # all and the caller's banner is suppressed downstream.
    needs_unlock: list[str] = []
    for s in services:
        env_var = f"LOCAL_SCRIBE_{s.upper()}_TOKEN"
        val = os.environ.get(env_var)
        if val:
            out[s] = val.strip()
        else:
            needs_unlock.append(s)
    if not needs_unlock:
        return out

    # Phase 2: master-key env-var paths (no Touch ID).
    mk_hex = os.environ.get("LOCAL_SCRIBE_MASTER_KEY_HEX") \
        or os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
    if mk_hex:
        try:
            mk_bytes = bytes.fromhex(mk_hex.strip())
        except ValueError as exc:
            raise ServiceAuthError(
                f"LOCAL_SCRIBE_MASTER_KEY_HEX not valid hex: {exc}"
            ) from exc
        for s in needs_unlock:
            out[s] = derive_service_token(mk_bytes, s)
        return out

    # Phase 3: the real unlock path. This is the ONE Touch ID +
    # ONE YubiKey tap that covers every service in ``needs_unlock``.
    # The banners fire from inside ``unlock_master_key`` so the
    # operator sees both prompts attributed to the same "service
    # warmup" event.
    from local_scribe.security import key_lifecycle  # noqa: PLC0415 — see note in client_token_for
    services_label = " + ".join(needs_unlock)
    mk = key_lifecycle.unlock_master_key(
        prompt=(prompt
                or f"Unlock local_scribe to start {services_label}"),
    )
    try:
        for s in needs_unlock:
            out[s] = derive_service_token(mk.as_bytes(), s)
    finally:
        mk.forget()
    return out


def _cli_warm(args: list[str]) -> int:
    """``warm <service ...>`` — derive tokens for multiple services
    from a single unlock and emit them as JSON to stdout. Used by
    ``run.sh cmd_start`` so the operator sees ONE Touch ID + ONE
    YubiKey prompt instead of one per service.

    Output shape (stdout):

        {"asr": "ls_asr_<hex>", "inspector": "ls_inspector_<hex>"}

    Bypass mode (``LOCAL_SCRIBE_DISABLE_AUTH=1``) emits ``{}`` and
    exits 0; the caller branches on emptiness.
    """
    import json as _json
    import sys as _sys
    if not args:
        _sys.stderr.write(
            f"usage: warm <{'|'.join(KNOWN_SERVICES)}> "
            f"[<{'|'.join(KNOWN_SERVICES)}> ...]\n"
        )
        return 2
    try:
        tokens = warm_tokens(args)
    except UnknownServiceError as exc:
        _sys.stderr.write(f"error: {exc}\n")
        return 2
    except Exception as exc:  # noqa: BLE001
        _sys.stderr.write(f"error: {exc}\n")
        return 1
    _sys.stdout.write(_json.dumps(tokens) + "\n")
    return 0


def _cli_health(_args: list[str]) -> int:
    """Quick diagnostic dump: are the bypass + Keychain helper paths
    sane? Doesn't actually unlock anything (no Touch ID), just shows
    what would happen if we did."""
    import sys
    from local_scribe.security.secret_store import has_master_key, helper_path
    out = {
        "bypass_enabled": is_bypass_enabled(),
        "keychain_helper_path": str(helper_path()),
        "keychain_helper_present": helper_path().is_file(),
        "master_key_in_keychain": False,
        "known_services": list(KNOWN_SERVICES),
        "derivation_version": DERIVATION_VERSION,
    }
    try:
        out["master_key_in_keychain"] = has_master_key()
    except Exception as exc:  # noqa: BLE001
        out["master_key_in_keychain_error"] = str(exc)
    import json as _json
    print(_json.dumps(out, indent=2))
    return 0


def _cli_main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python -m service_auth <token|fingerprint|url|health> [args...]",
            file=__import__("sys").stderr,
        )
        return 2
    cmd, rest = argv[1], argv[2:]
    dispatch = {
        "token": _cli_token,
        "fingerprint": _cli_fingerprint,
        "url": _cli_url,
        "health": _cli_health,
        "warm": _cli_warm,
    }
    handler = dispatch.get(cmd)
    if handler is None:
        print(f"unknown command: {cmd}", file=__import__("sys").stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv))
