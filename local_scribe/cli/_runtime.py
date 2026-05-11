"""Runtime constants shared by the CLI subcommand handlers.

Centralises the path + port + env-var defaults that ``run.sh`` used to
own, so a future containerised build can override them in one place
rather than chasing string-literal occurrences across multiple shell
scripts. Every value here is also read back by ``run.sh`` via
``python -m local_scribe …`` invocations, so the two stay in sync by
construction.
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_repo_root() -> Path:
    """Walk up from this module to find the repo root.

    Anchored on ``pyproject.toml`` (guaranteed at repo root post-refactor).
    Falls back to a fixed walk if no anchor is found.
    """
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return here.parents[2]


REPO_ROOT: Path = _find_repo_root()
VENV_PY: Path = REPO_ROOT / "venv" / "bin" / "python"
RUN_DIR: Path = REPO_ROOT / ".run"


def ensure_run_dir() -> Path:
    """Idempotently create the per-service runtime directory and
    return it. Mirrors ``mkdir -p "$RUN_DIR"`` in ``run.sh``."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


# Per-service port + bind defaults. Each follows the same precedence
# ``run.sh`` documents: env var wins, otherwise the ``config.json``
# value from ``local_scribe.common.config``, otherwise the hard-coded
# default. ``run.sh``'s shell-level env vars are inherited verbatim
# when the CLI is invoked from there.
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


def asr_port() -> int:
    return _env_int("ASR_PORT", 8000)


def inspector_port() -> int:
    return _env_int("INSPECTOR_PORT", 8001)


def inspector_bind() -> str:
    return _env_str("INSPECTOR_BIND", "127.0.0.1")


def egress_proxy_port() -> int:
    return _env_int("EGRESS_PROXY_PORT", 8889)


# Per-service PID + log file paths.
def asr_pid_file() -> Path:
    return ensure_run_dir() / "asr_server.pid"


def asr_log_file() -> Path:
    return ensure_run_dir() / "asr_server.log"


def inspector_pid_file() -> Path:
    return ensure_run_dir() / "inspector_server.pid"


def inspector_log_file() -> Path:
    return ensure_run_dir() / "inspector_server.log"


def egress_proxy_pid_file() -> Path:
    return ensure_run_dir() / "egress_proxy.pid"


def egress_proxy_log_file() -> Path:
    return ensure_run_dir() / "egress_proxy.log"


# Display helpers — ANSI colour codes that the CLI uses when stdout is
# a TTY. Match ``run.sh``'s palette so the two surfaces feel uniform.
def _is_tty() -> bool:
    import sys
    return sys.stdout.isatty()


def colors() -> dict[str, str]:
    if _is_tty():
        return {
            "green": "\033[32m",
            "red": "\033[31m",
            "yellow": "\033[33m",
            "bold": "\033[1m",
            "dim": "\033[2m",
            "reset": "\033[0m",
        }
    return {k: "" for k in ("green", "red", "yellow", "bold", "dim", "reset")}
