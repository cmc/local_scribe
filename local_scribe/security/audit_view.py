"""Aggregated security posture for the inspector UI's audit view.

Every defense layer in :doc:`SECURITY.md` already exposes a cheap
``status()`` / ``verify()`` function -- :mod:`sip_check`,
:mod:`firewall`, :mod:`signed_config`, :mod:`script_integrity`,
:mod:`char_integrity`, :mod:`vault`, :mod:`secret_store`,
:mod:`yubikey_backup`, :mod:`disaster_recovery`. This module is the
single composition point that calls all of them, applies a uniform
``OK / WARN / FAIL / INFO`` grading, and returns a JSON-serialisable
dict the inspector renders into the "Char audit → Security
verification" view.

Design notes
------------

* **Cheap-only.** Every call here must be safe to invoke on every
  inspector page load. No Touch ID prompts, no YubiKey taps, no
  hdiutil shell-outs. We only read whatever's already on disk or
  cached in process state. That means a few invariants we'd love to
  pin (like "the actual master key reconstitutes correctly") are
  *not* in this view -- they live in the dedicated unlock paths and
  are surfaced via a separate "Verify unlock" button in the UI.

* **Stable JSON shape.** The inspector front-end pins the section
  ids + check keys verbatim. Adding fields is allowed; renaming or
  removing them is a breaking change -- the front-end will silently
  drop unknown keys but a missing key would empty a card. Add a new
  test in ``tests/security/test_audit_view.py`` whenever the
  schema grows.

* **No secrets in the output.** Everything in the returned dict is
  fingerprint-safe (counts, booleans, paths, key fingerprints --
  not key bodies). The bearer-token-not-leaking invariant pinned in
  :mod:`tests.security.test_security_invariants` extends to this
  module: nothing here should call ``service_auth.derive_*`` or
  similar.

* **Severity grading.** Every check returns one of:

  - ``"ok"``    — the layer is in the documented-good state.
  - ``"warn"``  — the layer is in a known-degraded but not-broken
    state (e.g. SIP disabled with dev-mode explicitly set; vault
    not mounted but bundle on disk).
  - ``"fail"``  — the layer is in a state that violates the
    SECURITY.md guarantee (e.g. plaintext char data on disk).
  - ``"info"``  — informational only, no posture change (e.g. the
    DR backup is optional).

  The UI maps ``ok → green, warn → yellow, fail → red, info → grey``.

The top-level :func:`snapshot` returns the whole dict; the
sub-functions are exposed individually for unit tests + for future
CLI mirroring (``./run.sh security audit`` is the planned operator
command).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# Status enum encoded as a string so it survives JSON round-trip
# without hand-rolling a TypedDict on the front-end. Stable wire
# values; do not renumber.
OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"


@dataclass
class Check:
    """One row in the audit-view table. The front-end pins ``key``
    so don't rename existing keys without bumping ``snapshot()``'s
    schema version below.
    """
    key: str
    label: str
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
        }


def _safe(fn: Callable[[], Check], *, key: str, label: str) -> Check:
    """Call ``fn`` and convert any exception into a FAIL row so a
    broken layer doesn't tank the whole audit view. The bug surfaces
    as a FAIL with the exception in the detail dict, which is what
    the operator needs to see anyway."""
    try:
        return fn()
    except Exception as exc:
        return Check(
            key=key,
            label=label,
            status=FAIL,
            summary=f"check raised {type(exc).__name__}: {exc}",
            detail={"error_type": type(exc).__name__, "error_msg": str(exc)},
        )


# ---------------------------------------------------------------------------
# Per-layer checks


def check_sip() -> Check:
    """Defense layer 0 — System Integrity Protection."""
    from local_scribe.common import dev_mode
    from local_scribe.security import sip_check

    rep = sip_check.status()
    dev = dev_mode.is_enabled()
    # ``SIPState`` is a str-Enum so equality against the string form
    # of its value works either way. The canonical "good" state is
    # ``FULLY_ENABLED``.
    state_str = rep.state.value if hasattr(rep.state, "value") else str(rep.state)
    fully_enabled = state_str == sip_check.SIPState.FULLY_ENABLED.value
    detail = {
        "state": state_str,
        "raw_top_line": getattr(rep, "raw_top_line", ""),
        "dev_mode": dev,
    }
    if fully_enabled and not dev:
        return Check("sip", "SIP enforcement", OK,
                     "System Integrity Protection is fully enabled.",
                     detail)
    if dev:
        return Check("sip", "SIP enforcement", WARN,
                     "Dev mode is set — SIP gate is bypassed. "
                     "Production hosts must clear LOCAL_SCRIBE_DEV_MODE "
                     "and re-enable SIP via Recovery → `csrutil enable`.",
                     detail)
    return Check("sip", "SIP enforcement", FAIL,
                 f"SIP is in state {detail['state']!r} and dev mode is not "
                 "set; every other defense layer assumes SIP keeps the "
                 "task_for_pid + DYLD boundary. Reboot into Recovery and "
                 "run `csrutil enable`.",
                 detail)


def check_master_key() -> Check:
    """Defense layer 4 — Option C split-key presence."""
    from local_scribe.security import secret_store, yubikey_backup
    has_kc = secret_store.has_kc_half()
    has_yk = yubikey_backup.has_yk_half()
    legacy = secret_store.has_master_key()
    detail = {"kc_half_present": has_kc, "yk_half_present": has_yk,
              "legacy_whole_key_present": legacy}
    if has_kc and has_yk:
        return Check("master_key", "Option C split-key", OK,
                     "Both halves present (Touch ID + YubiKey).", detail)
    if legacy:
        return Check("master_key", "Option C split-key", WARN,
                     "Legacy whole-key Keychain item present; will migrate "
                     "to Option C on next unlock.", detail)
    if has_kc and not has_yk:
        return Check("master_key", "Option C split-key", FAIL,
                     "Keychain half present but YubiKey half missing. "
                     "Run `./run.sh key init --re-enroll-yubikey` or "
                     "`./run.sh key dr-restore`.", detail)
    return Check("master_key", "Option C split-key", FAIL,
                 "No master key on this machine. Run `./run.sh bootstrap` "
                 "(first-time) or `./run.sh key init` (re-enroll).", detail)


def check_vault() -> Check:
    """Defense layer 3 — at-rest encrypted vault.

    Combines: bundle existence, mount state, char-data relocation,
    and the new plaintext-leftover scan. The detail dict carries all
    three so the UI can render each as its own sub-bullet.
    """
    from local_scribe.security import vault as vault_mod
    s = vault_mod.status()
    leftovers = vault_mod.find_plaintext_char_data_copies()
    leftover_total = sum(f.size_bytes for f in leftovers)
    detail = {
        "bundle_exists": s["exists"],
        "mounted": s["mounted"],
        "char_data_relocated": s["char_data_relocated"],
        "bundle_path": s["bundle_path"],
        "mount_path": s["mount_path"],
        "char_data_path": s["char_data_path"],
        "bundle_size_bytes": s["bundle_size_bytes"],
        "plaintext_leftovers": [f.to_dict() for f in leftovers],
        "plaintext_leftover_count": len(leftovers),
        "plaintext_leftover_total_bytes": leftover_total,
    }

    if not s["exists"]:
        return Check("vault", "Encrypted vault (at-rest)", FAIL,
                     "Sparse-bundle vault is not on disk. "
                     "Run `./run.sh vault init` to create it.", detail)

    # The most severe of (relocated, leftovers) wins.
    if not s["char_data_relocated"]:
        return Check("vault", "Encrypted vault (at-rest)", FAIL,
                     "Vault is on disk but Char's data dir is NOT a symlink "
                     "into it. Char is writing PLAINTEXT to "
                     f"{s['char_data_path']}. Fix: quit Char, then "
                     "`./run.sh vault unlock`.", detail)

    if leftovers:
        # Surface this as FAIL because the user explicitly asked us
        # to confirm there is ONLY ONE copy of their Char data on
        # disk. Any leftover is a violation of that promise.
        # PRE_VAULT_BACKUP / PRE_ARCH_BACKUP are real-data copies
        # (high severity); DEMO_CACHE is synthetic seed data (still
        # leftover but informational).
        real_copies = [f for f in leftovers
                       if f.kind != vault_mod.LeftoverKind.DEMO_CACHE]
        if real_copies:
            return Check("vault", "Encrypted vault (at-rest)", FAIL,
                         f"{len(real_copies)} plaintext copies of Char data "
                         f"are sitting outside the vault on disk "
                         f"({leftover_total / (1024**3):.2f} GiB total). "
                         "Review and delete each via the 'Plaintext copies' "
                         "panel below.",
                         detail)
        return Check("vault", "Encrypted vault (at-rest)", WARN,
                     "Char data is correctly inside the vault, but the "
                     "demo-cache directory (synthetic seed data) is still "
                     "on disk. Safe to delete from the panel below.",
                     detail)

    if not s["mounted"]:
        return Check("vault", "Encrypted vault (at-rest)", WARN,
                     "Vault is on disk and the symlink is correct, but the "
                     "vault is not currently mounted. Char will fail to "
                     "read its data until `./run.sh vault unlock`.", detail)

    return Check("vault", "Encrypted vault (at-rest)", OK,
                 "Vault mounted, Char data relocated, no plaintext "
                 "leftovers detected on disk.", detail)


def check_signed_config() -> Check:
    """Defense layer 6 — signed pinned config.

    Walks the canonical signed-files list (``pinned.json`` +
    ``char_baseline.json`` if present), runs the cheap on-disk
    ``signed_config.status(path)`` for each, and rolls them up.
    Never unlocks the master key — operator-facing ``config verify``
    is the path that actually re-computes the HMAC.
    """
    from local_scribe.common import pinned as _p
    from local_scribe.security import signed_config
    files = [("pinned", _p.pinned_path(), _p.pinned_sig_path())]
    baseline = Path.home() / ".config" / "local_scribe" / "char_baseline.json"
    if baseline.is_file():
        files.append(("char_baseline", baseline,
                      baseline.with_name(baseline.name + ".sig")))
    by_file: dict[str, dict[str, Any]] = {}
    ok_keys: list[str] = []
    bad_keys: list[str] = []
    for name, path, sig_path in files:
        st = signed_config.status(path, sig_path=sig_path)
        by_file[name] = {
            "path": str(path),
            "sig_path": str(sig_path),
            "protected_present": st.protected_present,
            "sig_present": st.sig_present,
            "sig_parseable": st.sig_parseable,
            "sig_fp": st.sig_fp,
            "sig_alg": st.sig_alg,
            "note": st.note,
        }
        if st.protected_present and st.sig_present and st.sig_parseable:
            ok_keys.append(name)
        else:
            bad_keys.append(name)
    detail = {"by_file": by_file, "ok": ok_keys, "bad": bad_keys}
    if not bad_keys:
        return Check("signed_config", "Signed pinned config", OK,
                     "pinned.json" + (" + char_baseline.json"
                                      if "char_baseline" in ok_keys
                                      else "")
                     + " sidecar(s) present and parse cleanly. "
                     "(Full HMAC re-verification is the separate "
                     "`./run.sh config verify` path that requires a "
                     "Touch ID + YubiKey unlock.)", detail)
    return Check("signed_config", "Signed pinned config", FAIL,
                 f"Sidecar missing or malformed for: "
                 f"{', '.join(bad_keys)}. Run `./run.sh config sign`.",
                 detail)


def check_script_integrity() -> Check:
    """Cross-cutting — operator-facing .py / .sh / .swift drift."""
    from local_scribe.security import script_integrity
    rep = script_integrity.verify()
    # ``Report`` may not be importable; we rely on attribute access.
    drift = getattr(rep, "drift", None) or getattr(rep, "drifted", None) or []
    missing = getattr(rep, "missing", None) or []
    detail = {
        "drift_count": len(drift),
        "missing_count": len(missing),
        "drift_paths": [str(p) for p in (drift or [])[:20]],
    }
    if not drift and not missing:
        return Check("script_integrity", "Script integrity", OK,
                     "Every operator-facing script matches its pinned "
                     "blob hash in git.", detail)
    return Check("script_integrity", "Script integrity", FAIL,
                 f"{len(drift)} scripts drifted from their pinned blob "
                 "hash. Run `git status` and reconcile, or set "
                 "LOCAL_SCRIBE_ALLOW_DIRTY=1 to bypass (dangerous).",
                 detail)


def check_char_integrity() -> Check:
    """Defense layer 5 — Char binary integrity."""
    from local_scribe.char import char_integrity
    try:
        rep = char_integrity.verify()
    except FileNotFoundError as exc:
        # Char.app isn't installed yet.
        return Check("char_integrity", "Char binary integrity", WARN,
                     f"Char.app not installed yet: {exc}",
                     {"error": str(exc)})
    drift = getattr(rep, "drift", None) or getattr(rep, "drifted", None) or []
    detail = {
        "drift_count": len(drift),
        "drift_paths": [str(p) for p in (drift or [])[:20]],
    }
    if not drift:
        return Check("char_integrity", "Char binary integrity", OK,
                     "CDHash, Team ID, Bundle ID and linked-lib hashes "
                     "match the recorded baseline.", detail)
    return Check("char_integrity", "Char binary integrity", FAIL,
                 f"{len(drift)} component(s) drifted from the baseline. "
                 "Run `./run.sh char baseline-update` if you intentionally "
                 "upgraded Char.app.", detail)


def check_char_settings() -> Check:
    """Defense layer 5 — Char settings drift (api_key, base_url, …)."""
    from local_scribe.char import char_audit
    from local_scribe.common.config import load_config
    try:
        rep = char_audit.audit(load_config())
    except Exception as exc:
        return Check("char_settings", "Char settings enforcement", WARN,
                     f"Could not audit Char settings: {exc}",
                     {"error": str(exc)})
    d = rep.to_dict()
    summary_counts = d.get("summary", {})
    fail_like = summary_counts.get("miss", 0) + summary_counts.get("warn", 0)
    detail = {"summary": summary_counts}
    if fail_like == 0:
        return Check("char_settings", "Char settings enforcement", OK,
                     "Char's settings.json is pointed at our loopback ASR "
                     "and telemetry is off.", detail)
    return Check("char_settings", "Char settings enforcement", WARN,
                 f"{fail_like} setting(s) drifted from the expected "
                 "values. Open the Char audit tab for details + "
                 "`Run configure-char` to remediate.", detail)


def check_firewall() -> Check:
    """Defense layer 1 — per-Char outbound firewall.

    Our shipping firewall design is the per-process wrapper
    (sandbox-exec + HTTPS_PROXY) applied at ``./run.sh char launch``
    time -- the system-wide ``/etc/hosts`` mode is an optional
    operator opt-in. So "NOT INSTALLED" on the hosts-file mode is
    NOT a failure on its own; what we actually need to know is
    whether the per-process bits are in place.
    """
    from local_scribe.egress import firewall, char_sandbox
    # ``firewall.status()`` returns a dataclass; flatten to a plain
    # JSON-serialisable dict so the inspector front-end can render
    # it without bespoke marshalling.
    try:
        s = firewall.status()
        hosts_state: dict[str, Any] = {
            "installed": bool(getattr(s, "installed", False)),
            "blocked_count": len(getattr(s, "blocked_hostnames", []) or []),
            "missing_count": sum(
                len(v) for v in (getattr(s, "missing_by_category", {}) or {}).values()
            ),
            "coverage": dict(getattr(s, "coverage_by_category", {}) or {}),
        }
    except Exception as exc:
        hosts_state = {"installed": False, "error": str(exc)}
    sandbox_ok = False
    try:
        path = char_sandbox.profile_path()
        sandbox_ok = Path(path).is_file()
    except Exception:
        pass
    detail = {
        "hosts_file_mode": hosts_state,
        "sandbox_profile_present": sandbox_ok,
    }
    if sandbox_ok:
        msg = ("Per-Char sandbox profile is on disk. ``./run.sh char "
               "launch`` will route Char through the loopback egress "
               "proxy on port 8889. (System-wide /etc/hosts mode is "
               "optional — opt in with `./run.sh firewall enable "
               "--mode system`.)")
        return Check("firewall", "Egress firewall (per-Char)", OK,
                     msg, detail)
    return Check("firewall", "Egress firewall (per-Char)", WARN,
                 "Per-Char sandbox profile is not on disk. Run "
                 "`./run.sh char sandbox write`. Without this, "
                 "launching Char via `./run.sh char launch` will fall "
                 "back to no-firewall mode.", detail)


def check_pre_commit_hook() -> Check:
    """Defense layer 7 — secret-scan pre-commit hook.

    Only relevant in a working tree (operators on a tarball clone
    have no .git/ and we surface that as INFO).
    """
    from local_scribe import __file__ as ls_file
    repo_root = Path(ls_file).resolve().parents[1]
    git_dir = repo_root / ".git"
    hook = git_dir / "hooks" / "pre-commit"
    scanner = repo_root / "tools" / "secret_scan.sh"
    detail = {
        "repo_root": str(repo_root),
        "git_dir_present": git_dir.exists(),
        "hook_present": hook.exists(),
        "scanner_present": scanner.exists(),
    }
    if not git_dir.exists():
        return Check("precommit_hook", "Secret-scan pre-commit hook", INFO,
                     "Not a git working tree; the hook is contributor-"
                     "only and has nothing to install against.", detail)
    if not scanner.exists():
        return Check("precommit_hook", "Secret-scan pre-commit hook", FAIL,
                     "tools/secret_scan.sh is missing from the repo.",
                     detail)
    if not hook.exists():
        return Check("precommit_hook", "Secret-scan pre-commit hook", WARN,
                     "Pre-commit hook is not installed. Run "
                     "`./tools/install_git_hooks.sh`.", detail)
    contents = ""
    try:
        contents = hook.read_text(errors="replace")
    except OSError:
        pass
    delegates = "secret_scan.sh" in contents and "--staged" in contents
    detail["hook_delegates_correctly"] = delegates
    if delegates:
        return Check("precommit_hook", "Secret-scan pre-commit hook", OK,
                     "Pre-commit hook installed and delegates to "
                     "tools/secret_scan.sh --staged.", detail)
    return Check("precommit_hook", "Secret-scan pre-commit hook", WARN,
                 "Pre-commit hook exists but does not delegate to the "
                 "shipped scanner. Run `./tools/install_git_hooks.sh` "
                 "to refresh it.", detail)


def check_disaster_recovery() -> Check:
    """Optional layer — DR passphrase backup. Surfaced INFO unless
    explicitly absent."""
    from local_scribe.security import disaster_recovery as dr
    has = dr.has_backup()
    detail = {"backup_present": has}
    if has:
        return Check("dr", "Disaster-recovery backup", OK,
                     "Passphrase-encrypted master-key backup on disk.",
                     detail)
    return Check("dr", "Disaster-recovery backup", INFO,
                 "No DR backup configured. Recommended for laptops you "
                 "carry off-site; run `./run.sh key dr-create`.", detail)


# ---------------------------------------------------------------------------
# Top-level composition

SCHEMA_VERSION = 1
"""Bump whenever a check key is renamed or removed. The inspector
front-end refuses to render an unknown schema_version."""


def snapshot() -> dict[str, Any]:
    """Run every layer's cheap check and return the aggregated dict.

    Shape:

      {
        "schema_version": 1,
        "summary": {"ok": N, "warn": N, "fail": N, "info": N},
        "checks": [Check.to_dict(), ...],
      }

    The ordering of ``checks`` is the operator-facing layer order
    (0 → SIP, 1 → firewall, 2 → service auth (implicit via
    master_key), 3 → vault, 4 → master key, 5 → char settings +
    integrity, 6 → signed config, 7 → pre-commit hook, plus DR +
    script integrity at the end).
    """
    checks = [
        _safe(check_sip, key="sip", label="SIP enforcement"),
        _safe(check_firewall, key="firewall",
              label="Egress firewall (per-Char)"),
        _safe(check_master_key, key="master_key",
              label="Option C split-key"),
        _safe(check_vault, key="vault",
              label="Encrypted vault (at-rest)"),
        _safe(check_char_settings, key="char_settings",
              label="Char settings enforcement"),
        _safe(check_char_integrity, key="char_integrity",
              label="Char binary integrity"),
        _safe(check_signed_config, key="signed_config",
              label="Signed pinned config"),
        _safe(check_script_integrity, key="script_integrity",
              label="Script integrity"),
        _safe(check_pre_commit_hook, key="precommit_hook",
              label="Secret-scan pre-commit hook"),
        _safe(check_disaster_recovery, key="dr",
              label="Disaster-recovery backup"),
    ]
    summary = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for c in checks:
        summary[c.status] = summary.get(c.status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
    }
