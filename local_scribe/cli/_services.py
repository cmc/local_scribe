"""Service lifecycle primitives — start, stop, status, restart.

Direct Python port of the ``asr_start``/``asr_stop``/``asr_pid``/
``inspector_*``/``egress_proxy_*`` helpers in ``run.sh``. The CLI
subcommand handlers in ``__main__.py`` thin-wrap these.

The shell semantics we preserve byte-for-byte:
  * PID files at ``$RUN_DIR/<svc>.pid``; the file existing implies a
    *recorded* PID, not a running process — ``status()`` re-checks with
    ``kill -0`` before reporting alive.
  * Logs are *appended*, never truncated, with a banner line that
    delimits each run. Operators rely on this to grep across restarts.
  * ``start()`` blocks until a readiness probe (HTTP or TCP) succeeds,
    or returns ``False`` after a per-service timeout.
  * ``stop()`` sends SIGTERM, polls for exit, escalates to SIGKILL if
    the process is still alive after the grace window.

Differences from the shell version:
  * We launch the child as a detached process group via ``preexec_fn=
    os.setsid`` rather than ``nohup &``, so signal delivery is cleaner.
  * We use ``http.client`` for readiness probes (no ``curl`` dep).
  * We avoid ``ps``/``kill -0`` shell calls; ``os.kill(pid, 0)`` is
    the direct equivalent.
"""

from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import _runtime as rt


@dataclass(frozen=True)
class ServiceSpec:
    """A single managed service. Created by the
    ``{asr,inspector,egress_proxy}_spec()`` factory functions below."""
    name: str
    pid_file: Path
    log_file: Path
    argv: tuple[str, ...]
    readiness: Callable[[], bool]
    readiness_timeout_s: float
    stop_grace_s: float
    # Human-facing display sub-line (printed by ``status()`` when running).
    display_url: Optional[str] = None


def _read_pid(pid_file: Path) -> Optional[int]:
    """Return the integer PID stored at ``pid_file``, or ``None`` if
    the file is missing / malformed / the recorded PID isn't alive."""
    try:
        raw = pid_file.read_text().strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _http_ready(host: str, port: int, path: str, timeout_s: float = 0.5) -> bool:
    """Single ``GET path`` probe returning ``True`` on any 2xx."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.request("GET", path)
        resp = conn.getresponse()
        ok = 200 <= resp.status < 300
        conn.close()
        return ok
    except (OSError, http.client.HTTPException):
        return False


def _tcp_ready(host: str, port: int, timeout_s: float = 0.2) -> bool:
    """Single TCP-connect probe — used by services without an HTTP
    health endpoint (e.g. the egress proxy, which is a raw HTTP
    CONNECT tunnel)."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def asr_spec() -> ServiceSpec:
    port = rt.asr_port()
    return ServiceSpec(
        name="asr",
        pid_file=rt.asr_pid_file(),
        log_file=rt.asr_log_file(),
        argv=(
            str(rt.VENV_PY), "-u", "-m", "uvicorn",
            "local_scribe.asr.asr_server:app",
            "--host", "0.0.0.0", "--port", str(port),
        ),
        readiness=lambda: _http_ready("127.0.0.1", port, "/health"),
        readiness_timeout_s=30.0,
        stop_grace_s=7.5,
        display_url=f"http://127.0.0.1:{port}",
    )


def inspector_spec() -> ServiceSpec:
    port = rt.inspector_port()
    bind = rt.inspector_bind()
    return ServiceSpec(
        name="inspector",
        pid_file=rt.inspector_pid_file(),
        log_file=rt.inspector_log_file(),
        argv=(
            str(rt.VENV_PY), "-u", "-m", "uvicorn",
            "local_scribe.inspector.inspector_server:app",
            "--host", bind, "--port", str(port),
        ),
        readiness=lambda: _http_ready("127.0.0.1", port, "/api/health"),
        readiness_timeout_s=15.0,
        stop_grace_s=5.0,
        display_url=f"http://{bind}:{port}",
    )


def egress_proxy_spec() -> ServiceSpec:
    port = rt.egress_proxy_port()
    return ServiceSpec(
        name="egress-proxy",
        pid_file=rt.egress_proxy_pid_file(),
        log_file=rt.egress_proxy_log_file(),
        argv=(
            str(rt.VENV_PY), "-u", "-m", "local_scribe.egress.egress_proxy",
            "start", "--port", str(port),
        ),
        readiness=lambda: _tcp_ready("127.0.0.1", port),
        readiness_timeout_s=8.0,
        stop_grace_s=5.0,
        display_url=f"http://127.0.0.1:{port}",
    )


# Lookup helper so ``__main__`` can map argparse subcommand name → spec.
def spec_by_name(name: str) -> ServiceSpec:
    table = {
        "asr": asr_spec,
        "inspector": inspector_spec,
        "egress-proxy": egress_proxy_spec,
    }
    if name not in table:
        raise KeyError(f"unknown service: {name!r}")
    return table[name]()


# --- start / stop / status / restart ---------------------------------------


def status(spec: ServiceSpec) -> tuple[bool, Optional[int]]:
    """Return ``(running, pid)``. ``pid`` is ``None`` when not running."""
    pid = _read_pid(spec.pid_file)
    return (pid is not None, pid)


def start(spec: ServiceSpec, *, stream=sys.stderr) -> bool:
    """Start the service in a detached process group, wait for the
    readiness probe to succeed, return ``True`` on success.

    Idempotent: if the recorded PID is already alive, returns ``True``
    immediately without re-launching.
    """
    c = rt.colors()

    running, pid = status(spec)
    if running:
        _say(f"{spec.name} already running (pid {pid})", stream=stream)
        return True

    if not rt.VENV_PY.exists():
        _say(f"{c['red']}venv python missing at {rt.VENV_PY}{c['reset']}", stream=stream)
        return False

    _say(f"starting {spec.name} ...", stream=stream)

    # Append a run-boundary banner so log greps don't blur across runs.
    rt.ensure_run_dir()
    with spec.log_file.open("a") as lf:
        lf.write(f"\n========== started {time.strftime('%Y-%m-%d %H:%M:%S')} ==========\n")

    # Detach the child into its own process group so a future
    # ``stop()`` SIGTERM cleans up the whole subtree (uvicorn spawns
    # workers; egress_proxy spawns the proxy task).
    log_fd = open(spec.log_file, "a")
    try:
        proc = subprocess.Popen(
            spec.argv,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(rt.REPO_ROOT),
            start_new_session=True,
        )
    finally:
        log_fd.close()

    spec.pid_file.write_text(f"{proc.pid}\n")

    # Readiness loop — sleep in small slices so we don't overshoot
    # the budget.
    deadline = time.monotonic() + spec.readiness_timeout_s
    while time.monotonic() < deadline:
        # Bail early if the process died.
        if proc.poll() is not None:
            spec.pid_file.unlink(missing_ok=True)
            _say(
                f"{c['red']}{spec.name} exited during startup; see {spec.log_file}{c['reset']}",
                stream=stream,
            )
            return False
        if spec.readiness():
            url = spec.display_url or ""
            _say(
                f"{c['green']}{spec.name} up{(' on ' + url) if url else ''} "
                f"(pid {proc.pid}){c['reset']}",
                stream=stream,
            )
            return True
        time.sleep(0.5)

    _say(
        f"{c['red']}{spec.name} didn't respond after {spec.readiness_timeout_s:.0f}s; "
        f"see {spec.log_file}{c['reset']}",
        stream=stream,
    )
    return False


def stop(spec: ServiceSpec, *, stream=sys.stderr) -> bool:
    """Stop the service. SIGTERM → grace window → SIGKILL fallback."""
    c = rt.colors()
    running, pid = status(spec)
    if not running:
        _say(f"{spec.name} is not running", stream=stream)
        spec.pid_file.unlink(missing_ok=True)
        return True

    _say(f"stopping {spec.name} (pid {pid}) ...", stream=stream)

    # Target the process group so detached children die together.
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + spec.stop_grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.25)
    else:
        _say(f"{c['yellow']}forcing SIGKILL{c['reset']}", stream=stream)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    spec.pid_file.unlink(missing_ok=True)
    _say(f"{c['green']}{spec.name} stopped{c['reset']}", stream=stream)
    return True


def restart(spec: ServiceSpec, *, stream=sys.stderr) -> bool:
    stop(spec, stream=stream)
    return start(spec, stream=stream)


def tail_log(spec: ServiceSpec) -> int:
    """``tail -F`` the service log. Exec()'s into the real tail
    binary so Ctrl-C is handed straight to it."""
    if not spec.log_file.exists():
        sys.stderr.write(f"no log at {spec.log_file}\n")
        return 1
    tail = shutil.which("tail")
    if tail is None:
        sys.stderr.write("tail(1) not found on PATH\n")
        return 1
    os.execv(tail, [tail, "-F", str(spec.log_file)])
    return 0  # unreachable


# --- internals --------------------------------------------------------------


def _say(msg: str, *, stream=sys.stderr) -> None:
    """Match ``run.sh``'s ``say`` format: ``[HH:MM:SS] <msg>``."""
    c = rt.colors()
    ts = time.strftime("%H:%M:%S")
    stream.write(f"{c['dim']}[{ts}]{c['reset']} {msg}\n")
    stream.flush()
