"""local_scribe configuration loader.

Single source of truth for ASR + LLM + inspector wiring. Three layers,
in order of increasing priority:

1.  Defaults baked into this module (DEFAULT_CONFIG below).
2.  ``~/.config/local_scribe/config.json`` if it exists.
3.  Environment variables (preserving backwards compatibility with the
    pre-config.json era — tests + old scripts that ``os.environ[...]``
    things keep working).

The inspector's PUT /api/config writes layer 2; layer 3 stays
authoritative at runtime so an operator can still override a single
field with an env var without re-editing the JSON.

Run ``./run.sh bootstrap`` to seed layer 2 with a copy of the defaults
on first install. The shape is documented in README §Configuration.
"""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


CONFIG_VERSION = 1

# Where the user-editable config lives. Stays under ~/.config so it
# survives ``rm -rf`` of the repo and gets per-user file ACLs by default
# (700 from XDG conventions). Path is exposed so the inspector can show
# it in the UI.
DEFAULT_CONFIG_DIR = Path(os.environ.get("LOCAL_SCRIBE_CONFIG_DIR") or
                          Path.home() / ".config" / "local_scribe")
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "asr": {
        "bind": "127.0.0.1",
        "port": 8000,
        # `parakeet` (Apple Silicon native, recommended) | `whisper`.
        # Whisper is fully supported but slower; included for users who
        # want a non-Apple-Silicon path or specifically need its language
        # coverage.
        "backend": "parakeet",
        "parakeet_model": "mlx-community/parakeet-tdt-0.6b-v3",
        "whisper_model": "large-v3-turbo",
        "stream_heartbeat_seconds": 20.0,
        "diarization": {
            "enabled": True,
            # 4 hours. Long enough for any plausible single-meeting
            # recording; short enough that a runaway diarization run on
            # an accidentally-long file (~10h podcast etc.) still gets
            # bounded. Set to 0 to remove the cap entirely.
            "max_seconds": 14400,
            "max_speakers": 12,
            "num_speakers": None,
            "cluster_threshold": None,
        },
    },
    "llm": {
        # Host + port are split (rather than a single URL) so the
        # inspector's `Run LM Studio on another Mac` UI can validate
        # them independently.
        "host": "127.0.0.1",
        "port": 1234,
        "model": "qwen3-30b-a3b-instruct-2507",
        "max_tokens": 4096,
        "temperature": 0.1,
    },
    "inspector": {
        # Always loopback by default. Documented in README §Privacy.
        "bind": "127.0.0.1",
        "port": 8001,
        # Optional bearer token. Off by default since loopback-only
        # bind already gates external access; turn it on if you ever
        # rebind to 0.0.0.0 for LAN access.
        "auth_token": None,
    },
    "char": {
        # null = use platform default (~/Library/Application Support/hyprnote)
        "data_dir": None,
        "expected_stt_provider": "openai",
        "expected_stt_model": "gpt-4o-transcribe",
    },
}


# Map env var name → dotted path into the config dict. Order matters
# only for predictable round-tripping; lookup is dict-direct.
_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    # ASR
    "ASR_BIND": ("asr.bind", str),
    "ASR_PORT": ("asr.port", int),
    "ASR_BACKEND": ("asr.backend", str),
    "PARAKEET_MODEL": ("asr.parakeet_model", str),
    "WHISPER_MODEL": ("asr.whisper_model", str),
    "STREAM_HEARTBEAT_SECONDS": ("asr.stream_heartbeat_seconds", float),
    # ASR diarization
    "DIARIZE": ("asr.diarization.enabled", "bool_str"),
    "OPENAI_BATCH_DIARIZE": ("asr.diarization.enabled", "bool_str"),
    "MAX_DIARIZE_SECONDS": ("asr.diarization.max_seconds", int),
    "MAX_SPEAKERS": ("asr.diarization.max_speakers", int),
    "NUM_SPEAKERS": ("asr.diarization.num_speakers", "int_or_none"),
    "CLUSTER_THRESHOLD": ("asr.diarization.cluster_threshold", "float_or_none"),
    # LLM
    "LLM_HOST": ("llm.host", str),
    "LLM_PORT": ("llm.port", int),
    "LLM_MODEL": ("llm.model", str),
    "LLM_MAX_TOKENS": ("llm.max_tokens", int),
    # Inspector
    "INSPECTOR_BIND": ("inspector.bind", str),
    "INSPECTOR_PORT": ("inspector.port", int),
    "INSPECTOR_AUTH_TOKEN": ("inspector.auth_token", "str_or_none"),
    # Char
    "CHAR_DATA_DIR": ("char.data_dir", "str_or_none"),
}


@dataclass
class Config:
    """Typed wrapper around the JSON-serialisable config dict.

    Kept thin on purpose -- the ground truth is the dict shape
    (``DEFAULT_CONFIG``). This class just gives callers attribute
    access for the most common fields. New fields can be read via
    ``cfg.raw["section"]["new_field"]`` without a code change here.
    """
    raw: dict[str, Any]

    # ---------- ASR ----------
    @property
    def asr_bind(self) -> str:
        return str(self.raw["asr"]["bind"])

    @property
    def asr_port(self) -> int:
        return int(self.raw["asr"]["port"])

    @property
    def asr_backend(self) -> str:
        return str(self.raw["asr"]["backend"]).lower()

    @property
    def parakeet_model(self) -> str:
        return str(self.raw["asr"]["parakeet_model"])

    @property
    def whisper_model(self) -> str:
        return str(self.raw["asr"]["whisper_model"])

    @property
    def stream_heartbeat_seconds(self) -> float:
        return float(self.raw["asr"]["stream_heartbeat_seconds"])

    @property
    def diarize_enabled(self) -> bool:
        return bool(self.raw["asr"]["diarization"]["enabled"])

    @property
    def max_diarize_seconds(self) -> int:
        return int(self.raw["asr"]["diarization"]["max_seconds"])

    @property
    def max_speakers(self) -> int:
        return int(self.raw["asr"]["diarization"]["max_speakers"])

    @property
    def num_speakers(self) -> Optional[int]:
        v = self.raw["asr"]["diarization"]["num_speakers"]
        return int(v) if v not in (None, "", 0) else None

    @property
    def cluster_threshold(self) -> Optional[float]:
        v = self.raw["asr"]["diarization"]["cluster_threshold"]
        return float(v) if v not in (None, "") else None

    # ---------- LLM ----------
    @property
    def llm_host(self) -> str:
        return str(self.raw["llm"]["host"])

    @property
    def llm_port(self) -> int:
        return int(self.raw["llm"]["port"])

    @property
    def llm_model(self) -> str:
        return str(self.raw["llm"]["model"])

    @property
    def llm_url(self) -> str:
        """Full chat-completions URL — the LM Studio API endpoint
        ``transcribe_file.py`` posts summaries to."""
        return f"http://{self.llm_host}:{self.llm_port}/v1/chat/completions"

    @property
    def llm_models_url(self) -> str:
        return f"http://{self.llm_host}:{self.llm_port}/api/v0/models"

    @property
    def llm_max_tokens(self) -> int:
        return int(self.raw["llm"]["max_tokens"])

    @property
    def llm_temperature(self) -> float:
        return float(self.raw["llm"]["temperature"])

    # ---------- Inspector ----------
    @property
    def inspector_bind(self) -> str:
        return str(self.raw["inspector"]["bind"])

    @property
    def inspector_port(self) -> int:
        return int(self.raw["inspector"]["port"])

    @property
    def inspector_auth_token(self) -> Optional[str]:
        v = self.raw["inspector"]["auth_token"]
        return str(v) if v else None

    # ---------- Char ----------
    @property
    def char_data_dir(self) -> Path:
        v = self.raw["char"]["data_dir"]
        if v:
            return Path(os.path.expanduser(str(v)))
        return Path.home() / "Library" / "Application Support" / "hyprnote"

    @property
    def char_settings_path(self) -> Path:
        return self.char_data_dir / "settings.json"

    @property
    def char_store_path(self) -> Path:
        return self.char_data_dir / "store.json"

    @property
    def char_sessions_dir(self) -> Path:
        return self.char_data_dir / "sessions"

    @property
    def expected_stt_provider(self) -> str:
        return str(self.raw["char"]["expected_stt_provider"])

    @property
    def expected_stt_model(self) -> str:
        return str(self.raw["char"]["expected_stt_model"])

    @property
    def expected_stt_base_url(self) -> str:
        """The base_url Char's OpenAI provider should be pointed at
        for transcription to land on our local ASR server."""
        return f"http://{self.asr_bind}:{self.asr_port}/v1"


def _coerce(value: str, kind: Any) -> Any:
    """Apply type coercion appropriate for env-var → typed-config."""
    if kind is str:
        return value
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind == "bool_str":
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    if kind == "int_or_none":
        v = value.strip()
        if not v or v == "0":
            return None
        return int(v)
    if kind == "float_or_none":
        v = value.strip()
        return float(v) if v else None
    if kind == "str_or_none":
        v = value.strip()
        return v or None
    raise ValueError(f"unknown coercion kind: {kind!r}")


def _set_dotted(d: dict[str, Any], dotted: str, value: Any) -> None:
    """Walk a dict by dotted key and set the leaf. Creates intermediate
    dicts as needed (defensive, in case the user trimmed sections from
    their config.json)."""
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive overlay: scalar values in `overlay` replace `base`;
    dicts are merged key-by-key. Lists replace whole."""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(d: dict[str, Any]) -> dict[str, Any]:
    """Layer 3: env vars win over file + defaults."""
    out = copy.deepcopy(d)
    for env_name, (dotted, kind) in _ENV_OVERRIDES.items():
        if env_name in os.environ:
            try:
                _set_dotted(out, dotted, _coerce(os.environ[env_name], kind))
            except (ValueError, TypeError):
                # Bad env-var value: ignore rather than crash startup.
                # Tests + the doctor command will surface this.
                continue
    return out


def load_config(path: Optional[Path] = None) -> Config:
    """Load + merge defaults → file → env. Missing file is fine."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    base = copy.deepcopy(DEFAULT_CONFIG)
    file_layer: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            file_layer = json.loads(cfg_path.read_text() or "{}") or {}
        except json.JSONDecodeError:
            file_layer = {}
    merged = _deep_merge(base, file_layer)
    merged = _apply_env_overrides(merged)
    merged["version"] = CONFIG_VERSION
    return Config(raw=merged)


def save_config(data: dict[str, Any], path: Optional[Path] = None,
                backup: bool = True) -> Path:
    """Write the user-editable layer to disk.

    Always backs up the previous file (if any) to a timestamped sibling
    so accidental edits via the inspector UI are recoverable.
    """
    cfg_path = path or DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if backup and cfg_path.is_file():
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = cfg_path.with_name(cfg_path.name + f".bak.{ts}")
        bak.write_bytes(cfg_path.read_bytes())
    payload = dict(data)
    payload["version"] = CONFIG_VERSION
    cfg_path.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        # Best-effort tighten perms (loopback only by default, but the
        # file may eventually contain an inspector auth token).
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    return cfg_path


def write_default_config_if_missing(path: Optional[Path] = None) -> Optional[Path]:
    """Used by ``./run.sh bootstrap`` to seed a fresh install."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if cfg_path.is_file():
        return None
    return save_config(copy.deepcopy(DEFAULT_CONFIG), cfg_path, backup=False)


def to_dict(cfg: Config) -> dict[str, Any]:
    """Round-trippable JSON-safe view of the merged config (i.e. what
    the inspector UI sees on GET /api/config)."""
    return copy.deepcopy(cfg.raw)


def validate(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors, empty if OK.

    Used by the inspector before persisting a PUT to refuse obviously
    broken values (e.g. negative port). Not exhaustive — the goal is to
    catch fat-finger mistakes, not enforce a schema.
    """
    errors: list[str] = []
    try:
        if not (1 <= int(data["asr"]["port"]) <= 65535):
            errors.append("asr.port must be 1..65535")
    except Exception:
        errors.append("asr.port must be an integer")
    try:
        if not (1 <= int(data["llm"]["port"]) <= 65535):
            errors.append("llm.port must be 1..65535")
    except Exception:
        errors.append("llm.port must be an integer")
    try:
        if not (1 <= int(data["inspector"]["port"]) <= 65535):
            errors.append("inspector.port must be 1..65535")
    except Exception:
        errors.append("inspector.port must be an integer")
    backend = str(data.get("asr", {}).get("backend", "")).lower()
    if backend not in ("parakeet", "whisper"):
        errors.append(f"asr.backend must be 'parakeet' or 'whisper', got {backend!r}")
    if int(data["asr"]["port"]) == int(data["inspector"]["port"]):
        errors.append("asr.port and inspector.port must differ")
    # Inspector auth is always required at runtime (per-service bearer
    # token derived from the Keychain master key — see service_auth.py).
    # The legacy ``inspector.auth_token`` config field is kept in
    # DEFAULT_CONFIG for back-compat with older config.json files but
    # is no longer consulted by the inspector. A non-loopback bind is
    # still worth flagging since the inspector wasn't designed to be
    # internet-exposed (no TLS, no rate-limiting).
    bind = data.get("inspector", {}).get("bind")
    if bind not in ("127.0.0.1", "localhost"):
        errors.append(
            f"inspector.bind is {bind!r} (non-loopback); auth is enforced "
            "via Keychain-derived tokens but the inspector still isn't "
            "designed for network exposure — rebind to 127.0.0.1 unless "
            "you really know what you're doing"
        )
    return errors
