"""Layer C — per-launch session gate.

Goal
----
"Char can only access keys when invoked from ``./run.sh start``."

We can't actually intercept Char's launch (it's an .app the user
double-clicks via LaunchServices), so we enforce the rule at the
*token* level: Char's bearer is only valid while a specific
``./run.sh start`` invocation is alive. When that invocation exits,
the token stops working — even if Char is still running with the
old value cached in ``settings.json``.

Mechanism
---------
On ``./run.sh start``:

1.  Generate a 128-bit ``launch_id`` (UUID4) and a 64-bit
    short-display token. Write a JSON ``launch.lock`` at
    ``~/.config/local_scribe/launch.lock`` (chmod 0600) containing
    everything an inspecting service might want: the launch_id, the
    short id, the parent PID (``run.sh`` itself), wall-clock start
    time, and the in-flight ASR/Inspector PIDs.
2.  Set ``LOCAL_SCRIBE_LAUNCH_ID`` in the env of the spawned ASR /
    inspector services. They read it once at startup and cache it.
3.  Embed the launch_id in Char's ``api_key`` (via
    ``char_settings_writer``): the bearer becomes
    ``ls_asr_<32hex>.ls<16hex>``. The HKDF token still validates;
    the suffix tells the server which launch session the client
    thinks it belongs to.
4.  On ``./run.sh start`` exit (``trap``: EXIT, INT, TERM), atomically
    rewrite ``launch.lock`` with ``status="closed"`` then remove the
    file. Also rewrite Char's ``api_key`` to a clearly-expired marker
    so a future Char launch fails loud rather than silent.

On every request, the ASR / inspector services:

1.  Extract the bearer.
2.  If it carries a ``.ls<id>`` suffix, compare to the cached
    ``LOCAL_SCRIBE_LAUNCH_ID`` AND require ``launch.lock`` to still
    exist with a matching launch_id. Either check fail → 403 with
    a body explaining what to do.
3.  Bearers WITHOUT a suffix continue to work for the in-tree
    operator scripts (``transcribe_file.py`` / ``redo_session.py``)
    — those run under the user's shell, not under run.sh, but they
    derive the token from the master key at request time so they
    can't be replayed across reboots.

Refresh interval
----------------
launch.lock is re-read from disk every 1 second per service (cached
in module state). That's well below human-perceptible latency and
expensive only on a poisoned attacker that's trying to brute-force
the lock-replacement window.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_LOCK_PATH = (
    Path.home() / ".config" / "local_scribe" / "launch.lock"
)

LAUNCH_ID_ENV = "LOCAL_SCRIBE_LAUNCH_ID"
DISABLE_ENV = "LOCAL_SCRIBE_DISABLE_LAUNCH_GATE"

# We stat the lock file on EVERY call (cheap, ~50µs) so a closed /
# deleted lock is reflected immediately. The cache below only avoids
# *parsing* the JSON when the mtime hasn't changed.

# The bearer-suffix prefix that marks a launch-bound token.
# ``ls_asr_<32hex>.ls<16hex>``: 16 hex = first 8 bytes of the
# launch_id's hex form.
SUFFIX_DELIM = "."
SUFFIX_PREFIX = "ls"
SUFFIX_LEN = 16  # hex chars after the "ls" prefix


log = logging.getLogger(__name__)


# --- data carriers --------------------------------------------------


@dataclass
class LaunchSession:
    """The contents of ``launch.lock``. We serialise this with
    ``json.dumps`` — keep it primitive-only."""
    launch_id: str
    short_id: str
    started_at: float          # unix epoch seconds
    parent_pid: int             # run.sh's own PID
    asr_pid: Optional[int] = None
    inspector_pid: Optional[int] = None
    status: str = "active"      # active | closed
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LaunchSession":
        return cls(
            launch_id=d["launch_id"],
            short_id=d.get("short_id", d["launch_id"][:SUFFIX_LEN]),
            started_at=float(d.get("started_at", 0)),
            parent_pid=int(d.get("parent_pid", 0)),
            asr_pid=d.get("asr_pid"),
            inspector_pid=d.get("inspector_pid"),
            status=d.get("status", "active"),
            extra=d.get("extra", {}) or {},
        )


# --- writers (used by run.sh / cmd_start) ---------------------------


def new_session(parent_pid: int) -> LaunchSession:
    """Mint a fresh launch session record. Doesn't write to disk;
    the caller composes the file once it has the service PIDs."""
    uid = uuid.uuid4().hex
    return LaunchSession(
        launch_id=uid,
        short_id=uid[:SUFFIX_LEN],
        started_at=time.time(),
        parent_pid=parent_pid,
    )


def _resolve_path(path: Optional[Path]) -> Path:
    """Resolve ``path`` to ``DEFAULT_LOCK_PATH`` *at call time* so
    monkey-patching the module-level constant in tests is honoured.
    Function default arguments capture the constant at definition
    time, which is exactly what we want to avoid."""
    return path if path is not None else DEFAULT_LOCK_PATH


def write_lock(session: LaunchSession,
               path: Optional[Path] = None) -> None:
    """Atomically write the lock file at ``path`` with mode 0600."""
    path = _resolve_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(session.to_dict(), indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def close_lock(path: Optional[Path] = None) -> None:
    """Mark the session closed and remove the file. We write the
    closed marker first so an in-flight reader (the ASR server
    handling a request the moment run.sh exits) sees a clear ``status
    = closed`` rather than racing into a half-written file."""
    path = _resolve_path(path)
    if not path.is_file():
        return
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        doc = {}
    doc["status"] = "closed"
    doc["closed_at"] = time.time()
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --- readers (used by ASR / inspector) ------------------------------


_cache: dict[str, object] = {
    "session": None,
    "mtime": -1.0,
    "path": None,
}


def _maybe_reload(path: Path) -> Optional[LaunchSession]:
    """Return the session described by ``path`` if any. Stats the
    file on every call so a removed lock is reflected immediately;
    only re-parses the JSON when ``st_mtime`` has changed since the
    last read."""
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache["session"] = None
        _cache["mtime"] = -1.0
        _cache["path"] = str(path)
        return None
    if (
        _cache.get("path") != str(path)
        or st.st_mtime != _cache.get("mtime")
        or _cache.get("session") is None
    ):
        try:
            doc = json.loads(path.read_text())
            _cache["session"] = LaunchSession.from_dict(doc)
            _cache["mtime"] = st.st_mtime
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.warning("launch.lock: invalid contents at %s: %s",
                        path, exc)
            _cache["session"] = None
            _cache["mtime"] = -1.0
        _cache["path"] = str(path)
    return _cache["session"]  # type: ignore[return-value]


def _reset_cache() -> None:
    """Test hook — invalidate the in-process cache so a brand-new
    test gets a fresh read."""
    _cache["session"] = None
    _cache["mtime"] = -1.0
    _cache["path"] = None


def read_current(path: Optional[Path] = None) -> Optional[LaunchSession]:
    """Public surface for `read the current launch session`."""
    return _maybe_reload(_resolve_path(path))


def is_gate_disabled() -> bool:
    """Honoured by the services so tests / bypass-mode skip the
    gate. Matches the convention used by
    ``service_auth.is_bypass_enabled``."""
    return os.environ.get(DISABLE_ENV, "") not in ("", "0", "false", "FALSE")


def expected_launch_id() -> Optional[str]:
    """The launch ID the service was told to expect via env at
    startup. ``None`` means the service was started outside
    ``./run.sh start`` (e.g. via ``uvicorn`` directly in a test) —
    we still load from disk in that case."""
    v = os.environ.get(LAUNCH_ID_ENV) or None
    return v


# --- token-suffix helpers -------------------------------------------


def attach_suffix(bearer: str, launch_id: str) -> str:
    """Append ``.ls<16hex>`` to a base bearer. Idempotent: a bearer
    that already carries a suffix is rewritten."""
    base = strip_suffix(bearer)
    suffix = SUFFIX_PREFIX + launch_id[:SUFFIX_LEN]
    return f"{base}{SUFFIX_DELIM}{suffix}"


def strip_suffix(bearer: str) -> str:
    """Return ``bearer`` with any trailing ``.ls<...>`` removed."""
    if SUFFIX_DELIM not in bearer:
        return bearer
    base, sep, rest = bearer.rpartition(SUFFIX_DELIM)
    if rest.startswith(SUFFIX_PREFIX) and len(rest) <= SUFFIX_LEN + len(SUFFIX_PREFIX):
        return base
    return bearer


def extract_suffix(bearer: str) -> Optional[str]:
    """Return the 16-hex ``short_id`` from a launch-bound bearer, or
    ``None`` if the bearer doesn't carry one."""
    if SUFFIX_DELIM not in bearer:
        return None
    _, _, rest = bearer.rpartition(SUFFIX_DELIM)
    if not rest.startswith(SUFFIX_PREFIX):
        return None
    short = rest[len(SUFFIX_PREFIX):]
    if len(short) == SUFFIX_LEN and all(c in "0123456789abcdef" for c in short):
        return short
    return None


# --- main gate ------------------------------------------------------


@dataclass
class GateOutcome:
    """The decision a service's auth dependency turns into 200/403."""
    allowed: bool
    reason: str            # for log lines / 403 body
    bearer_was_bound: bool # bearer carried .ls suffix
    session: Optional[LaunchSession] = None


def check_bearer(bearer: str,
                 path: Optional[Path] = None,
                 expected_id: Optional[str] = None) -> GateOutcome:
    """Decide whether ``bearer`` should be honoured under the launch
    gate. Returns a :class:`GateOutcome` regardless of pass/fail so
    the caller can log the reason it rejected.

    Policy:

    *  ``LOCAL_SCRIBE_DISABLE_LAUNCH_GATE=1`` → always allow (tests).
    *  Bearer with no ``.ls`` suffix is a "non-Char" bearer
       (scripts) → always allow. The HKDF check is still doing its
       job; we just don't tie scripts to the launch session.
    *  Bearer with a ``.ls`` suffix → require BOTH:
        - ``launch.lock`` exists with ``status == "active"``
        - the suffix matches its ``short_id``
        - if ``expected_id`` is supplied (from env at server boot),
          the lock's ``launch_id`` must also match.
    """
    if is_gate_disabled():
        return GateOutcome(
            allowed=True, reason="launch gate disabled by env",
            bearer_was_bound=False,
        )
    suffix = extract_suffix(bearer)
    if suffix is None:
        return GateOutcome(
            allowed=True,
            reason="unbound bearer (script invocation)",
            bearer_was_bound=False,
        )
    session = _maybe_reload(_resolve_path(path))
    if session is None:
        return GateOutcome(
            allowed=False,
            reason=(
                "launch.lock not present — start the services via "
                "./run.sh start"
            ),
            bearer_was_bound=True,
        )
    if session.status != "active":
        return GateOutcome(
            allowed=False,
            reason=f"launch.lock status={session.status!r}; restart via ./run.sh start",
            bearer_was_bound=True, session=session,
        )
    if session.short_id != suffix:
        return GateOutcome(
            allowed=False,
            reason=(
                f"bearer is bound to a stale launch (suffix={suffix} "
                f"vs current={session.short_id}). Run "
                f"`./run.sh configure-char` to refresh Char's saved key."
            ),
            bearer_was_bound=True, session=session,
        )
    if expected_id is not None and session.launch_id != expected_id:
        return GateOutcome(
            allowed=False,
            reason=(
                "launch.lock launch_id does not match the launch this "
                "service was started against; the lock was rotated "
                "without restarting the service."
            ),
            bearer_was_bound=True, session=session,
        )
    return GateOutcome(
        allowed=True, reason="ok", bearer_was_bound=True, session=session,
    )


def status() -> dict:
    """Operator-facing snapshot for ``./run.sh status`` /
    ``./run.sh doctor``."""
    s = read_current()
    return {
        "lock_present": s is not None,
        "lock_path": str(_resolve_path(None)),
        "session": s.to_dict() if s else None,
        "env_launch_id": expected_launch_id(),
        "gate_disabled": is_gate_disabled(),
    }


# --- CLI ------------------------------------------------------------


def _main(argv: list[str]) -> int:
    """``python -m launch_session [mint | close | status | check-bearer TOKEN]``"""
    mode = "status" if not argv else argv[0]
    if mode == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if mode == "mint":
        parent_pid = int(os.environ.get("PPID") or os.getpid())
        s = new_session(parent_pid)
        write_lock(s)
        print(s.launch_id)
        return 0
    if mode == "close":
        close_lock()
        return 0
    if mode == "check-bearer":
        if len(argv) < 2:
            print("usage: check-bearer <token>", file=__import__("sys").stderr)
            return 2
        outcome = check_bearer(argv[1])
        print(json.dumps({
            "allowed": outcome.allowed,
            "reason": outcome.reason,
            "bearer_was_bound": outcome.bearer_was_bound,
        }, indent=2))
        return 0 if outcome.allowed else 1
    print(f"unknown subcommand: {mode}", file=__import__("sys").stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
