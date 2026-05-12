# Security audit — guarantee × code × test traceability matrix

> **Status:** 2026-05-11. This audit was triggered by the operator
> request *"please go back and confirm each and every one of our
> security guarantees are in place and remediate if not, create and
> run tests."* It enumerates every guarantee documented in
> [`SECURITY.md`](../SECURITY.md) and [`CRYPTO.md`](../CRYPTO.md),
> resolves each to the module that enforces it and the test that
> pins it, and records the remediation step for every gap that the
> audit surfaced.

## How to use this document

- The **Defense layer** rows are the canonical 8-layer model from
  `SECURITY.md`. Each row says exactly what the layer promises,
  where the enforcement lives, and which test would fail if the
  promise was silently weakened.
- The **Cross-cutting invariants** rows are the promises that span
  more than one layer (e.g. "the master key is never on argv of any
  subprocess"). These are usually what an external review cares
  about first — they're the ones whose violation collapses several
  layers at once.
- The **Audit findings** section records every gap surfaced by this
  audit and the commit that remediated it. New gaps surface here
  the moment we notice them — closing a finding requires both code
  AND test, and the commit lands as one PR.
- To re-run the audit: `./venv/bin/python -m pytest tests/security/
  --timeout=30 --timeout-method=signal -q` is the fast inner loop.
  The slower whole-suite run is `./venv/bin/python -m pytest tests/`.

## Defense layers

| # | Layer | What it promises | Code | Test file(s) |
|---|---|---|---|---|
| 0 | SIP enforcement | Refuse to start when System Integrity Protection is not fully enabled; honour `LOCAL_SCRIBE_DEV_MODE` as the **only** documented bypass; emit a loud red banner on every bypassed boot so the operator can never "forget" they're in dev mode. | [`local_scribe/security/sip_check.py`](../local_scribe/security/sip_check.py), `sip_gate` in [`run.sh`](../run.sh) | [`test_sip_check.py`](../tests/security/test_sip_check.py), [`test_dev_mode.py`](../tests/common/test_dev_mode.py) |
| 1 | Egress firewall | Char's network traffic is routed through a per-process `HTTPS_PROXY=127.0.0.1:8889` injected by `./run.sh char launch` (via `/usr/bin/open -a Char.app --env …`); every host in `firewall.BLOCK_CATALOG` returns 502 from the egress proxy. The SBPL `sandbox-exec` profile is still rendered + validated for manual operator use but is no longer applied at launch (it broke macOS TCC's system-audio-capture attribution — see [`CHAR_REVIEW.md` § Layered firewall trade-offs](CHAR_REVIEW.md#layered-firewall-trade-offs-the-may-2026-sandbox-exec-drop)). A `tccd`-log probe surfaces "responsible bundle is a terminal" regressions in `./run.sh char firewall-status`. Optional system-wide `/etc/hosts` mode for operators who want a machine-wide block. | [`local_scribe/egress/firewall.py`](../local_scribe/egress/firewall.py), [`char_sandbox.py`](../local_scribe/egress/char_sandbox.py), [`char_tcc_probe.py`](../local_scribe/egress/char_tcc_probe.py), [`egress_proxy.py`](../local_scribe/egress/egress_proxy.py) | [`test_firewall.py`](../tests/egress/test_firewall.py), [`test_char_sandbox.py`](../tests/egress/test_char_sandbox.py), [`test_char_tcc_probe.py`](../tests/egress/test_char_tcc_probe.py), [`test_egress_proxy.py`](../tests/egress/test_egress_proxy.py) |
| 2 | Inter-service auth | Every ASR + Inspector endpoint except `/health` requires a HKDF-derived bearer token; tokens are 16-byte HKDF outputs with `info=b"service:<name>"`; comparison is constant-time; tokens are never on disk, never in env vars, never in argv. | [`local_scribe/security/service_auth.py`](../local_scribe/security/service_auth.py) | [`test_service_auth.py`](../tests/security/test_service_auth.py), [`test_inspector_server.py`](../tests/inspector/test_inspector_server.py), [`test_asr_server.py`](../tests/asr/test_asr_server.py) |
| 3 | At-rest encryption | Char's session directory lives in an AES-256 sparse-bundle vault unlocked from the master key. Bootstrap auto-creates + auto-relocates Char's data into the vault. `cmd_start` refuses to launch unless `vault.char_data_relocated()` is True (override: `LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1`, loud yellow banner). | [`local_scribe/security/vault.py`](../local_scribe/security/vault.py), [`vault_unlock.py`](../local_scribe/security/vault_unlock.py), `vault_relocation_gate` in [`run.sh`](../run.sh) | [`test_vault_unlock.py`](../tests/security/test_vault_unlock.py), [`test_vault_relocation_gate.py`](../tests/bootstrap/test_vault_relocation_gate.py) |
| 4 | Option C split-key | `master_key = kc_half XOR yk_half`; `kc_half` lives in the Keychain behind Touch ID, `yk_half` is `age`-encrypted to a YubiKey identity. Master key is reconstituted in process RAM only, `forget()`ed on shutdown, never written whole to disk, never on argv. | [`local_scribe/security/key_lifecycle.py`](../local_scribe/security/key_lifecycle.py), [`key_split.py`](../local_scribe/security/key_split.py), [`secret_store.py`](../local_scribe/security/secret_store.py), [`yubikey_backup.py`](../local_scribe/security/yubikey_backup.py) | [`test_key_lifecycle.py`](../tests/security/test_key_lifecycle.py) (incl. `ThreatModelInvariantTests`), [`test_key_split.py`](../tests/security/test_key_split.py), [`test_secret_store.py`](../tests/security/test_secret_store.py), [`test_yubikey_backup.py`](../tests/security/test_yubikey_backup.py), [`test_touchid_keychain_real.py`](../tests/security/test_touchid_keychain_real.py) |
| 5 | Char-settings enforcement | `char_settings_writer.py` atomically writes the four critical Char `settings.json` keys (api_key, base_url, telemetry off, posthog off) via stdin (never argv); `char_audit.py` flags drift on every startup; `char_integrity_gate` refuses to launch a Char.app whose CDHash / Team-ID / Bundle-ID / linked-lib hashes don't match the recorded baseline. | [`local_scribe/char/char_settings_writer.py`](../local_scribe/char/char_settings_writer.py), [`char_audit.py`](../local_scribe/char/char_audit.py), [`char_integrity.py`](../local_scribe/char/char_integrity.py), `char_integrity_gate` in [`run.sh`](../run.sh) | [`test_char_settings_writer.py`](../tests/char/test_char_settings_writer.py), [`test_char_audit.py`](../tests/char/test_char_audit.py), [`test_char_integrity.py`](../tests/char/test_char_integrity.py), [`test_char_persist.py`](../tests/char/test_char_persist.py) |
| 6 | Signed pinned config | `pinned.json` (Char DMG SHA-256s, Team ID, LM Studio version) + `char_baseline.json` are HMAC-signed with a subkey derived from the master key (`info=b"config:hmac"`). `pinned_config_gate` refuses to start unless both signatures verify (override: `LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG=1`, loud yellow banner). Bootstrap auto-signs both files at the end of stage 10. | [`local_scribe/security/signed_config.py`](../local_scribe/security/signed_config.py), [`pinned.py`](../local_scribe/common/pinned.py), `pinned_config_gate` in [`run.sh`](../run.sh) | [`test_signed_config.py`](../tests/security/test_signed_config.py), [`test_pinned.py`](../tests/common/test_pinned.py), [`test_bootstrap_autosign.py`](../tests/bootstrap/test_bootstrap_autosign.py) |
| 7 | Secret-scan pre-commit hook | `tools/secret_scan.sh` detects PEM private-key blocks, `AGE-SECRET-KEY-1…`, `AGE-PLUGIN-YUBIKEY-1…`, `sk-…`, `sk-ant-…`, `AKIA…`, `ghp_…`, `gho_…`, `xox[abprs]-…`, and JWTs in staged content; allowlists Sentry public DSNs + synthetic `AAAA…`/`deadbeef…`/`cafebabe…` test fixtures; `tools/install_git_hooks.sh` wires it into `.git/hooks/pre-commit` and is invoked automatically by `cmd_bootstrap`. | [`tools/secret_scan.sh`](../tools/secret_scan.sh), [`tools/install_git_hooks.sh`](../tools/install_git_hooks.sh) | [`test_secret_scan_hook.py`](../tests/security/test_secret_scan_hook.py) — 25 cases (installer + 10 patterns + 4 allowlist + 7 forbidden paths) |

## Cross-cutting invariants

| Invariant | Pinned by |
|---|---|
| **Master key never appears on subprocess argv** (init, unlock, rotate). Pinned by `subprocess.run` mock + `mk.as_hex() not in " ".join(cmd)` for every captured invocation. | [`test_key_lifecycle.py::ThreatModelInvariantTests::test_master_key_never_in_subprocess_argv`](../tests/security/test_key_lifecycle.py) |
| **Master key bytes never end up on disk in cleartext** (walks every file under `LOCAL_SCRIBE_CONFIG_DIR` after init). | [`test_key_lifecycle.py::ThreatModelInvariantTests::test_no_plaintext_master_key_on_disk`](../tests/security/test_key_lifecycle.py) |
| **Vault passphrase never on hdiutil argv** for `create`, `mount`, `rotate_password` — only via `-stdinpass` / `-newstdinpass` on stdin. | [`test_security_invariants.py::VaultPassphraseNotInArgvTests`](../tests/security/test_security_invariants.py) |
| **Derived bearer tokens stay in process RAM.** Token body does not appear in `os.environ` or anywhere under `LOCAL_SCRIBE_CONFIG_DIR` after a fresh `derive_service_token` call. `ServiceToken.repr` redacts the body. | [`test_security_invariants.py::BearerTokenNotInEnvOrFilesTests`](../tests/security/test_security_invariants.py) |
| **`LOCAL_SCRIBE_DEV_MODE` bypasses ONLY `sip_gate`.** Static parse of `run.sh` confirms `master_key_gate`, `script_integrity_gate`, `pinned_config_gate`, `vault_relocation_gate`, `char_integrity_gate` do not reference the env var. | [`test_security_invariants.py::DevModeBoundaryTests`](../tests/security/test_security_invariants.py) |
| **Every non-SIP gate has its own named override env var** (`LOCAL_SCRIBE_ALLOW_DIRTY`, `LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG`, `LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA`, `LOCAL_SCRIBE_ALLOW_DIRTY_CHAR`). No hidden DEV_MODE side effects. | [`test_security_invariants.py::DevModeBoundaryTests::test_each_non_sip_gate_has_its_own_named_override`](../tests/security/test_security_invariants.py) |
| **Script-integrity gate** re-hashes every operator-facing `.py` / `.sh` / `.swift` against git's pinned blob SHA on every startup; drift refuses to continue unless `LOCAL_SCRIBE_ALLOW_DIRTY=1` is set, which then prints a one-line yellow warning on every subsequent command. | [`test_script_integrity.py`](../tests/security/test_script_integrity.py) |
| **Char binary integrity gate** verifies CDHash + Team ID + Bundle ID + every linked Mach-O hash against `char_baseline.json`. | [`test_char_integrity.py`](../tests/char/test_char_integrity.py) |
| **Touch ID + YubiKey banners** print explicit yellow/red banners before every `kc_half` / `yk_half` access so the operator never wonders why the terminal "froze". Quiet mode for tests via `LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS=1`. | [`test_touch_prompts.py`](../tests/common/test_touch_prompts.py), [`test_key_lifecycle.py::TouchPromptIntegrationTests`](../tests/security/test_key_lifecycle.py) |
| **Bootstrap auto-signs config** at the end of stage 10 (Touch ID + YubiKey) so a fresh install doesn't fail on the first `start` with "no signature". | [`test_bootstrap_autosign.py`](../tests/bootstrap/test_bootstrap_autosign.py) |
| **Bootstrap auto-relocates Char data** into the vault during stage 4 if existing plaintext sessions are detected (with operator confirmation). `cmd_status` surfaces encryption-at-rest state. | [`test_vault_relocation_gate.py`](../tests/bootstrap/test_vault_relocation_gate.py) |
| **`SECURITY.md` doc freshness.** Every `local_scribe/...py` path referenced in the doc must resolve; the 8 defense-layer headings must remain present; CRYPTO.md must continue to reference HKDF-SHA256; SECURITY.md must reference this audit doc. | [`test_security_invariants.py::SecurityDocFreshnessTests`](../tests/security/test_security_invariants.py) |
| **Only one copy of Char data on disk** lives outside the encrypted sparse-bundle (the demo cache is informational only). The scanner enumerates every documented leftover pattern + the deleter refuses any path not in the current leftover set, so a stolen bearer can't `rm -rf` arbitrary dirs. | [`test_vault_plaintext_copies.py`](../tests/security/test_vault_plaintext_copies.py) |
| **`./run.sh stop` dismounts the vault.** `vault_lock_on_stop` runs after every service is killed; the at-rest-encryption guarantee has a sharp time boundary (mounted while pipeline is up, ciphertext-on-disk the moment `stop` returns). Polite-detach only — refuses to `-force` while Char.app is still running so a live SQLite handle on `app.db` can't be truncated mid-write. | [`test_vault_lock_on_stop.py`](../tests/bootstrap/test_vault_lock_on_stop.py), [`test_vault_unmount_force.py`](../tests/security/test_vault_unmount_force.py) |
| **`./run.sh start` re-mounts a previously-locked vault** with `vault_auto_unlock_gate`, prompting Touch ID + YubiKey if the bundle is on disk but unmounted. Refuses to launch services if the unlock fails (would otherwise hit a dangling symlink and corrupt it). | [`test_vault_auto_unlock_gate.py`](../tests/bootstrap/test_vault_auto_unlock_gate.py) |
| **Bearer-token warmup is single-unlock.** `cmd_start` runs `service_auth warm asr inspector` ONCE in the foreground shell (Touch ID + YubiKey banners visible to the operator), then spawns each service with its token in its PER-SUBPROCESS environ (`VAR=val cmd` form). The parent shell never holds the token. | [`test_service_auth_warm.py`](../tests/security/test_service_auth_warm.py), [`test_warmup_service_tokens.py`](../tests/bootstrap/test_warmup_service_tokens.py), [`test_asr_server_prewarmed_token.py`](../tests/asr/test_asr_server_prewarmed_token.py), [`test_inspector_server_prewarmed_token.py`](../tests/inspector/test_inspector_server_prewarmed_token.py) |
| **Audit view is cheap** — no Touch ID / YubiKey / `hdiutil` calls inside `audit_view.snapshot()`. Safe to invoke on every inspector page load. Pinned by `CheapnessTests` which patches `unlock_master_key` + `_hdiutil` to raise `AssertionError`. | [`test_audit_view.py::CheapnessTests`](../tests/security/test_audit_view.py) |
| **No secret leakage in audit-view JSON.** A sentinel master-key body fed into the underlying layers must not appear in `json.dumps(snapshot())`. | [`test_audit_view.py::NoSecretLeakageTests`](../tests/security/test_audit_view.py) |

## Audit findings — 2026-05-11 cycle

The findings below were surfaced during this audit cycle and
remediated in the same commit set that landed this document. Each
row records (a) the gap, (b) the operational impact, (c) the
commit that landed the fix, (d) the test now guarding it.

| # | Gap | Impact | Remediation | Test guard |
|---|---|---|---|---|
| F1 | Char's `~/Library/Application Support/hyprnote` was a plaintext directory rather than a symlink into the encrypted vault. Operator had 6 sessions of recorded audio + transcripts in cleartext on disk despite a working vault, master key, and `bootstrap`. | Layer 3 (at-rest encryption) was effectively bypassed on every install that pre-dated the bootstrap-auto-relocate change. Time Machine / iCloud / forensic-imager all see plaintext. | `vault_relocation_gate` added to `cmd_start`; bootstrap stage 4 now auto-mounts the vault and relocates Char data with operator confirmation; `cmd_status` surfaces encryption-at-rest state. Commit `49974ec`. | [`test_vault_relocation_gate.py`](../tests/bootstrap/test_vault_relocation_gate.py) (6 tests) |
| F2 | `pinned.json` and `char_baseline.json` were not auto-signed by bootstrap. First `./run.sh start` after a fresh install failed with `FAIL [pinned] no signature; run ./run.sh config sign`. | Bootstrap left the install in a non-runnable state; operators had to know to run `./run.sh config sign` manually. | `cmd_bootstrap` now invokes `local_scribe config sign` at end of stage 10 (idempotent, Touch ID + YubiKey). Commit `936221c`. | [`test_bootstrap_autosign.py`](../tests/bootstrap/test_bootstrap_autosign.py) (4 tests) |
| F3 | Touch ID + YubiKey prompts during `key init` / `unlock` were silent — the terminal looked frozen and the operator didn't know to glance at their YubiKey for the flash. | UX → operators stop trusting the tool and reach for `--no-prompt`-style escape hatches that nobody actually implemented. | `local_scribe/common/touch_prompts.py` prints loud yellow (Touch ID) / red (YubiKey) banners before every blocking authn step. Wired into `key_lifecycle.unlock_master_key` + `key_safety.require_physical_presence`. Commit `44673c8`. | [`test_touch_prompts.py`](../tests/common/test_touch_prompts.py) (18 tests), [`test_key_lifecycle.py::TouchPromptIntegrationTests`](../tests/security/test_key_lifecycle.py) (5 tests) |
| F4 | Stale `python -c '...'` and `python -m ...` invocations in `run.sh` heredocs referenced pre-reorg module names (`char_sandbox`, `char_settings_writer`). `set -e` + `2>/dev/null` swallowed the `ModuleNotFoundError`, causing bootstrap to **silently exit** mid-flow. | Bootstrap appeared to succeed; subsequent `start` failed with a downstream error that gave no hint about the broken bootstrap. | All inline + dash-`m` invocations updated to `local_scribe.*` paths. Two new static-analysis test classes added to `run_sh_imports`. Commit `a346add`. | [`test_run_sh_imports.py::RunShPythonDashCImportsResolveTests`](../tests/bootstrap/test_run_sh_imports.py), [`test_run_sh_imports.py::RunShPythonDashMResolveTests`](../tests/bootstrap/test_run_sh_imports.py) |
| F5 | No test exercised `tools/secret_scan.sh`. The pre-commit hook (Layer 7) could be silently broken by a future edit and the suite would stay green. | Layer 7 silent failure → contributors push secrets to GitHub. | `tests/security/test_secret_scan_hook.py` adds 25 tests covering installer behaviour, every pattern in the scanner's regex catalog, every allowlist exclusion, and every forbidden-path rule. Commit `<this audit>`. | [`test_secret_scan_hook.py`](../tests/security/test_secret_scan_hook.py) (25 tests) |
| F6 | No test pinned the cross-cutting "vault passphrase never on argv" / "bearer token not in env" / "dev mode boundary" invariants. Layer-internal tests would happily pass while one of these silently broke. | A future refactor could leak secrets to argv (visible in `ps`) or `os.environ` (visible to child processes) without any test failing. | `tests/security/test_security_invariants.py` adds 17 cross-cutting invariant tests (dev-mode boundary, vault passphrase not in argv, bearer token not in env/files, doc freshness). Commit `<this audit>`. | [`test_security_invariants.py`](../tests/security/test_security_invariants.py) (17 tests) |
| F7 | `docs/SECURITY_AUDIT.md` did not exist. No single document mapped the SECURITY.md prose to the code + tests that back it; reviewers had to grep for each claim. | Audit fatigue; an external review would take days instead of an hour. | This document. Commit `<this audit>`. | `test_security_invariants.py::SecurityDocFreshnessTests::test_security_md_references_audit_doc` |
| F10 | `./run.sh start` fired **two independent Touch ID + YubiKey unlocks** (one inside each of the ASR worker and the inspector worker), and the explanatory `touch_prompts` banners landed in each worker's *log file* rather than the operator's terminal — because the daemonised spawn redirects stdout/stderr. The operator was left staring at `starting asr ...` with no textual cue that they needed to authenticate. (Direct quote: *"when the services are loading it needs to print out the instruction to accept the touchid press and tell user when the press the yubikey"*.) | UX → every `start` was a confusing 4-modal sequence that operators learned to dread. Worse, with no foreground banner, a new contributor watching their first start has no idea what's happening or why their Mac keeps prompting them. | New `service_auth.warm_tokens(services)` + `python -m service_auth warm <s ...>` CLI verb derives every requested service's HKDF token from a SINGLE master-key unlock. `cmd_start` invokes it in the foreground (Touch ID + YubiKey banners visible in the operator's terminal), captures the JSON, and spawns each service with `LOCAL_SCRIBE_<SERVICE>_TOKEN=$tok funcname` (per-subprocess env, never `export`ed into the parent). `asr_server` + `inspector_server` lifespan hooks now check the env var FIRST and short-circuit the unlock. | [`test_service_auth_warm.py`](../tests/security/test_service_auth_warm.py) (19 tests: single-unlock invariant, env-override resolution order, no-environ-leakage), [`test_warmup_service_tokens.py`](../tests/bootstrap/test_warmup_service_tokens.py) (10 tests covering the bash helper + 4 static wiring checks that catch the per-subprocess form vs `export` regression), [`test_asr_server_prewarmed_token.py`](../tests/asr/test_asr_server_prewarmed_token.py), [`test_inspector_server_prewarmed_token.py`](../tests/inspector/test_inspector_server_prewarmed_token.py) (worker-side env-token contract incl. whitespace trimming) |
| F9 | `./run.sh stop` killed the services but **left the encrypted sparse bundle mounted**. Operator caught this manually by running `mount` after `stop` and seeing `/Users/user/Library/Application Support/local_scribe-vault` still in the list. The at-rest-encryption guarantee was effectively "until the operator manually runs `./run.sh vault lock`" rather than "while the pipeline is up". | Layer 3 (at-rest encryption) guarantee was substantially weaker than documented — anyone with read access to the operator's filesystem (a curious shell session, a misconfigured Time Machine, a compromised app running as the user) could read every session's cleartext audio/transcripts for hours or days after `stop`. | `vault_lock_on_stop` helper added to `run.sh`, wired into `cmd_stop` after every service is killed. Polite-detach only — refuses `-force` while Char is running so SQLite `app.db` isn't corrupted; reports the offending process if anything else (Finder window, Spotlight indexer) holds the volume open. Counterpart `vault_auto_unlock_gate` in `cmd_start` re-mounts on next start with Touch ID + YubiKey. New `vault.unmount(force=False)` + `vault_unlock.unmount_vault(force=False)` + `vault_unlock lock --polite` CLI flag. | [`test_vault_lock_on_stop.py`](../tests/bootstrap/test_vault_lock_on_stop.py) (10 tests covering happy path, idempotent no-op branches, Char-running safety branch, polite-failure warning, both env overrides, plus static wiring check that `cmd_stop` invokes the hook AFTER service stops), [`test_vault_auto_unlock_gate.py`](../tests/bootstrap/test_vault_auto_unlock_gate.py) (8 tests covering already-mounted, no-vault, unlock-trigger, unlock-failure, both env overrides, plus static wiring check that the gate runs between `vault_relocation_gate` and `char_integrity_gate`), [`test_vault_unmount_force.py`](../tests/security/test_vault_unmount_force.py) (10 tests pinning the `force=False` / `--polite` semantics) |
| F8 | Operator had **3 plaintext copies** of Char data sitting outside the encrypted vault (2 × 2.5 GiB real-data backups + 1 × 2 MiB demo seed). The vault gate + symlink were correct, but `relocate_char_data()`'s safety backup, an older bootstrap rev's `pre_arch_backup`, and the demo seed were all untouched. Nothing in the operator-facing surface flagged the state. | Layer 3 (at-rest encryption) was effectively bypassed for any leftover that landed in Time Machine / Spotlight / forensic-imager scope. | `vault.find_plaintext_char_data_copies()` + `vault.delete_plaintext_copy()`. New `local_scribe/security/audit_view.py` aggregates the state, surfaced through `GET /api/security/audit` and the new "Security verification" panel in the Char audit tab of the inspector UI. Guided per-row deletion gated by typed-DELETE confirm + the deleter's whitelist (refuses any path not currently in the scanner's output). | [`test_vault_plaintext_copies.py`](../tests/security/test_vault_plaintext_copies.py) (17 tests covering the scanner + deleter), [`test_audit_view.py`](../tests/security/test_audit_view.py) (21 tests covering the aggregator's shape + grading + cheapness invariant), plus 6 new inspector endpoint tests in [`test_inspector_server.py::AuthTests`](../tests/inspector/test_inspector_server.py) |

## What is intentionally NOT pinned by this audit

Out-of-scope items, documented here so a future operator doesn't
spend hours hunting for a test that was never going to exist:

- **Real hdiutil / `age` / `ykman` / Touch ID round-trips.** These
  need a real YubiKey + Touch-ID-capable laptop. The dedicated
  end-to-end test runs are gated by `RUN_E2E_AUDIT=1` and are not
  part of CI — they're an operator-driven step before tagging a
  release. See `SECURITY.md` § "Verifying it works on your machine"
  for the manual recipe.
- **`csrutil` enabled-vs-disabled coverage.** SIP state cannot be
  toggled at test time; we cover the parser with synthetic
  `csrutil status` outputs and rely on the live `sip_gate` banner
  on dev hosts (which is the documented operator-facing posture).
- **Physical YubiKey enrollment.** `tests/security/test_yubikey_backup_enroll.py`
  uses a high-fidelity Bash fake (`_fake_age_plugin_yubikey.py`)
  that mirrors `age-plugin-yubikey 0.5.x`'s real CLI behaviour
  including the slot-not-empty recovery path. The real PIV
  interaction is tested manually before each release.
- **Server-side push protection.** The L7 hook is a *client-side*
  guardrail. Server-side complement (GitHub Push Protection,
  GitHub Secret Scanning) is configured at the org level and is
  out of this project's scope.
- **Kernel-level or task-port adversaries.** Adversary tiers 6 and
  7 in `SECURITY.md` § "Threat model" are explicitly out of scope
  because `task_for_pid()` / `dtrace -p` would defeat every
  user-space defense in this document. The Defense layer 0 SIP
  gate is what keeps these tiers contained, and it has no test
  coverage *of the kernel boundary itself* by design — the parser
  + gate path is what we can pin.

## Re-running this audit

```bash
# Fast inner loop — every test referenced in this matrix runs in ~5s.
./venv/bin/python -m pytest \
  tests/security/ tests/common/ tests/egress/ tests/bootstrap/ \
  tests/char/ tests/inspector/ \
  --timeout=30 --timeout-method=signal -q

# Slow full sweep — includes ASR backends + transcribe-file glue.
./venv/bin/python -m pytest tests/ \
  --timeout=60 --timeout-method=signal -q
```

If either invocation produces a failure, **stop and triage it**.
This audit is the documented contract; a red test in this file is
how a regression would surface, and silently disabling the test
defeats the whole point.
