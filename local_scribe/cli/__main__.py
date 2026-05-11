"""``python -m local_scribe`` — top-level CLI dispatcher.

Container-ready entry point for the whole project. ``run.sh`` keeps the
shell-only responsibilities (SIP preflight, ``osascript`` privileged
prompts, ``sandbox-exec`` for ``char launch``, the interactive
``bootstrap`` flow). Everything else routes through this module so
future deployments can invoke a single Python entry point and skip
the shell layer entirely.

Subcommand surface (mirrors the ``run.sh`` verbs the operator already
knows; arguments after the verb are passed through unmodified):

    Service lifecycle:
      start                  bring up asr + inspector + egress-proxy
      stop                   tear them all down
      restart                stop then start
      status                 one-line status per service
      asr         {start,stop,restart,status,logs}
      inspector   {start,stop,restart,status,logs,open}
      egress-proxy{start,stop,restart,status,logs,verify}

    Operator one-shots (delegate to existing module ``main()``):
      vault       {init,unlock,lock,status,...}
      key         {init,rotate,destroy,unlock,status,...}
      yubikey     {enroll,verify,restore,...}
      firewall    {enable,disable,status,mode,...}
      char        {audit,configure,firewall-status,...}
      transcribe  FILE

    Signed pinned config (Char binary identity, DMG hashes, LM Studio
    version pin — see ``local_scribe/common/pinned.json``):
      config      {show,sign,verify,status}

    Diagnostics:
      doctor                  comprehensive health check
      version                 print package version + git short-ref

Shell-only paths (NOT extracted; ``run.sh`` still owns them):
  * ``./run.sh bootstrap``     — interactive setup, osascript prompts,
                                 sudo, swiftc compile
  * ``./run.sh char launch``   — sandbox-exec wrapper
  * ``./run.sh configure-char``— Touch ID prompt + Char config patch
"""

from __future__ import annotations

import argparse
import importlib
import runpy
import sys
from typing import Optional

from . import _services


# --- subcommand handlers ---------------------------------------------------


def _cmd_service(svc_name: str, verb: str) -> int:
    """Generic dispatch for asr / inspector / egress-proxy verbs."""
    spec = _services.spec_by_name(svc_name)
    if verb == "start":
        return 0 if _services.start(spec) else 1
    if verb == "stop":
        return 0 if _services.stop(spec) else 1
    if verb == "restart":
        return 0 if _services.restart(spec) else 1
    if verb == "status":
        running, pid = _services.status(spec)
        if running:
            url = f"  url={spec.display_url}" if spec.display_url else ""
            print(f"  ● {spec.name:13s}  pid={pid}{url}")
            return 0
        print(f"  ○ {spec.name:13s}  not running")
        return 3
    if verb in ("log", "logs"):
        return _services.tail_log(spec)
    if verb == "open":
        if svc_name != "inspector":
            print(f"'{svc_name} open' not supported", file=sys.stderr)
            return 2
        running, _ = _services.status(spec)
        if not running:
            ok = _services.start(spec)
            if not ok:
                return 1
        import webbrowser
        if spec.display_url:
            webbrowser.open(spec.display_url)
        return 0
    if verb == "verify":
        if svc_name != "egress-proxy":
            print(f"'{svc_name} verify' not supported", file=sys.stderr)
            return 2
        # Delegate to the existing CLI in local_scribe.egress.egress_proxy
        return _delegate_module(["local_scribe.egress.egress_proxy", "verify"])
    print(f"unknown verb: {verb}", file=sys.stderr)
    return 2


def _cmd_start_all() -> int:
    """Start asr + inspector + egress-proxy in dependency order
    (egress-proxy first since the Char sandbox profile depends on it)."""
    rc = 0
    for s in (_services.egress_proxy_spec(),
              _services.asr_spec(),
              _services.inspector_spec()):
        if not _services.start(s):
            rc = 1
    return rc


def _cmd_stop_all() -> int:
    """Stop in reverse order; best-effort even if one fails."""
    for s in (_services.inspector_spec(),
              _services.asr_spec(),
              _services.egress_proxy_spec()):
        _services.stop(s)
    return 0


def _cmd_restart_all() -> int:
    _cmd_stop_all()
    return _cmd_start_all()


def _cmd_status_all() -> int:
    # Dev-mode marker rendered above the service table. Reflects
    # the *current shell* — if a service was started in another
    # shell with --dev, the inspector's /api/dev_mode/status
    # endpoint is the authoritative source. We surface the local
    # signal because the most common operator use of this command
    # is to verify "is the run I'm about to do going to bypass
    # SIP?" and that's a question about THIS shell's env.
    try:
        from local_scribe.common import dev_mode as _dev
        if _dev.is_enabled():
            print("  [DEV MODE] LOCAL_SCRIBE_DEV_MODE is set in this shell.")
            print("             SIP gates will be bypassed for any service")
            print("             you start from here. See SECURITY.md § 'Dev mode'.")
    except Exception:  # noqa: BLE001 — never let banner errors break status
        pass

    rc = 0
    for s in (_services.asr_spec(),
              _services.inspector_spec(),
              _services.egress_proxy_spec()):
        running, pid = _services.status(s)
        if running:
            url = f"  url={s.display_url}" if s.display_url else ""
            print(f"  ● {s.name:13s}  pid={pid}{url}")
        else:
            print(f"  ○ {s.name:13s}  not running")
            rc = 3 if rc == 0 else rc
    return rc


def _cmd_version() -> int:
    """Print package version + (if available) git short-ref."""
    v = _read_pyproject_version()
    short = _git_short_ref()
    extra = f" ({short})" if short else ""
    print(f"local_scribe {v}{extra}")
    return 0


def _read_pyproject_version() -> str:
    """Read ``project.version`` from ``pyproject.toml``.

    Prefers ``importlib.metadata`` (works post ``pip install -e .``)
    and falls back to reading ``pyproject.toml`` directly when the
    package isn't installed — which is the supported development path
    per the ``flat-layout`` decision documented in ``pyproject.toml``.
    """
    try:
        from importlib.metadata import version as _pv
        return _pv("local_scribe")
    except Exception:
        pass
    try:
        import tomllib
        from . import _runtime as rt
        with (rt.REPO_ROOT / "pyproject.toml").open("rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "unknown"


def _git_short_ref() -> Optional[str]:
    """Best-effort short-ref, ``None`` on failure (e.g. detached install)."""
    import subprocess
    from . import _runtime as rt
    try:
        out = subprocess.run(
            ["git", "-C", str(rt.REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2.0,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _delegate_module(argv: list[str]) -> int:
    """Run ``python -m argv[0] argv[1:]`` in-process via ``runpy``.

    The delegated module's ``__name__`` becomes ``"__main__"`` so its
    ``if __name__ == "__main__":`` block fires exactly as it would
    under ``python -m``. ``sys.argv`` is temporarily replaced so
    ``argparse`` parsers inside the delegated module see only the
    arguments after the module name.
    """
    mod_name, *rest = argv
    saved_argv = sys.argv
    sys.argv = [f"python -m {mod_name}", *rest]
    try:
        runpy.run_module(mod_name, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0
    except Exception as e:
        print(f"{mod_name}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        sys.argv = saved_argv
    return 0


def _signed_files() -> list[tuple[str, "Path", "Path"]]:
    """Roster of files governed by the signed-config gate.

    Each entry is ``(label, file_path, sig_path)``:

    * ``pinned`` — ``local_scribe/common/pinned.json``: the in-tree
      distribution constants (Char DMG hashes, Team ID, LM Studio
      version pin). Always present after refactor; required for the
      gate to pass.
    * ``char_baseline`` — ``~/.config/local_scribe/char_baseline.json``:
      operator-set CDHash baseline. Optional (set by
      ``./run.sh char baseline-set`` after a clean Char install). If
      the file is absent, we silently skip it from sign/verify; if
      present, it MUST carry a valid signature.
    """
    from pathlib import Path  # local import to keep top of file lean
    from local_scribe.common import pinned as _p
    out: list[tuple[str, Path, Path]] = [
        ("pinned", _p.pinned_path(), _p.pinned_sig_path()),
    ]
    baseline = Path.home() / ".config" / "local_scribe" / "char_baseline.json"
    if baseline.is_file():
        out.append(("char_baseline", baseline,
                    baseline.with_name(baseline.name + ".sig")))
    return out


def _cmd_config(args: argparse.Namespace) -> int:
    """Handle the ``config <verb>`` subcommand surface.

    Verbs:
      * ``show``   — print pinned config; ``--shell`` re-emits as
                     bash-sourceable ``KEY=value`` for run.sh.
                     ``--json`` is the default and prints raw JSON.
      * ``status`` — print signature state for every file in
                     :func:`_signed_files` without unlocking the
                     master key (safe to call from doctor / status
                     bars / non-interactive shells).
      * ``verify`` — full HMAC verify of every file in
                     :func:`_signed_files` (requires Touch ID +
                     YubiKey unlock); exit 0 on success, non-zero per
                     failure class. Used by the run.sh start-time gate.
      * ``sign``   — unlock master key, write a fresh sidecar for
                     every file in :func:`_signed_files`. This is the
                     "I authorise these files" gesture; one Touch ID
                     + YubiKey tap covers both files.
    """
    from local_scribe.common import pinned as _p
    from local_scribe.security import signed_config as _sc

    verb = args.verb

    if verb == "show":
        # Unverified read: ``show`` is informational, the verify path
        # is its own subcommand. Strict consumers must follow up with
        # ``config verify``.
        #
        # Silence the "loading without signature" WARNING here — the
        # operator (or run.sh) is making the conscious choice and
        # surfacing it on every invocation would just train them to
        # ignore it. The warning is still emitted from any unintended
        # library call path.
        import logging
        _p.logger.setLevel(logging.ERROR)
        try:
            data = _p.load_pinned_unverified()
        except FileNotFoundError as e:
            print(f"config: {e}", file=sys.stderr)
            return 1
        if args.shell:
            # Bash-sourceable. Keys mirror the variable names the
            # pre-refactor run.sh used so existing scripts keep working
            # after ``eval "$(python -m local_scribe config show --shell)"``.
            print(f'CHAR_KNOWN_GOOD_VERSION="{data.char.known_good_version}"')
            print(f'CHAR_RELEASE_TAG="{data.char.release_tag}"')
            print(f'CHAR_RELEASE_BASE_URL="{data.char.release_base_url}"')
            print(f'CHAR_DMG_SHA256_AARCH64="{data.char.dmg_sha256_aarch64}"')
            print(f'CHAR_DMG_SHA256_X86_64="{data.char.dmg_sha256_x86_64}"')
            print(f'CHAR_PINNED_TEAM_ID="{data.char.pinned_team_id}"')
            print(f'CHAR_PINNED_BUNDLE_ID="{data.char.pinned_bundle_id}"')
            print(f'CHAR_DEFAULT_APP_PATH="{data.char.default_app_path}"')
            print(f'LMSTUDIO_KNOWN_GOOD_VERSION="{data.lmstudio.known_good_version}"')
            print(f'LMSTUDIO_APP_PATH="{data.lmstudio.app_path}"')
            print(f'LMSTUDIO_PORT="{data.lmstudio.default_port}"')
        else:
            import json
            print(json.dumps(data.raw, indent=2))
        return 0

    if verb == "status":
        rc = 0
        for label, fp, sp in _signed_files():
            st = _sc.status(fp, sig_path=sp)
            print(f"[{label}]")
            print(f"  file        : {st.protected_path}")
            print(f"  sidecar     : {st.sig_path}")
            print(f"  file ok     : {st.protected_present}")
            print(f"  sig ok      : {st.sig_present and st.sig_parseable}")
            if st.sig_fp:
                print(f"  key fp      : {st.sig_fp}")
            if st.sig_alg:
                print(f"  alg         : {st.sig_alg}")
            if st.note:
                print(f"  note        : {st.note}")
            # Worst exit code wins.
            if not st.protected_present:
                rc = max(rc, 1)
            elif not st.sig_present or not st.sig_parseable:
                rc = max(rc, 3)
        return rc

    if verb == "verify":
        try:
            from local_scribe.security import key_lifecycle as _kl
            mk = _kl.unlock_master_key(
                prompt="Verify local_scribe pinned config signatures",
            )
        except Exception as e:
            print(f"config verify: cannot unlock master key: {e}",
                  file=sys.stderr)
            return 1
        try:
            worst = 0
            for label, fp, sp in _signed_files():
                try:
                    sig = _sc.verify_file(fp, mk.as_bytes(), sig_path=sp)
                    print(f"OK   [{label}] verifies under key fp={sig.fp_hex}")
                except _sc.SignatureMissingError as e:
                    print(f"FAIL [{label}] no signature; run `./run.sh config sign`",
                          file=sys.stderr)
                    print(f"     ({e})", file=sys.stderr)
                    worst = max(worst, 10)
                except _sc.SignatureMismatchError as e:
                    print(f"FAIL [{label}] file changed since signing.",
                          file=sys.stderr)
                    print("     Audit the diff:", file=sys.stderr)
                    print(f"       git diff -- {fp}", file=sys.stderr)
                    print("     Then re-bless: ./run.sh config sign",
                          file=sys.stderr)
                    print(f"     Or revert: git checkout -- {fp}",
                          file=sys.stderr)
                    print(f"     ({e})", file=sys.stderr)
                    worst = max(worst, 11)
                except _sc.KeyFingerprintMismatchError as e:
                    print(f"FAIL [{label}] master key was rotated; signature is stale.",
                          file=sys.stderr)
                    print("     Re-bless with: ./run.sh config sign",
                          file=sys.stderr)
                    print(f"     ({e})", file=sys.stderr)
                    worst = max(worst, 12)
                except _sc.SignedConfigError as e:
                    print(f"FAIL [{label}] {e}", file=sys.stderr)
                    worst = max(worst, 13)
            return worst
        finally:
            mk.forget()

    if verb == "sign":
        try:
            from local_scribe.security import key_lifecycle as _kl
            mk = _kl.unlock_master_key(
                prompt="Sign local_scribe pinned config",
            )
        except Exception as e:
            print(f"config sign: cannot unlock master key: {e}",
                  file=sys.stderr)
            return 1
        try:
            key_fp = _sc.fingerprint(mk.as_bytes())
            for label, fp, sp in _signed_files():
                out = _sc.sign_file(fp, mk.as_bytes(), sig_path=sp)
                print(f"OK  [{label}] signed under key fp={key_fp}")
                print(f"    file    : {fp}")
                print(f"    sidecar : {out}")
            return 0
        finally:
            mk.forget()

    print(f"unknown config verb: {verb}", file=sys.stderr)
    return 2


def _cmd_doctor() -> int:
    """Comprehensive health check — mirrors ``run.sh doctor`` but
    delegates work to the relevant Python modules and ``status`` of
    each managed service."""
    print("local_scribe doctor")
    print("===================")
    print()
    # Dev-mode banner above everything else so it can't be missed
    # while scrolling. The Python-side rich banner emission is
    # idempotent (one-shot per process); doctor is one process, so
    # this fires exactly once.
    try:
        from local_scribe.common import dev_mode as _dev
        if _dev.is_enabled():
            _dev.emit_banner_once(sys.stderr)
            print(
                "[DEV MODE ACTIVE] SIP gates are bypassed for any service "
                "started from this shell.\n"
                "                  See SECURITY.md § 'Dev mode'."
            )
            print()
    except Exception:  # noqa: BLE001
        pass
    # System Integrity Protection — security-critical preflight.
    print("System Integrity Protection:")
    rc_sip = _delegate_module(["local_scribe.security.sip_check", "check"])
    print()
    # Script integrity — has any tracked file drifted from HEAD?
    print("Script integrity:")
    _delegate_module(["local_scribe.security.script_integrity", "--banner"])
    print()
    # Service status.
    print("Services:")
    _cmd_status_all()
    print()
    # Char audit.
    print("Char audit:")
    _delegate_module(["local_scribe.char.char_audit", "--summary"])
    print()
    return rc_sip


# --- argparse plumbing -----------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m local_scribe",
        description="local_scribe CLI — service lifecycle + operator commands",
    )
    sp = p.add_subparsers(dest="command", metavar="<command>")
    sp.required = True

    # Top-level lifecycle verbs.
    for name in ("start", "stop", "restart", "status", "doctor", "version"):
        sp.add_parser(name, help=f"{name} (top-level)")

    # Signed pinned config.
    config_sp = sp.add_parser(
        "config",
        help="signed pinned config (show/sign/verify/status)",
    )
    config_sp.add_argument(
        "verb",
        choices=["show", "sign", "verify", "status"],
    )
    config_sp.add_argument(
        "--shell", action="store_true",
        help="(show only) emit bash-sourceable KEY=value lines for run.sh",
    )
    config_sp.add_argument(
        "--json", action="store_true",
        help="(show only, default) emit raw JSON",
    )

    # Per-service.
    for svc in ("asr", "inspector", "egress-proxy"):
        sub = sp.add_parser(svc, help=f"{svc} service management")
        verbs = ["start", "stop", "restart", "status", "logs"]
        if svc == "inspector":
            verbs.append("open")
        if svc == "egress-proxy":
            verbs.append("verify")
        sub.add_argument("verb", choices=verbs)

    # Operator one-shots — delegate to existing CLIs. Each accepts
    # ``REMAINDER`` so we don't have to mirror their parsers here.
    for name, target in (
        ("vault",      "local_scribe.security.vault_unlock"),
        ("key",        "local_scribe.security.key_lifecycle"),
        ("yubikey",    "local_scribe.security.yubikey_backup"),
        ("firewall",   "local_scribe.egress.firewall"),
        ("char-audit", "local_scribe.char.char_audit"),
        ("transcribe", "local_scribe.asr.transcribe_file"),
        ("redo-session", "local_scribe.asr.redo_session"),
    ):
        sub = sp.add_parser(name, help=f"{name} — delegates to {target}")
        sub.add_argument("args", nargs=argparse.REMAINDER)
        sub.set_defaults(_delegate_target=target)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    # Top-level lifecycle.
    if cmd == "start":   return _cmd_start_all()
    if cmd == "stop":    return _cmd_stop_all()
    if cmd == "restart": return _cmd_restart_all()
    if cmd == "status":  return _cmd_status_all()
    if cmd == "doctor":  return _cmd_doctor()
    if cmd == "version": return _cmd_version()

    # Signed pinned config.
    if cmd == "config":
        return _cmd_config(args)

    # Per-service dispatch.
    if cmd in ("asr", "inspector", "egress-proxy"):
        return _cmd_service(cmd, args.verb)

    # Delegated one-shots.
    target = getattr(args, "_delegate_target", None)
    if target is not None:
        rest = args.args or []
        return _delegate_module([target, *rest])

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
