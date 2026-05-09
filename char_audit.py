"""Char.app safety audit.

Reads Char's persisted state (``settings.json`` + ``store.json``) and
reports whether it's still pointed at our local ASR server, whether
PostHog analytics is disabled, and which third-party provider URLs
remain at their defaults (informational -- only matters if the user
ever switches Char's STT/LLM provider away from our shim).

Used by:

* ``./run.sh doctor`` for a quick CLI summary
* ``inspector_server.py``'s ``/api/char/audit`` endpoint for the
  Char Audit tab in the web UI

Findings are returned as a structured list of ``Check`` records so
both surfaces can render them their own way. See ``CHAR_REVIEW.md``
for the full background on each check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

from config import Config


# Status sentinels used by every check. Kept as plain strings (not an
# Enum) so JSON serialisation is trivial and the inspector frontend
# can switch on them with no extra mapping.
OK = "ok"      # matches expectation
WARN = "warn"  # drift from expectation, actionable
INFO = "info"  # not a problem, just worth surfacing
MISS = "miss"  # config / file not present; can't tell


@dataclass
class Check:
    """One auditable assertion about Char's state."""
    key: str
    status: str
    current: Any = None
    expected: Any = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditReport:
    settings_path: str
    store_path: str
    settings_present: bool
    store_present: bool
    checks: list[Check]
    summary: dict[str, int]
    backups: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings_path": self.settings_path,
            "store_path": self.store_path,
            "settings_present": self.settings_present,
            "store_present": self.store_present,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary,
            "backups": self.backups,
        }


# Char's settings.json defaults its provider base_urls to the real
# vendor endpoints. We don't *force* these to localhost (the user has
# only chosen one of them as their `current_stt_provider`), but we
# surface any non-default value so an unexpected proxy URL would be
# caught.
_KNOWN_REAL_PROVIDER_BASE_URLS = {
    "deepgram": "https://api.deepgram.com",
    "assemblyai": "https://api.assemblyai.com",
    "gladia": "https://api.gladia.io",
    "soniox": "https://api.soniox.com",
    "aquavoice": "https://api.aquavoice.com",
    "elevenlabs": "https://api.elevenlabs.io",
    "fireworks": "https://api.fireworks.ai",
    "mistral": "https://api.mistral.ai",
    "pyannote": "https://api.pyannote.ai",
}


def _safe_load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _decode_scoped(value: Any) -> dict[str, Any]:
    """tauri-plugin-store2 wraps each scoped store value as a JSON-encoded
    string. Decode tolerantly: real dicts, JSON strings, anything else."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _list_backups(cfg: Config) -> list[str]:
    """Find every settings.json + OpenAI-key backup we've already
    written, so the inspector UI can offer to restore them."""
    out: list[str] = []
    settings_dir = cfg.char_data_dir
    if settings_dir.is_dir():
        out.extend(sorted(str(p) for p in settings_dir.glob("settings.json.bak.*")))
    backup_dir = Path.home() / ".config" / "local_scribe"
    if backup_dir.is_dir():
        out.extend(sorted(
            str(p) for p in backup_dir.glob("char-openai-key.*.txt")
        ))
    return out


def _mask(secret: str, keep: int = 4) -> str:
    """Render a key as ``sk-...XXXX`` so we don't echo full secrets in
    the audit JSON (the inspector frontend renders this verbatim)."""
    if not secret:
        return ""
    if len(secret) <= keep + 3:
        return "*" * len(secret)
    return f"{secret[:3]}...{secret[-keep:]}"


def audit(cfg: Config) -> AuditReport:
    """Run every check and return a structured report."""
    checks: list[Check] = []
    settings_path = cfg.char_settings_path
    store_path = cfg.char_store_path

    settings = _safe_load_json(settings_path)
    store = _safe_load_json(store_path)

    settings_present = settings is not None
    store_present = store is not None

    if not settings_present:
        checks.append(Check(
            key="char.settings.json",
            status=MISS,
            current=str(settings_path),
            note="Char hasn't been launched yet (or data dir is wrong)",
        ))
    else:
        ai = settings.get("ai") or {}
        oai = ((ai.get("stt") or {}).get("openai")) or {}

        prov = ai.get("current_stt_provider")
        checks.append(Check(
            key="ai.current_stt_provider",
            status=OK if prov == cfg.expected_stt_provider else WARN,
            current=prov,
            expected=cfg.expected_stt_provider,
            note=("matches local-scribe shim"
                  if prov == cfg.expected_stt_provider
                  else "Char will route transcription somewhere other than our shim"),
        ))

        model = ai.get("current_stt_model")
        checks.append(Check(
            key="ai.current_stt_model",
            status=OK if model == cfg.expected_stt_model else WARN,
            current=model,
            expected=cfg.expected_stt_model,
            note=("triggers Char's progressive (SSE) batch path"
                  if model == cfg.expected_stt_model
                  else "non-streaming model -- 60s BATCH_IDLE_TIMEOUT applies; long files will silently abort"),
        ))

        url = oai.get("base_url")
        url_status = OK if url == cfg.expected_stt_base_url else WARN
        checks.append(Check(
            key="ai.stt.openai.base_url",
            status=url_status,
            current=url,
            expected=cfg.expected_stt_base_url,
            note=("audio stays on this Mac"
                  if url_status == OK
                  else "audio would be POSTed to {!r} -- not the local ASR server".format(url)),
        ))

        api_key = oai.get("api_key") or ""
        if not api_key:
            checks.append(Check(
                key="ai.stt.openai.api_key",
                status=WARN,
                current="<unset>",
                note="Char rejects requests without an api_key; configure-char sets 'local'",
            ))
        elif api_key == "local":
            checks.append(Check(
                key="ai.stt.openai.api_key",
                status=OK,
                current="local",
                note="our placeholder -- the local ASR server ignores it",
            ))
        else:
            checks.append(Check(
                key="ai.stt.openai.api_key",
                status=INFO,
                current=_mask(api_key),
                note=(
                    "looks like a real OpenAI key. Even though base_url "
                    "is loopback, this string sits in plain text inside "
                    "settings.json -- consider rotating at "
                    "platform.openai.com/api-keys."
                ),
            ))

        # Surface non-default base_urls for every other STT provider in
        # case some past experiment left a proxy URL behind. Users who
        # never touched these will never see a non-default value.
        stt_section = (ai.get("stt") or {})
        for provider, default_url in _KNOWN_REAL_PROVIDER_BASE_URLS.items():
            cur = (stt_section.get(provider) or {}).get("base_url")
            if cur is None:
                continue
            if cur == default_url:
                checks.append(Check(
                    key=f"ai.stt.{provider}.base_url",
                    status=INFO,
                    current=cur,
                    note=("default upstream URL; only fires if Char's "
                          f"current_stt_provider is set to {provider!r}"),
                ))
            else:
                checks.append(Check(
                    key=f"ai.stt.{provider}.base_url",
                    status=WARN,
                    current=cur,
                    expected=default_url,
                    note=(f"non-default {provider} base_url -- review whether this "
                          "is something you set up deliberately"),
                ))

        # Char's "intelligence" (LLM) provider config. We don't pin
        # this in configure-char because Char's UI is the natural place
        # to set it, but we surface drift so the inspector can flag if
        # it's pointing at api.openai.com / api.mistral.ai / etc.
        intel = ai.get("intelligence") or {}
        cur_intel = ai.get("current_llm_provider")
        if cur_intel:
            checks.append(Check(
                key="ai.current_llm_provider",
                status=INFO,
                current=cur_intel,
                note="Char's intelligence (summary) provider; we recommend a local LM Studio target",
            ))
        for provider_name, provider_cfg in intel.items():
            if not isinstance(provider_cfg, dict):
                continue
            cur_url = provider_cfg.get("base_url")
            if not cur_url:
                continue
            is_local = "127.0.0.1" in cur_url or "localhost" in cur_url
            checks.append(Check(
                key=f"ai.intelligence.{provider_name}.base_url",
                status=OK if is_local else INFO,
                current=cur_url,
                note=("loopback -- summaries stay on this Mac"
                      if is_local
                      else "non-loopback URL; transcripts would be sent here when Char generates a summary"),
            ))

    # ---- store.json (analytics toggle, etc.) ----------------------
    if not store_present:
        checks.append(Check(
            key="char.store.json",
            status=MISS,
            current=str(store_path),
            note="Char hasn't written its scoped store yet",
        ))
    else:
        analytics = _decode_scoped(store.get("analytics"))
        disabled = bool(analytics.get("Disabled"))
        checks.append(Check(
            key="store.analytics.Disabled",
            status=OK if disabled else WARN,
            current=disabled,
            expected=True,
            note=("PostHog short-circuited at the is_disabled() check"
                  if disabled
                  else ("Char's PostHog analytics is ENABLED -- run "
                        "./run.sh configure-char or POST /api/char/configure")),
        ))

        # Updater info is informational only -- the auto-update channel
        # is not toggleable in-app per CHAR_REVIEW.md, so we just
        # surface the last-seen version it noticed.
        updater = _decode_scoped(store.get("updater2"))
        if "LastSeenVersion" in updater:
            checks.append(Check(
                key="store.updater2.LastSeenVersion",
                status=INFO,
                current=updater.get("LastSeenVersion"),
                note=("Char's auto-updater polls desktop2.hyprnote.com on a "
                      "timer -- block it at the firewall if you don't want "
                      "those checks. CHAR_REVIEW.md §Mitigations has the rules."),
            ))

    summary: dict[str, int] = {OK: 0, WARN: 0, INFO: 0, MISS: 0}
    for c in checks:
        summary[c.status] = summary.get(c.status, 0) + 1

    return AuditReport(
        settings_path=str(settings_path),
        store_path=str(store_path),
        settings_present=settings_present,
        store_present=store_present,
        checks=checks,
        summary=summary,
        backups=_list_backups(cfg),
    )


def configure_char(cfg: Config, *, backup_existing_key: bool = True) -> dict[str, Any]:
    """Equivalent of ``./run.sh configure-char``: rewrite the four
    settings.json keys + write the analytics-disabled flag. Returns a
    dict with ``ok`` (bool), ``settings_backup``, ``key_backup`` (or
    None), and ``before/after`` snapshots.

    Mirrors the Bash implementation so the inspector UI can offer
    one-click "fix it" without shelling out.
    """
    import time

    settings_path = cfg.char_settings_path
    store_path = cfg.char_store_path

    if not settings_path.is_file():
        return {
            "ok": False,
            "error": f"Char settings.json not found at {settings_path}; "
                     "open Char.app once first",
        }

    settings = json.loads(settings_path.read_text())

    # --- snapshot before for the response ----------------------------
    ai = settings.get("ai") or {}
    oai = ((ai.get("stt") or {}).get("openai")) or {}
    before = {
        "current_stt_provider": ai.get("current_stt_provider"),
        "current_stt_model": ai.get("current_stt_model"),
        "openai_base_url": oai.get("base_url"),
        "openai_api_key_preview": _mask(oai.get("api_key") or ""),
    }

    # --- backup the whole settings.json ------------------------------
    ts = time.strftime("%Y%m%d-%H%M%S")
    settings_backup = settings_path.with_name(settings_path.name + f".bak.{ts}")
    settings_backup.write_bytes(settings_path.read_bytes())

    # --- optional dedicated backup of an existing real key -----------
    key_backup_path: Optional[Path] = None
    cur_key = (oai.get("api_key") or "").strip()
    if backup_existing_key and cur_key and cur_key != "local":
        backup_dir = Path.home() / ".config" / "local_scribe"
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            backup_dir.chmod(0o700)
        except OSError:
            pass
        key_backup_path = backup_dir / f"char-openai-key.{ts}.txt"
        key_backup_path.write_text(cur_key + "\n")
        try:
            key_backup_path.chmod(0o600)
        except OSError:
            pass

    # --- patch the four keys -----------------------------------------
    ai = settings.setdefault("ai", {})
    stt = ai.setdefault("stt", {})
    oai = stt.setdefault("openai", {})
    ai["current_stt_provider"] = cfg.expected_stt_provider
    ai["current_stt_model"] = cfg.expected_stt_model
    oai["base_url"] = cfg.expected_stt_base_url
    oai["api_key"] = "local"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    # --- write/merge the analytics-disabled flag in store.json -------
    store: dict[str, Any] = {}
    if store_path.is_file():
        try:
            store = json.loads(store_path.read_text() or "{}")
        except json.JSONDecodeError:
            store = {}
    inner = _decode_scoped(store.get("analytics"))
    inner["Disabled"] = True
    store["analytics"] = json.dumps(inner, separators=(",", ":"))
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=4) + "\n")

    return {
        "ok": True,
        "before": before,
        "after": {
            "current_stt_provider": cfg.expected_stt_provider,
            "current_stt_model": cfg.expected_stt_model,
            "openai_base_url": cfg.expected_stt_base_url,
            "openai_api_key": "local",
        },
        "settings_backup": str(settings_backup),
        "key_backup": str(key_backup_path) if key_backup_path else None,
    }
