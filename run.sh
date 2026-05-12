#!/usr/bin/env bash
# local_scribe - service manager for the local ASR + LLM pipeline.
#
# Services managed:
#   - LM Studio API @ :1234            (started via `lms server start`)
#     + qwen3-30b-a3b-instruct-2507    (loaded via `lms load`)
#   - ASR server @ :8000               (uvicorn local_scribe.asr.asr_server:app)
#                                        backend = parakeet (default) or whisper
#
# The ASR backend (parakeet-mlx) and diarization backend (sherpa-onnx)
# both run in-process - no separate service.
#
# First-time setup on a freshly cloned repo:
#   ./run.sh bootstrap      one-shot: build venv, install pip deps, download
#                            ASR + diarization model weights, then print
#                            "next steps" (Char + LM Studio + Qwen). Safe to
#                            re-run; skips work that's already done.
#   ./run.sh start          run preflight (auto-install anything bootstrap
#                            missed), start LM Studio + ASR server, tail the
#                            ASR log. Ctrl+C detaches; services keep running.
#                            Optional ``--dev`` flag sets LOCAL_SCRIBE_DEV_MODE=1
#                            for the run — bypasses the SIP gate, makes the
#                            inspector render a sticky red banner, and surfaces
#                            "dev mode" in doctor/status. Production operators
#                            must NEVER use --dev. See SECURITY.md § 'Dev mode'.
#
# Day-to-day:
#   ./run.sh stop           stop the ASR server (LM Studio left alone)
#   ./run.sh restart        stop + start
#   ./run.sh status         show service health, PIDs, ports
#   ./run.sh logs           tail the ASR server log
#   ./run.sh health         one-shot HTTP health check
#   ./run.sh doctor         deep preflight report - python, deps, models,
#                            services, Char-config hints. Safe any time.
#   ./run.sh setup          force-reinstall pip deps + (re)download models
#   ./run.sh install-llm    install LM Studio.app (via Homebrew Cask), bootstrap
#                            the lms CLI, start its HTTP server, download the
#                            chosen Qwen model (~32 GB MLX for the 30B, or
#                            ~2.3 GB MLX for the 4B fallback on <48 GB Macs),
#                            and load it. Idempotent; skips any step already
#                            completed. Bootstrap step (4/5) calls this.
#   ./run.sh install-char   download the pinned Char.app DMG from GitHub
#                            Releases, verify SHA256, install to /Applications.
#                            Refuses to clobber a different installed version
#                            without confirmation.
#   ./run.sh configure-char point Char's OpenAI transcriber at this server
#                            (interactive; offers to back up existing API key).
#                            Bootstrap calls install-char + configure-char in
#                            sequence on a fresh install.
#   ./run.sh inspector {start|stop|restart|status|open|logs}
#                            local web UI on :8001 with a list of Char's
#                            sessions (audio playback + transcript + notes),
#                            a config editor for ~/.config/local_scribe/
#                            config.json, and a Char audit tab that flags
#                            drift toward non-local providers. Loopback
#                            only by default.
#   ./run.sh transcribe FILE [args...]
#                            run transcribe_file.py FILE with the venv python
#   ./run.sh redo-session SESSION [--speakers N] [--cluster-threshold F]
#                            re-run ASR + diarization on an existing Char
#                            session and overwrite its transcript.json.
#                            Use when the original Generate produced a
#                            single-speaker blob (1:1 call) or over-clustered
#                            (long meeting -> 30+ phantom speakers).
#   ./run.sh firewall {status|enable|disable|list|verify} [--strict]
#   ./run.sh char {status|fingerprint|baseline-set|baseline-update|baseline-clear}
#                            manage the /etc/hosts block list that severs
#                            Char's link to Sentry / PostHog / its
#                            auto-updater + every external STT/LLM provider.
#                            `enable` and `disable` require sudo (we prompt
#                            via the macOS admin dialog). See SECURITY.md
#                            for the full host catalog and threat model.
#   ./run.sh key {status|init|unlock|rotate|add-yubikey|dr-restore|migrate|destroy|backups}
#                            manage the split-key (Option C) master key:
#                            Keychain (Touch ID) AND YubiKey (tap) both
#                            required to unlock. ``init`` walks first-time
#                            enrollment and optional passphrase-encrypted
#                            disaster-recovery backup. All key material
#                            flows over stdin / Keychain ACL; never argv,
#                            never env, never logs. See ARCHITECTURE.md
#                            §4 + SECURITY.md for the threat model. See
#                            KEY_SAFETY.md for data-loss recovery flows.
#   ./run.sh vault {init|unlock|lock|status}
#                            manage the AES-256 sparse-bundle vault that
#                            holds Char's session data + our transcripts.
#                            The hdiutil passphrase is HKDF-derived from
#                            the master key, so every unlock is gated on
#                            Touch ID + a YubiKey tap. ``init`` is run
#                            automatically by ``bootstrap``.
#   ./run.sh yubikey {enroll|verify|restore|list|status}
#                            convenience surface for YubiKey operations.
#                            ``enroll`` adds a backup YubiKey (chains to
#                            ``key add-yubikey`` once the recipient is
#                            generated). ``verify`` is a round-trip tap
#                            test. ``restore <snap>`` re-instates a
#                            yk_half.age from a key-safety snapshot.
#   ./run.sh config {status|show|verify|sign}
#                            signed pinned-config layer. ``status`` is the
#                            read-only snapshot (no master-key unlock).
#                            ``show [--shell|--json]`` prints the pinned
#                            values (Char version, DMG hashes, Team ID,
#                            LM Studio version) from
#                            local_scribe/common/pinned.json. ``verify``
#                            HMACs both that file AND
#                            ~/.config/local_scribe/char_baseline.json
#                            against the operator master key (Touch ID +
#                            YubiKey tap) and is the gate ``./run.sh start``
#                            enforces. ``sign`` blesses the current
#                            contents of both files with one tap; auto-
#                            triggered by ``./run.sh char baseline-set``.
#                            See SECURITY.md § Defense layer 6 for the
#                            threat model.
#
# Env overrides (prefer ~/.config/local_scribe/config.json for these):
#   ASR_BACKEND       default parakeet  (parakeet | whisper)
#   ASR_PORT          default 8000      (where the ASR server listens)
#   PARAKEET_MODEL    default mlx-community/parakeet-tdt-0.6b-v3
#   WHISPER_MODEL     default large-v3-turbo  (only used if ASR_BACKEND=whisper)
#   LMSTUDIO_PORT     default 1234
#   LLM_MODEL         default qwen3-30b-a3b-instruct-2507
#   LLM_CONTEXT       default 65536
#   INSPECTOR_BIND    default 127.0.0.1
#   INSPECTOR_PORT    default 8001
#   PYTHON            default python3.14 (else python3.12, else python3)
#
# Env vars layered on top of config.json (env wins) so legacy scripts keep
# working unchanged. Inspector "Config" tab and `vim ~/.config/local_scribe/
# config.json` both edit the same file.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV_DIR="$REPO/venv"
VENV_PY="$VENV_DIR/bin/python"
RUN_DIR="$REPO/.run"
mkdir -p "$RUN_DIR"

ASR_PID_FILE="$RUN_DIR/asr_server.pid"
ASR_LOG_FILE="$RUN_DIR/asr_server.log"
INSPECTOR_PID_FILE="$RUN_DIR/inspector_server.pid"
INSPECTOR_LOG_FILE="$RUN_DIR/inspector_server.log"
EGRESS_PROXY_PID_FILE="$RUN_DIR/egress_proxy.pid"
EGRESS_PROXY_LOG_FILE="$RUN_DIR/egress_proxy.log"
EGRESS_PROXY_PORT="${EGRESS_PROXY_PORT:-8889}"
DEPS_STAMP="$RUN_DIR/deps.stamp"   # mtime tracks last successful pip install

ASR_BACKEND_DEFAULT="${ASR_BACKEND:-parakeet}"
PARAKEET_MODEL="${PARAKEET_MODEL:-mlx-community/parakeet-tdt-0.6b-v3}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"
ASR_PORT="${ASR_PORT:-8000}"
LMSTUDIO_PORT="${LMSTUDIO_PORT:-1234}"
INSPECTOR_PORT="${INSPECTOR_PORT:-8001}"
INSPECTOR_BIND="${INSPECTOR_BIND:-127.0.0.1}"

# Default LLM (recommended for ≥48 GB unified memory).
LLM_MODEL="${LLM_MODEL:-qwen3-30b-a3b-instruct-2507}"
LLM_MODEL_REPO="${LLM_MODEL_REPO:-qwen/qwen3-30b-a3b-instruct-2507}"

# Smaller fallback offered automatically on Macs with <48 GB unified memory.
# Uses the repo-path form (qwen/qwen3-4b) which is what `lms ls`, `lms load`,
# and LM Studio's /api/v0/models all report for this model.
LLM_MODEL_SMALL="qwen/qwen3-4b"
LLM_MODEL_SMALL_REPO="qwen/qwen3-4b"

# Minimum unified memory (GB) before we'll auto-pull the 30B model. Below
# this threshold the bootstrap step offers the 4B fallback instead.
LLM_MIN_RAM_GB="${LLM_MIN_RAM_GB:-48}"

LLM_CONTEXT="${LLM_CONTEXT:-65536}"

# --- Pinned distribution constants (Char, LM Studio) ----------------------
#
# These used to be hardcoded here. They now live in
# ``local_scribe/common/pinned.json`` (a single signable source of truth)
# and we source them via ``python -m local_scribe config show --shell``.
# See ``docs/SECURITY.md`` § "Signed pinned config" for the threat model:
# the JSON file is HMAC-signed by the operator (Touch ID + YubiKey) so a
# local attacker can't silently rewrite an expected DMG hash or Team ID.
#
# Bootstrap path: a fresh checkout has no signature yet (chicken-and-egg
# with the master key), so we use the *unverified* read here. The hard
# signature check lives in ``./run.sh start`` (see :gate_pinned_config:
# below) — the values we source here are only used for download/install
# bookkeeping where a separate codesign + Gatekeeper check at install
# time is the actual security gate.
#
# To bump a pinned value: edit local_scribe/common/pinned.json, audit
# the diff, then ``./run.sh config sign`` to re-bless with your YubiKey.
__pinned_show() {
  # Subshell `eval` keeps a parse error from poisoning our env.
  local out
  if ! out=$("$REPO/venv/bin/python" -m local_scribe config show --shell 2>/dev/null); then
    out=$(python3 -m local_scribe config show --shell 2>/dev/null) || true
  fi
  printf '%s\n' "$out"
}
eval "$(__pinned_show)"
unset -f __pinned_show

# Fallback hardcoded values for the absolute pre-bootstrap state where
# the venv doesn't exist yet and ``python -m local_scribe`` can't even
# import. These match pinned.json verbatim; ``./run.sh doctor`` flags
# any divergence between the two so this fallback can't silently drift.
: "${CHAR_KNOWN_GOOD_VERSION:=1.0.24}"
: "${CHAR_RELEASE_TAG:=desktop_v${CHAR_KNOWN_GOOD_VERSION}}"
: "${CHAR_RELEASE_BASE_URL:=https://github.com/fastrepl/anarlog/releases/download/${CHAR_RELEASE_TAG}}"
: "${CHAR_DMG_SHA256_AARCH64:=7f9c06881b9593b2aec17c8eddd65e5eb67d2c1072bfd008501989eb4181da89}"
: "${CHAR_DMG_SHA256_X86_64:=e7061d274308b563df724d7da5ede80e0cc68ff7082a3586b41ed8cc2c815503}"
: "${LMSTUDIO_KNOWN_GOOD_VERSION:=0.4.12}"
: "${LMSTUDIO_APP_PATH:=/Applications/LM Studio.app}"

# --- styling helpers ---
if [[ -t 1 ]]; then
  c_green=$'\033[32m'; c_red=$'\033[31m'; c_yellow=$'\033[33m'
  c_bold=$'\033[1m';   c_dim=$'\033[2m';  c_reset=$'\033[0m'
else
  c_green=""; c_red=""; c_yellow=""; c_bold=""; c_dim=""; c_reset=""
fi
say() { printf "%s[%s]%s %s\n" "$c_dim" "$(date '+%H:%M:%S')" "$c_reset" "$1"; }
ok()  { printf "%s●%s " "$c_green"  "$c_reset"; }
bad() { printf "%s○%s " "$c_red"    "$c_reset"; }
warn(){ printf "%s○%s " "$c_yellow" "$c_reset"; }

# Layer 0 — System Integrity Protection gate. The most fundamental
# gate of all: every other defense in the project (script integrity,
# Char integrity, Keychain ACL, YubiKey tap, firewall, vault) assumes
# the kernel enforces user-space process isolation. With SIP off:
#   - task_for_pid() is unrestricted → any process can mach_vm_read
#     our heap and pull the reconstituted master key out of our
#     Python process after a Touch ID unlock.
#   - DYLD_INSERT_LIBRARIES is no longer stripped from hardened-
#     runtime binaries → arbitrary code is loadable into Char and
#     our services before our integrity checks fire.
#   - /usr/bin/codesign, /usr/bin/security, csrutil itself become
#     replaceable, defeating every higher-layer verifier.
# There is ONE documented operator-facing override: setting
# ``LOCAL_SCRIBE_DEV_MODE=1`` (or starting via ``./run.sh start --dev``)
# bypasses this gate with a loud red banner. See
# ``local_scribe/common/dev_mode.py`` and ``SECURITY.md § 'Dev mode'``
# for the full rationale. Production operators should never set
# this; it exists for developers iterating on the pipeline itself
# on a host that doesn't have SIP configured.
#
# Gate semantics:
#   - cmd_start / cmd_bootstrap / cmd_key / cmd_configure_char /
#     cmd_redo_session ALL gate on this.
#   - cmd_stop / cmd_status / cmd_logs / cmd_doctor / cmd_firewall
#     do NOT gate (they don't touch keys; the operator may need
#     them precisely to clean up before rebooting to recovery).
#   - dev mode is a per-process env var; it does NOT mutate any
#     on-disk state, so unsetting it and re-running re-engages the
#     gate immediately.
sip_gate() {
  # Dev-mode short-circuit. Recognise any truthy value so this
  # stays in sync with local_scribe/common/dev_mode.py's parser.
  local devmode="${LOCAL_SCRIBE_DEV_MODE:-}"
  local devmode_lower
  devmode_lower="$(printf '%s' "$devmode" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "$devmode" && "$devmode_lower" != "0" && "$devmode_lower" != "false" \
        && "$devmode_lower" != "no" && "$devmode_lower" != "off" ]]; then
    if [[ -x "$VENV_PY" ]]; then
      # Use the Python module so the banner is identical to what the
      # service lifespans emit on direct uvicorn launch — keeps the
      # operator-facing output consistent across entry points.
      "$VENV_PY" -c \
        'from local_scribe.common import dev_mode; import sys; dev_mode.emit_banner_once(sys.stderr)' \
        2>/dev/null || true
    fi
    # Always print a final shell-side reminder so even
    # bootstrap-before-venv invocations show the bypass marker.
    printf '\n'
    printf "%s%s[DEV MODE] sip_gate bypassed — LOCAL_SCRIBE_DEV_MODE is set.%s\n" \
           "$c_red" "$c_bold" "$c_reset"
    printf "  See SECURITY.md § 'Dev mode' for what this costs you.\n"
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    # During bootstrap before the venv exists, fall back to the
    # bare csrutil invocation. We still fail closed.
    if ! command -v csrutil >/dev/null 2>&1; then
      say "${c_red}refusing to run: /usr/bin/csrutil not available${c_reset}"
      say "  cannot confirm System Integrity Protection state."
      return 1
    fi
    if ! csrutil status 2>&1 | grep -q "status: enabled\\.$"; then
      printf '\n'
      printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
             "$c_red" "$c_reset"
      printf "%s%sREFUSING TO RUN: System Integrity Protection is not fully enabled.%s\n" \
             "$c_red" "$c_bold" "$c_reset"
      printf '  csrutil reports:\n'
      csrutil status 2>&1 | sed 's/^/    /'
      printf '\n'
      printf "  Boot into Recovery and run %scsrutil enable%s, then reboot.\n" \
             "$c_bold" "$c_reset"
      printf "  See SECURITY.md § 'Defense layer 0' for the full rationale.\n"
      printf "  If this is a dev host, set %sLOCAL_SCRIBE_DEV_MODE=1%s\n" \
             "$c_bold" "$c_reset"
      printf "  or use %s./run.sh start --dev%s — see SECURITY.md § 'Dev mode'.\n" \
             "$c_bold" "$c_reset"
      printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
             "$c_red" "$c_reset"
      return 1
    fi
    return 0
  fi
  # venv ready — use the Python module for the rich banner + custom-
  # configuration handling.
  if "$VENV_PY" -m local_scribe.security.sip_check check 2>&1 >&2; then
    return 0
  fi
  return 1
}

# Master-key start-guard. ``./run.sh start`` reconstitutes the master
# key from kc_half (Keychain, Touch ID) ⊕ yk_half (age + YubiKey).
# Without either half present, the pipeline can't:
#   * mint service bearer tokens (Char ↔ ASR ↔ Inspector traffic
#     becomes 401 across the board)
#   * unlock the encrypted vault that holds Char's session data
#   * encrypt new on-disk transcripts at rest
# Rather than start with degraded security, refuse to launch and
# point the operator at the canonical setup command. The first
# install path is ``./run.sh bootstrap``, which runs ``key init``
# automatically; if that's already been done but the install was
# destroyed (laptop reset, etc.), ``./run.sh key init`` (or
# ``./run.sh key dr-restore``) are the supported recovery paths.
#
# The venv-missing case is treated separately because it means the
# operator hasn't bootstrapped at all -- pointing them at ``key init``
# would be the wrong hint.
master_key_gate() {
  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}refusing to start: Python venv not built${c_reset}"
    say "  run ${c_bold}./run.sh bootstrap${c_reset} first (one-time setup)."
    return 1
  fi
  # Treat either Option C kc_half OR a legacy v1 whole-key Keychain item
  # as "key is on this machine". The legacy item triggers migration on
  # first unlock; we don't want to refuse the first launch after an
  # update just because migration hasn't run yet.
  if "$VENV_PY" -c '
import sys
from local_scribe.security import secret_store
sys.exit(0 if (secret_store.has_kc_half() or secret_store.has_master_key()) else 1)
' 2>/dev/null; then
    return 0
  fi
  printf '\n'
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_red" "$c_reset"
  printf "%s%sREFUSING TO START: no master key on this machine.%s\n" \
         "$c_red" "$c_bold" "$c_reset"
  printf '\n'
  printf "  Without a master key, the ASR + Inspector services cannot mint\n"
  printf "  bearer tokens, the encrypted vault cannot be unlocked, and new\n"
  printf "  transcripts cannot be encrypted at rest.\n\n"
  printf "  Pick the recovery path:\n"
  printf "    First-time setup:   %s./run.sh bootstrap%s          (runs key init for you)\n" \
         "$c_bold" "$c_reset"
  printf "    Re-enroll only:     %s./run.sh key init%s           (Touch ID + YubiKey)\n" \
         "$c_bold" "$c_reset"
  printf "    Recovering laptop:  %s./run.sh key dr-restore%s     (DR passphrase)\n" \
         "$c_bold" "$c_reset"
  printf '\n'
  printf "  See README.md → 'Master key management' and KEY_SAFETY.md.\n"
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_red" "$c_reset"
  return 1
}

# Layer A — script-integrity gate. We re-hash every operator-facing
# .py / .sh / .swift in the working tree and compare it to git's
# pinned blob hash at HEAD. On drift we print the red banner from
# ``script_integrity.format_banner`` and refuse to continue unless
# ``LOCAL_SCRIBE_ALLOW_DIRTY=1`` is set. The override path then
# prints the one-line warning ahead of *every* further command so
# the operator is reminded they're running in a degraded state.
#
# The check is opportunistic: if git isn't on PATH (tarball install)
# or this isn't a working tree (no .git dir) we skip with a note.
# Set ``LOCAL_SCRIBE_SKIP_INTEGRITY=1`` to skip even from a checkout
# (only used by our own tests; the operator-facing override is
# ``LOCAL_SCRIBE_ALLOW_DIRTY``, which is louder).
script_integrity_gate() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0   # bootstrap path, venv not built yet
  fi
  if "$VENV_PY" -m local_scribe.security.script_integrity --check >&2; then
    return 0
  fi
  # Non-zero exit -> drift detected. The banner is already on stderr.
  if [[ "${LOCAL_SCRIBE_ALLOW_DIRTY:-0}" != "0" ]]; then
    # Print the compact override-warning one-liner so it's
    # impossible to forget which mode we're in.
    "$VENV_PY" -c "
import sys
from local_scribe.security import script_integrity as si
rep = si.verify()
sys.stderr.write(si.format_override_warning(rep, color=sys.stderr.isatty()) + '\n')
" >&2 || true
    return 0
  fi
  say "${c_red}refusing to continue with a tampered working tree.${c_reset}"
  say "  fix the drift above, or set ${c_bold}LOCAL_SCRIBE_ALLOW_DIRTY=1${c_reset}"
  say "  (the banner explains why this is dangerous)"
  return 1
}

# Layer A½ — pinned-config signature gate. Verifies the HMAC over
# ``local_scribe/common/pinned.json`` against the operator's master
# key (Touch ID + YubiKey). Catches the case where someone (malware,
# a compromised editor extension, a malicious supply-chain commit)
# rewrote the expected Char DMG SHA-256s, Team ID, or LM Studio
# version pin to weaken later checks.
#
# Sits AFTER master_key_gate (we need the key to verify) and AFTER
# script_integrity_gate (so a tampered ``signed_config.py`` doesn't
# get to lie about its own verification). It sits BEFORE
# char_integrity_gate because the latter reads the pinned Team ID +
# Bundle ID — we want to refuse to *use* pinned values that haven't
# been blessed.
#
# Honours ``LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG=1`` as a temporary
# escape hatch for pre-bootstrap states. Bootstrap itself sets this
# automatically before the very first ``config sign`` runs.
#
# Skip path: the signature gate is INFORMATIVE not BLOCKING in the
# pre-bootstrap window where the master key doesn't exist yet
# (master_key_gate will already have refused above; if we're here,
# the key exists and we expect a signature).
pinned_config_gate() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0   # bootstrap path, venv not built yet
  fi
  # `config verify` already prints a contextual remediation banner.
  # Exit codes: 0 ok | 10 missing sidecar | 11 mismatch | 12 key fp
  # mismatch | 13 other signed-config error | 1 cannot unlock master.
  if "$VENV_PY" -m local_scribe config verify >&2; then
    return 0
  fi
  local rc=$?
  if [[ "${LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG:-0}" != "0" ]]; then
    printf "%s⚠  pinned config not blessed; continuing anyway because LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG=1.%s\n" \
           "$c_yellow" "$c_reset" >&2
    return 0
  fi
  say "${c_red}refusing to start: pinned config signature is invalid.${c_reset}"
  say "  fix with: ${c_bold}./run.sh config sign${c_reset}"
  say "  (or set LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG=1 to bypass — dangerous)"
  return 1
}

# Layer A¾ — vault-relocation gate. The encrypted sparse-bundle
# vault is created by bootstrap stage 4, and ``vault.relocate_char_data()``
# moves ``~/Library/Application Support/hyprnote`` INTO the mounted
# vault and replaces the original location with a symlink. The whole
# encryption-at-rest story falls apart if the operator skips that
# relocation: Char keeps writing plaintext (session audio.mp3,
# transcript.json, app.db, search_index/) straight to the home
# filesystem, where Time Machine / backup software / forensic
# tooling can pick it up. The 2026-05-11 audit found 6 unencrypted
# sessions sitting at ``~/Library/Application Support/hyprnote/sessions``
# despite a working vault + master key — this gate is the fix.
#
# Failure mode: refuse to start with a loud banner pointing the
# operator at ``./run.sh vault unlock`` (which mounts + relocates
# atomically; Char.app must be quit first).
#
# Sits AFTER ``master_key_gate`` (we need the key to query vault
# state), AFTER ``pinned_config_gate`` (no point trusting unsigned
# vault state), and BEFORE ``char_integrity_gate`` (because we don't
# want to verify the Char binary against a baseline if we'd refuse
# to launch anyway). Skipped if ``LOCAL_SCRIBE_SKIP_INTEGRITY=1``
# (test seam) or the venv hasn't been built yet (bootstrap path).
#
# Override: ``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1``. Loud-but-
# explicit, mirroring ``LOCAL_SCRIBE_ALLOW_DIRTY``. Use this only if
# you're knowingly running on a tmpfs / scratch system where the
# encrypted-at-rest guarantee doesn't matter (CI, throwaway VM).
vault_relocation_gate() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0   # bootstrap path, venv not built yet
  fi
  # ``char_data_relocated()`` returns True iff the canonical Char
  # data dir is a symlink resolving INTO the vault mount. It does
  # NOT require the vault to be mounted (the symlink existence
  # check is enough to verify the relocation happened). If the
  # vault isn't mounted at start time, the next stage (asr_start /
  # inspector_start) would fail when it tries to follow the symlink
  # — that's also a documented failure mode, surfaced separately.
  if "$VENV_PY" -c '
import sys
from local_scribe.security import vault
sys.exit(0 if vault.char_data_relocated() else 1)
' 2>/dev/null; then
    return 0
  fi
  if [[ "${LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA:-0}" != "0" ]]; then
    printf "%s⚠  Char data dir is NOT relocated into the vault; continuing anyway because LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1.%s\n" \
           "$c_yellow" "$c_reset" >&2
    printf "%s    Session audio + transcripts will be written PLAINTEXT to ~/Library/Application Support/hyprnote.%s\n" \
           "$c_yellow" "$c_reset" >&2
    return 0
  fi
  printf '\n'
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_red" "$c_reset"
  printf "%s%sREFUSING TO START: Char data is PLAINTEXT on disk.%s\n" \
         "$c_red" "$c_bold" "$c_reset"
  printf '\n'
  printf "  Char's data dir lives at:\n"
  printf "    %s~/Library/Application Support/hyprnote%s\n" "$c_dim" "$c_reset"
  printf "  That dir contains:\n"
  printf "    %s•%s every session's audio.mp3 + transcript.json\n" "$c_dim" "$c_reset"
  printf "    %s•%s app.db (SQLite — speakers, calendars, notes, chats)\n" "$c_dim" "$c_reset"
  printf "    %s•%s search_index/ (Tantivy full-text index of transcripts)\n" "$c_dim" "$c_reset"
  printf "  The encrypted vault you created at bootstrap is empty + unused;\n"
  printf "  this gate exists so we don't quietly normalise the broken state.\n"
  printf '\n'
  printf "  %sFix (one-shot per machine — Touch ID + YubiKey tap):%s\n" \
         "$c_bold" "$c_reset"
  printf "    1.  %sQuit Char.app%s (Cmd-Q — closing the window is not enough;\n" \
         "$c_bold" "$c_reset"
  printf "        its SQLite handle MUST be released or we'll corrupt the DB).\n"
  printf "    2.  %s./run.sh stop%s   (no-op if the pipeline isn't already up).\n" \
         "$c_bold" "$c_reset"
  printf "    3.  %s./run.sh vault unlock%s   (Touch ID + YubiKey tap; mounts\n" \
         "$c_bold" "$c_reset"
  printf "        the vault, moves ~/Library/Application Support/hyprnote\n"
  printf "        INTO it, then replaces the original with a symlink so\n"
  printf "        Char continues to work without any reconfiguration).\n"
  printf "    4.  %s./run.sh start%s    (this gate will then pass).\n" \
         "$c_bold" "$c_reset"
  printf '\n'
  printf "  Override (DANGEROUS — disables encryption-at-rest entirely):\n"
  printf "    %sLOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1 ./run.sh start%s\n" \
         "$c_bold" "$c_reset"
  printf '\n'
  printf "  See SECURITY.md § 'Defense layer 4 — encrypted vault'.\n"
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_red" "$c_reset"
  return 1
}

# Layer A.2 — auto-mount the vault before services come up. Sits
# AFTER ``vault_relocation_gate`` (no point in mounting if the data
# isn't relocated -- the relocation gate has already failed loudly).
# Idempotent: if the vault is already mounted, this is a no-op. If
# it isn't, we shell out to ``vault_unlock unlock --no-relocate`` and
# THAT will prompt for Touch ID + YubiKey to derive the hdiutil
# passphrase. The ``--no-relocate`` flag is correct because the
# relocation gate just confirmed Char's data dir is already a
# symlink into the mount -- we just need the mount itself live.
#
# Without this gate, the sequence "./run.sh stop" (which dismounts)
# then "./run.sh start" would launch services against a dangling
# symlink (Char's data dir points into an unmounted volume) and the
# first read would surface as a cryptic ENOENT.
#
# Honours the same environment overrides as vault_relocation_gate so
# the "no vault relationship at all" dev path stays consistent.
vault_auto_unlock_gate() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ "${LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA:-0}" != "0" ]]; then
    return 0   # operator explicitly opted out of at-rest encryption
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0   # bootstrap path, venv not built yet
  fi
  # Probe: does a vault exist on disk and is it currently mounted?
  # Exit codes:
  #   0 → mounted, nothing to do
  #   1 → exists but not mounted (we'll unlock)
  #   2 → doesn't exist (skip; ``vault_relocation_gate`` would already
  #        have refused -- this branch is defense in depth)
  local rc=0
  "$VENV_PY" -c '
import sys
from local_scribe.security import vault
if not vault.exists():
    sys.exit(2)
sys.exit(0 if vault.is_mounted() else 1)
' 2>/dev/null
  rc=$?
  case "$rc" in
    0) return 0 ;;
    2) return 0 ;;
    1)
      printf "%s%sVault is locked — unlocking before services come up%s\n" \
             "$c_bold" "$c_yellow" "$c_reset"
      printf "  Touch ID + YubiKey tap required (the hdiutil passphrase is\n"
      printf "  HKDF-derived from the master key; we never store it).\n\n"
      if "$VENV_PY" -m local_scribe.security.vault_unlock unlock --no-relocate; then
        return 0
      fi
      printf '\n'
      printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
             "$c_red" "$c_reset"
      printf "%s%sREFUSING TO START: vault unlock failed.%s\n" \
             "$c_red" "$c_bold" "$c_reset"
      printf '\n'
      printf "  The encrypted vault is on disk but couldn't be mounted.\n"
      printf "  Char's data dir is a symlink INTO the (unmounted) volume,\n"
      printf "  so launching services now would corrupt that symlink the\n"
      printf "  moment Char tries to read from it.\n"
      printf '\n'
      printf "  Common causes:\n"
      printf "    %s•%s Touch ID prompt cancelled / wrong fingerprint\n" \
             "$c_dim" "$c_reset"
      printf "    %s•%s YubiKey not plugged in / tap timed out\n" \
             "$c_dim" "$c_reset"
      printf "    %s•%s Master key rotated but vault not re-keyed\n" \
             "$c_dim" "$c_reset"
      printf '\n'
      printf "  Recover with: %s./run.sh vault unlock%s   (verbose; same prompts).\n" \
             "$c_bold" "$c_reset"
      printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
             "$c_red" "$c_reset"
      return 1
      ;;
    *)
      # Probe itself crashed -- log loud + refuse rather than mask.
      printf "%svault auto-unlock probe failed (rc=%d); refusing to start%s\n" \
             "$c_red" "$rc" "$c_reset" >&2
      return 1
      ;;
  esac
}

# Counterpart of ``vault_auto_unlock_gate``: dismount the encrypted
# sparse bundle on the way out. Called from ``cmd_stop`` AFTER every
# service has been killed (otherwise ASR / inspector still have
# read handles on files inside the mount and ``hdiutil detach``
# would either bounce or, with ``-force``, risk truncating an
# in-flight write).
#
# Design constraints:
#
#   * Polite ``detach`` only -- NEVER ``-force``. The vault contains
#     Char's SQLite DB (``app.db``); a forced unmount mid-write is the
#     classic recipe for ``database is locked`` followed by hours of
#     ``.recover`` work. If polite detach fails we leave the volume
#     mounted and tell the operator exactly which process is holding
#     it open. Lock-on-stop is a defense-in-depth nicety, not a
#     hard guarantee -- the operator quitting Char.app is.
#
#   * Idempotent: no-op when no vault exists, when venv is missing,
#     or when the vault is already unmounted. This means ``cmd_stop``
#     can safely call us even when run twice or when ``cmd_start``
#     never actually got off the ground.
#
#   * Honours ``LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA=1`` (no vault
#     relationship to manage) and ``LOCAL_SCRIBE_SKIP_INTEGRITY=1``
#     (test seam) for symmetry with the start-side gate.
vault_lock_on_stop() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ "${LOCAL_SCRIBE_ALLOW_PLAINTEXT_CHAR_DATA:-0}" != "0" ]]; then
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0
  fi
  # Probe state first -- same exit-code convention as the unlock gate:
  #   0 → mounted (we should detach)
  #   1 → exists but not mounted (nothing to do)
  #   2 → no vault on disk (nothing to do)
  local rc=0
  "$VENV_PY" -c '
import sys
from local_scribe.security import vault
if not vault.exists():
    sys.exit(2)
sys.exit(0 if vault.is_mounted() else 1)
' 2>/dev/null
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    return 0   # not mounted, or no vault -- nothing to lock
  fi

  # Refuse to dismount while Char.app holds the SQLite handle.
  # We could ``-force`` here but the corruption risk on ``app.db``
  # is too high to justify the convenience. Surface the situation
  # clearly so the operator can quit Char and re-run.
  if char_running; then
    printf "%s⚠  vault left mounted: Char.app is still running%s\n" \
           "$c_yellow" "$c_reset" >&2
    printf "    Quit Char.app (Cmd-Q -- not just close window) so its\n" >&2
    printf "    SQLite handle releases, then run %s./run.sh vault lock%s\n" \
           "$c_bold" "$c_reset" >&2
    printf "    to dismount safely. Forced detach would risk corrupting\n" >&2
    printf "    ~/Library/Application Support/hyprnote/app.db.\n" >&2
    return 0
  fi

  # Polite detach only — ``--polite`` opts the vault_unlock CLI out
  # of its ``-force`` fallback so we never truncate a live SQLite.
  # The interactive ``./run.sh vault lock`` path keeps the force
  # behaviour; only the lock-on-stop hook is paranoid like this.
  if "$VENV_PY" -m local_scribe.security.vault_unlock lock --polite >/dev/null 2>&1; then
    printf "  %svault dismounted%s — Char data is now ciphertext-at-rest\n" \
           "$c_green" "$c_reset"
    return 0
  fi
  printf "%s⚠  polite detach failed; vault left mounted%s\n" \
         "$c_yellow" "$c_reset" >&2
  printf "    A process still has an open handle into the volume.\n" >&2
  printf "    Find it with: %slsof +D \"\$HOME/Library/Application Support/local_scribe-vault\"%s\n" \
         "$c_bold" "$c_reset" >&2
  printf "    Then run: %s./run.sh vault lock%s   (uses -force as a fallback).\n" \
         "$c_bold" "$c_reset" >&2
  return 0   # don't fail cmd_stop overall -- services are already down
}

# Layer B — Char-binary verification gate. Verifies codesign +
# Gatekeeper + linked-Mach-O hashes against the recorded baseline.
# Honours ``LOCAL_SCRIBE_ALLOW_DIRTY_CHAR=1`` as an override.
#
# When the baseline is absent (first-time install), the gate fails
# with a "no recorded Char baseline" drift -- ``cmd_bootstrap`` is
# expected to call ``./run.sh char baseline-set`` once after a clean
# ``install-char``, but we also short-circuit here to print a hint
# instead of bailing if the bundle is otherwise valid.
char_integrity_gate() {
  if [[ "${LOCAL_SCRIBE_SKIP_INTEGRITY:-0}" != "0" ]]; then
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    return 0   # bootstrap path, venv not built yet
  fi
  # If Char.app isn't installed yet, skip — the operator hasn't
  # got past the install step and the script-integrity gate will
  # already have stopped us if our own code is tampered.
  if [[ ! -d "$CHAR_APP" ]]; then
    return 0
  fi
  if "$VENV_PY" -m local_scribe.char.char_integrity --check >&2; then
    return 0
  fi
  if [[ "${LOCAL_SCRIBE_ALLOW_DIRTY_CHAR:-0}" != "0" ]]; then
    "$VENV_PY" -c "
import sys
from local_scribe.char import char_integrity as ci
rep = ci.verify(require_baseline=False)
sys.stderr.write(ci.format_override_warning(rep, color=sys.stderr.isatty()) + '\n')
" >&2 || true
    return 0
  fi
  say "${c_red}refusing to launch a tampered or unverified Char.app${c_reset}"
  say "  see the banner above for next steps"
  say "  (override: ${c_bold}LOCAL_SCRIBE_ALLOW_DIRTY_CHAR=1${c_reset})"
  return 1
}

# Layer C — launch-session gate. Writes a per-launch lock file the
# ASR + inspector services read on every request; closes/removes
# the file on shutdown so a Char bearer carrying ``.ls<short_id>``
# stops working the moment ``./run.sh start`` exits.
launch_session_mint() {
  if [[ ! -x "$VENV_PY" ]]; then
    return 1
  fi
  local launch_id
  if ! launch_id="$("$VENV_PY" -m local_scribe.common.launch_session mint 2>&1)"; then
    say "${c_red}failed to mint launch session: $launch_id${c_reset}"
    return 1
  fi
  export LOCAL_SCRIBE_LAUNCH_ID="$launch_id"
  printf "  launch session : %s%s%s  (services bound to this run.sh)\n" \
         "$c_dim" "${launch_id:0:16}" "$c_reset"
}

launch_session_close() {
  # idempotent: a re-entry from EXIT + INT trap is harmless
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" -m local_scribe.common.launch_session close 2>/dev/null || true
  fi
  unset LOCAL_SCRIBE_LAUNCH_ID
}

# --- preflight: deps + models ---

# Pick a usable system python for venv creation. We prefer 3.14 (matches what
# this repo was developed against), then 3.12, then whatever `python3` is.
pick_python() {
  if [[ -n "${PYTHON:-}" ]] && command -v "$PYTHON" >/dev/null 2>&1; then
    echo "$PYTHON"; return 0
  fi
  for cand in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

ensure_venv() {
  if [[ -x "$VENV_PY" ]]; then
    return 0
  fi
  local sys_py
  if ! sys_py="$(pick_python)"; then
    say "${c_red}python3 not found${c_reset}"
    say "  install Python 3.12+ from https://www.python.org/ or 'brew install python@3.14'"
    return 1
  fi
  say "creating venv at $VENV_DIR using $sys_py ..."
  "$sys_py" -m venv "$VENV_DIR"
  "$VENV_PY" -m pip install --upgrade pip wheel >/dev/null
  rm -f "$DEPS_STAMP"
}

ensure_pip_deps() {
  ensure_venv || return 1
  local req="$REPO/requirements.txt"
  if [[ ! -f "$req" ]]; then
    say "${c_red}requirements.txt missing${c_reset}"
    return 1
  fi
  if [[ -f "$DEPS_STAMP" && "$DEPS_STAMP" -nt "$req" ]]; then
    return 0  # deps stamp newer than requirements -> nothing to do
  fi
  say "installing/updating Python deps from requirements.txt ..."
  if ! "$VENV_PY" -m pip install -q -r "$req"; then
    say "${c_red}pip install failed${c_reset}"
    return 1
  fi
  touch "$DEPS_STAMP"
  say "${c_green}python deps ready${c_reset}"
}

# Compile the Touch ID / Keychain bridge if it's missing or older than
# the .swift source. The compiled binary lives at bin/touchid-keychain
# and is gitignored; we recompile on every source change so updates to
# the Swift CLI (e.g. the --account flag for the Option C split-key
# storage) take effect without a manual swiftc step. swiftc ships with
# Xcode CLT; if it's not on PATH we leave a clear "install xcode-select"
# hint -- this whole stack assumes Xcode CLT for compiled native helpers.
ensure_touchid_helper() {
  local src="$REPO/bin/touchid_keychain.swift"
  local out="$REPO/bin/touchid-keychain"
  if [[ ! -f "$src" ]]; then
    say "${c_yellow}Touch ID helper source missing at $src${c_reset}"
    return 0  # not fatal; the rest of the pipeline runs without it
  fi
  if [[ -x "$out" && "$out" -nt "$src" ]]; then
    return 0  # up-to-date
  fi
  if ! command -v swiftc >/dev/null 2>&1; then
    say "${c_yellow}swiftc not found — install Xcode Command Line Tools:${c_reset}"
    say "  xcode-select --install"
    return 1
  fi
  say "compiling Touch ID helper (bin/touchid-keychain) ..."
  if ! swiftc -O -o "$out" "$src" 2>&1; then
    say "${c_red}swiftc failed; the Keychain-backed master key won't work${c_reset}"
    return 1
  fi
  chmod 755 "$out"
  say "${c_green}Touch ID helper compiled${c_reset}"
}

# Install the three external CLI tools the Option C key flow depends on:
#
#   * age                 (https://age-encryption.org) — the file-encryption
#                         primitive yk_half.age and disaster_recovery.age
#                         are built on top of.
#   * age-plugin-yubikey  — age plugin that lets the YubiKey decrypt
#                         (no decryption is possible without it; the user
#                         can't `cp` yk_half.age off the laptop and recover
#                         later from a stock age binary).
#   * ykman               — Yubico's CLI; we shell out to it to detect
#                         insertion, query slot occupancy, and list serials.
#
# All three are Homebrew-installable, so the most-secure default install
# path is: probe, install whichever is missing, fail loud if Homebrew
# itself is missing (`brew.sh` is the only macOS-supported answer; we'd
# rather refuse to bootstrap than silently downgrade security).
# Minimum age version that supports the plugin recipient system
# (``age1<plugin_name>1...``). Plugins shipped in age v1.1.0 (Feb 2023);
# age v1.0.0 — still in some Homebrew caches as recently as 2026 — emits
# ``malformed recipient ... invalid type "age1yubikey"`` when handed a
# YubiKey-wrapped recipient. We pin the floor here so bootstrap auto-
# upgrades stale installs instead of failing opaquely at stage 3.
#
# Bumping this constant: any age release that adds plugin protocol or
# security fixes we depend on can move this floor up. The current floor
# is the minimum that supports the FiloSottile/age-plugin-yubikey 0.5.x
# recipient format we ship with.
AGE_MIN_VERSION="1.1.0"

# Compare two dotted version strings as integer triples.
# Returns 0 if "$1" is less than "$2", else returns 1.
# Empty / non-numeric components default to 0 so partial versions like
# ``1.2`` are compared as ``1.2.0``.
_version_lt() {
  local a="$1" b="$2"
  local IFS=.
  read -r a1 a2 a3 <<<"$a"
  read -r b1 b2 b3 <<<"$b"
  a1=${a1:-0}; a2=${a2:-0}; a3=${a3:-0}
  b1=${b1:-0}; b2=${b2:-0}; b3=${b3:-0}
  if   (( a1 < b1 )); then return 0
  elif (( a1 > b1 )); then return 1
  elif (( a2 < b2 )); then return 0
  elif (( a2 > b2 )); then return 1
  elif (( a3 < b3 )); then return 0
  else                     return 1
  fi
}

# Print the installed age's version string (e.g. ``1.2.0``) by parsing
# ``age --version`` output. Returns empty string if age is missing or
# the output doesn't look like a version we recognise.
#
# Real outputs we've observed:
#   age v1.0.0
#   v1.0.0
#   1.2.0
#   age 1.3.1
_age_installed_version() {
  command -v age >/dev/null 2>&1 || { echo ""; return; }
  local raw
  raw="$(age --version 2>/dev/null | head -n1)" || { echo ""; return; }
  # Strip ``age `` prefix and a leading ``v``. ``BASH_REMATCH`` is the
  # cleanest portable way to extract the first dotted triple.
  if [[ "$raw" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?) ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo ""
  fi
}

ensure_age_tools() {
  # Stage-2 of bootstrap. We need ``age``, ``age-plugin-yubikey``, and
  # ``ykman`` not just on PATH but actually able to RUN — and ``age``
  # must be ≥ AGE_MIN_VERSION so it recognises plugin-recipient prefixes
  # like ``age1yubikey1...`` (production failures: zero-byte ykman libexec
  # python on 2026-05-11 and a stale age v1.0.0 on the same day).
  local need=()
  _tool_works() {
    # Returns 0 if the binary is on PATH AND executes successfully when
    # asked for its version. Anything else (missing, non-zero exit,
    # ``exec format error``) → return non-zero so we trigger a
    # reinstall.
    local name="$1"
    command -v "$name" >/dev/null 2>&1 || return 1
    "$name" --version >/dev/null 2>&1
  }
  _tool_works age                || need+=("age")
  _tool_works age-plugin-yubikey || need+=("age-plugin-yubikey")
  _tool_works ykman              || need+=("ykman")
  unset -f _tool_works

  # Version-floor check for age. Even when ``age --version`` exits 0,
  # an installation predating plugin support (v1.0.0, ~2022) is
  # functionally broken for our use. We model "too-old age" as a
  # separate state from "missing age" / "broken age": the brew action
  # is ``upgrade`` (formula installed, just stale), not ``install`` or
  # ``reinstall``.
  local age_too_old=0
  local age_have=""
  local age_already_in_need=0
  if [[ ${#need[@]} -gt 0 ]]; then
    local _n
    for _n in "${need[@]}"; do
      [[ "$_n" == "age" ]] && age_already_in_need=1 && break
    done
  fi
  if (( age_already_in_need == 0 )); then
    age_have="$(_age_installed_version)"
    if [[ -z "$age_have" ]]; then
      # ``age --version`` succeeded but we couldn't parse a triple.
      # Treat as missing so the operator gets a clear path.
      need+=("age")
    elif _version_lt "$age_have" "$AGE_MIN_VERSION"; then
      age_too_old=1
    fi
  fi

  if [[ ${#need[@]} -eq 0 && $age_too_old -eq 0 ]]; then
    say "${c_green}age ($age_have), age-plugin-yubikey, ykman present + executable${c_reset}"
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    local _missing="${need[*]+${need[*]}}"
    say "${c_red}Homebrew not installed — needed to install/upgrade: ${_missing}${age_too_old:+ age}${c_reset}"
    say "  install Homebrew (https://brew.sh) and re-run bootstrap"
    return 1
  fi

  # If the binary EXISTS on PATH but didn't execute, ``brew install`` is
  # a no-op (Homebrew thinks it's installed). We have to use
  # ``brew reinstall`` to repair the install. Partition into install vs
  # reinstall sets so the operator sees what we're doing and why.
  local to_install=() to_reinstall=()
  if [[ ${#need[@]} -gt 0 ]]; then
    local t
    for t in "${need[@]}"; do
      if command -v "$t" >/dev/null 2>&1; then
        to_reinstall+=("$t")
      else
        to_install+=("$t")
      fi
    done
  fi

  # ``age`` too-old is its own action: ``brew upgrade age``. The bottle
  # is already installed (so install/reinstall would no-op or rewrite
  # the same old version); only ``upgrade`` actually advances the
  # version pin.
  if [[ $age_too_old -eq 1 ]]; then
    say "${c_yellow}upgrading age: installed $age_have < required $AGE_MIN_VERSION${c_reset}"
    say "  age v1.0.0 predates the plugin-recipient system (v1.1.0+);"
    say "  ``age1yubikey1...`` recipients fail with 'invalid type'"
    if ! brew upgrade age; then
      say "${c_red}brew upgrade age failed${c_reset}"
      say "  try: ${c_bold}brew update && brew upgrade age${c_reset}"
      return 1
    fi
  fi

  if [[ ${#to_reinstall[@]} -gt 0 ]]; then
    say "${c_yellow}reinstalling broken key-management tools via Homebrew: ${to_reinstall[*]}${c_reset}"
    say "  (binary is on PATH but ``<tool> --version`` failed — usually a stale or"
    say "   architecture-mismatched install)"
    if ! brew reinstall "${to_reinstall[@]}"; then
      say "${c_red}brew reinstall ${to_reinstall[*]} failed${c_reset}"
      say "  inspect manually with: ${c_bold}brew doctor${c_reset}"
      return 1
    fi
  fi
  if [[ ${#to_install[@]} -gt 0 ]]; then
    say "installing missing key-management tools via Homebrew: ${to_install[*]}"
    if ! brew install "${to_install[@]}"; then
      say "${c_red}brew install ${to_install[*]} failed${c_reset}"
      say "  fix the brew error (or install the listed formulae manually) and re-run"
      return 1
    fi
  fi

  # Re-verify after install/reinstall/upgrade. If something still
  # doesn't run we must NOT silently continue — the operator will hit a
  # much more confusing error in stage 3.
  for t in age age-plugin-yubikey ykman; do
    if ! command -v "$t" >/dev/null 2>&1 || ! "$t" --version >/dev/null 2>&1; then
      say "${c_red}$t still not working after install/reinstall${c_reset}"
      say "  diagnose with: ${c_bold}$t --version${c_reset}"
      return 1
    fi
  done
  # Re-verify the age version floor too.
  age_have="$(_age_installed_version)"
  if [[ -n "$age_have" ]] && _version_lt "$age_have" "$AGE_MIN_VERSION"; then
    say "${c_red}age still at $age_have after upgrade (need ≥ $AGE_MIN_VERSION)${c_reset}"
    say "  check ${c_bold}brew info age${c_reset} — your tap may pin an old version"
    return 1
  fi
  local action_summary="${need[*]+${need[*]}}"
  [[ $age_too_old -eq 1 ]] && action_summary="${action_summary:+$action_summary, }age (upgraded)"
  say "${c_green}installed/repaired: $action_summary${c_reset}"
  return 0
}

# Pre-fetch the parakeet-mlx weights into the HuggingFace cache. parakeet-mlx
# does this on first transcribe() too, but doing it up-front means the first
# /v1/listen request doesn't pay the download tax.
ensure_parakeet_model() {
  local model="$PARAKEET_MODEL"
  if "$VENV_PY" - <<PY >/dev/null 2>&1
from huggingface_hub import snapshot_download
snapshot_download(repo_id="$model", local_files_only=True)
PY
  then
    say "${c_green}parakeet model present: $model${c_reset}"
    return 0
  fi
  say "downloading parakeet model $model (one-time, ~1.2GB) ..."
  if ! "$VENV_PY" - <<PY
from huggingface_hub import snapshot_download
snapshot_download(repo_id="$model")
PY
  then
    say "${c_red}failed to download parakeet model${c_reset}"
    return 1
  fi
  say "${c_green}parakeet model ready${c_reset}"
}

# Pre-fetch faster-whisper weights. Only relevant when ASR_BACKEND=whisper;
# otherwise we skip silently. faster-whisper auto-downloads on first use, but
# this gives the user an explicit progress indication.
ensure_whisper_model() {
  local model="$WHISPER_MODEL"
  if "$VENV_PY" - <<PY >/dev/null 2>&1
import os
from faster_whisper.utils import download_model
download_model("$model", local_files_only=True)
PY
  then
    say "${c_green}faster-whisper model present: $model${c_reset}"
    return 0
  fi
  say "downloading faster-whisper model $model ..."
  if ! "$VENV_PY" - <<PY
from faster_whisper.utils import download_model
download_model("$model")
PY
  then
    say "${c_red}failed to download faster-whisper model${c_reset}"
    return 1
  fi
  say "${c_green}faster-whisper model ready${c_reset}"
}

# Pre-fetch the sherpa-onnx pyannote segmentation + NeMo TitaNet embedding
# models that diarization needs. diarization_backend.ensure_models() handles
# the actual download; we just call it.
ensure_diarization_models() {
  if "$VENV_PY" - <<'PY' 2>/dev/null
import sys
from local_scribe.asr.backends.diarization_backend import ensure_models
ensure_models()
PY
  then
    say "${c_green}diarization models ready${c_reset}"
    return 0
  fi
  say "${c_yellow}diarization models couldn't be prefetched${c_reset}"
  say "  they will auto-download on the first --diarize run instead"
}

# Seed ~/.config/local_scribe/config.json from the baked-in DEFAULT_CONFIG
# if missing. Idempotent: re-running bootstrap on an existing install
# leaves an edited config alone.
ensure_config_json() {
  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing; can't seed config.json${c_reset}"
    return 1
  fi
  "$VENV_PY" - <<'PY'
import sys
from local_scribe.common.config import DEFAULT_CONFIG_PATH, write_default_config_if_missing
created = write_default_config_if_missing()
if created:
    print(f"\033[32mwrote default config\033[0m  {created}")
else:
    print(f"\033[2mconfig.json already exists\033[0m  {DEFAULT_CONFIG_PATH}")
PY
}

# Friendly check for the LM Studio CLI. Not strictly required (you can run
# LM Studio.app's local server manually) but the auto-load convenience needs it.
ensure_lms_cli() {
  if lms_path >/dev/null; then
    return 0
  fi
  say "${c_yellow}lms CLI not found${c_reset}"
  say "  bootstrap will install LM Studio.app + the lms CLI for you;"
  say "  or run \`./run.sh bootstrap\` to do it now"
  return 0  # not fatal — bootstrap step (4/5) handles full install
}

# Total system RAM in whole GB. macOS-only; falls back to 0 on failure.
machine_ram_gb() {
  local b
  b="$(sysctl -n hw.memsize 2>/dev/null)" || { echo 0; return; }
  echo $(( b / 1024 / 1024 / 1024 ))
}

# Top-level preflight. Returns non-zero if anything *required* is broken.
preflight() {
  local rc=0
  printf "%spreflight%s\n" "$c_bold" "$c_reset"

  ensure_pip_deps             || rc=1

  case "$ASR_BACKEND_DEFAULT" in
    parakeet) ensure_parakeet_model || rc=1 ;;
    whisper)  ensure_whisper_model  || rc=1 ;;
    *) say "${c_red}unknown ASR_BACKEND='$ASR_BACKEND_DEFAULT'${c_reset}"; rc=1 ;;
  esac

  ensure_diarization_models   || true   # best-effort, will auto-download later

  ensure_lms_cli              || true

  return $rc
}

cmd_doctor() {
  printf "%sdoctor%s — validating local pipeline\n\n" "$c_bold" "$c_reset"

  # Dev mode banner. If LOCAL_SCRIBE_DEV_MODE is set, surface it
  # at the *top* of doctor output so an operator chasing a
  # weird-symptom bug sees the bypass before they get to the SIP
  # section. The block is intentionally noisy and references both
  # the env var name and the canonical doc anchor.
  local _devmode="${LOCAL_SCRIBE_DEV_MODE:-}"
  local _devmode_lower
  _devmode_lower="$(printf '%s' "$_devmode" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "$_devmode" && "$_devmode_lower" != "0" && "$_devmode_lower" != "false" \
        && "$_devmode_lower" != "no" && "$_devmode_lower" != "off" ]]; then
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
           "$c_red" "$c_reset"
    printf "%s%s[DEV MODE ACTIVE] LOCAL_SCRIBE_DEV_MODE=%s%s\n" \
           "$c_red" "$c_bold" "$_devmode" "$c_reset"
    printf "  SIP gates are bypassed for this shell. This is NOT a\n"
    printf "  production configuration. To exit dev mode:\n"
    printf "    1. ./run.sh stop\n"
    printf "    2. unset LOCAL_SCRIBE_DEV_MODE\n"
    printf "    3. ./run.sh start\n"
    printf "  See SECURITY.md § 'Dev mode' for what this costs you.\n"
    printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n\n" \
           "$c_red" "$c_reset"
  fi

  # SIP first — every other check is meaningless if the kernel
  # can't enforce process boundaries. Doctor doesn't refuse to
  # continue (the operator may be using doctor precisely to figure
  # out why things won't start), but it surfaces the failure
  # prominently.
  printf "%sSystem Integrity Protection:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    if "$VENV_PY" -m local_scribe.security.sip_check check 2>&1 | sed 's/^/  /'; then
      printf "  %s● SIP fully enabled — every key-touching command is allowed%s\n" \
             "$c_green" "$c_reset"
    else
      printf "\n  %s○ key-touching commands (start / bootstrap / key * /%s\n" \
             "$c_red" "$c_reset"
      printf "  %s   configure-char / redo-session) are BLOCKED until SIP is on%s\n" \
             "$c_red" "$c_reset"
    fi
  else
    if csrutil status 2>/dev/null | grep -q "status: enabled\\.$"; then
      printf "  %s● SIP enabled (parsed via csrutil; venv not built yet)%s\n" \
             "$c_green" "$c_reset"
    else
      printf "  %s○ SIP NOT fully enabled — fix before bootstrap%s\n" \
             "$c_red" "$c_reset"
      csrutil status 2>&1 | sed 's/^/    /'
    fi
  fi

  printf "\n%sscript integrity:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    if "$VENV_PY" -m local_scribe.security.script_integrity --banner 2>&1 | sed 's/^/  /'; then
      :
    else
      # Banner already printed by --banner mode; add the override hint
      # if it's currently active so the operator sees it inline.
      if [[ "${LOCAL_SCRIBE_ALLOW_DIRTY:-0}" != "0" ]]; then
        printf "  %s● override active (LOCAL_SCRIBE_ALLOW_DIRTY=1)%s\n" \
               "$c_yellow" "$c_reset"
      else
        printf "  %s○ run\`./run.sh start\` is BLOCKED until you fix the drift above%s\n" \
               "$c_red" "$c_reset"
      fi
    fi
  else
    printf "  (skip - venv missing)\n"
  fi

  printf "\n%sconfig.json:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import sys
from local_scribe.common.config import DEFAULT_CONFIG_PATH, load_config, validate, to_dict
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
exists = DEFAULT_CONFIG_PATH.is_file()
mark = (G + "\u25cf") if exists else (Y + "\u25cb")
print(f"  {mark}{Z} {DEFAULT_CONFIG_PATH}"
      + ("" if exists else "  (will be seeded on next bootstrap)"))
cfg = load_config()
errs = validate(to_dict(cfg))
if errs:
    for e in errs: print(f"  {R}\u25cb{Z} {e}")
else:
    print(f"  {G}\u25cf{Z} schema OK   asr={cfg.asr_backend}  llm={cfg.llm_host}:{cfg.llm_port}  inspector={cfg.inspector_bind}:{cfg.inspector_port}")
PY
  else
    printf "  (skip - venv missing)\n"
  fi

  printf "\n%spython:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    printf "  "; ok; printf "venv at %s (%s)\n" "$VENV_DIR" "$($VENV_PY --version)"
  else
    printf "  "; bad; printf "venv missing at %s\n" "$VENV_DIR"
  fi

  printf "\n%spython packages:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<PY
import importlib, sys
G,R,Z = ("\033[32m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","")
mods = ["fastapi","uvicorn","requests","numpy","soundfile","librosa",
        "parakeet_mlx","faster_whisper","sherpa_onnx","huggingface_hub"]
for name in mods:
    try:
        m = importlib.import_module(name)
        v = getattr(m, "__version__", "ok")
        print(f"  {G}\u25cf{Z} {name:18s} {v}")
    except Exception as e:
        print(f"  {R}\u25cb{Z} {name:18s} MISSING ({type(e).__name__})")
PY
  else
    printf "  (skip - venv missing)\n"
  fi

  printf "\n%smodels:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<PY
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
G,Y,Z = ("\033[32m","\033[33m","\033[0m") if sys.stdout.isatty() else ("","","")
def check(repo, label):
    try:
        p = snapshot_download(repo_id=repo, local_files_only=True)
        print(f"  {G}\u25cf{Z} {label:30s} cached at {p}")
    except Exception:
        print(f"  {Y}\u25cb{Z} {label:30s} not yet downloaded")
check("$PARAKEET_MODEL",      "parakeet ($ASR_BACKEND_DEFAULT default)")
diar = Path.home() / ".cache" / "local_scribe" / "diarization"
seg  = diar / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
emb  = diar / "nemo_en_titanet_small.onnx"
mark = lambda b: f"{G}\u25cf{Z}" if b else f"{Y}\u25cb{Z}"
print(f"  {mark(seg.exists())} pyannote segmentation          {seg}")
print(f"  {mark(emb.exists())} NeMo TitaNet embedding         {emb}")
PY
  fi

  printf "\n%sservices:%s\n" "$c_bold" "$c_reset"
  if curl -sf "http://127.0.0.1:$ASR_PORT/health" -o /dev/null 2>&1; then
    printf "  "; ok; printf "ASR server   :%s   reachable\n" "$ASR_PORT"
  else
    printf "  "; warn; printf "ASR server   :%s   not running (start with ./run.sh start)\n" "$ASR_PORT"
  fi
  if lmstudio_running; then
    printf "  "; ok; printf "LM Studio    :%s   reachable\n" "$LMSTUDIO_PORT"
    if lmstudio_model_loaded "$LLM_MODEL"; then
      printf "  "; ok; printf "%s loaded\n" "$LLM_MODEL"
    else
      printf "  "; warn; printf "%s NOT loaded (will auto-load on start)\n" "$LLM_MODEL"
    fi
  else
    printf "  "; bad; printf "LM Studio    :%s   not running\n" "$LMSTUDIO_PORT"
    if lms_path >/dev/null 2>&1; then
      printf "         (./run.sh start will auto-start it)\n"
    elif lmstudio_app_installed; then
      printf "         LM Studio.app present, lms CLI not bootstrapped — run \`./run.sh bootstrap\`\n"
    else
      printf "         not installed — run \`./run.sh bootstrap\` to install via brew\n"
    fi
  fi

  printf "\n%schar.app:%s\n" "$c_bold" "$c_reset"
  if char_installed; then
    local v; v="$(char_installed_version)"
    if [[ "$v" == "$CHAR_KNOWN_GOOD_VERSION" ]]; then
      printf "  "; ok; printf "Char %s installed (matches pinned)\n" "$v"
    elif [[ -z "$v" ]]; then
      printf "  "; warn; printf "Char installed but version unreadable (pinned: %s)\n" "$CHAR_KNOWN_GOOD_VERSION"
    else
      printf "  "; warn; printf "Char %s installed; %s pinned -- run \`./run.sh install-char\` to align\n" \
             "$v" "$CHAR_KNOWN_GOOD_VERSION"
    fi
    if [[ -f "$CHAR_SETTINGS" ]]; then
      "$VENV_PY" - "$CHAR_SETTINGS" "$ASR_PORT" <<'PY'
import json, os, sys, pathlib
from local_scribe.security import service_auth

expected_port = sys.argv[2]
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
d = json.loads(pathlib.Path(sys.argv[1]).read_text())
ai  = d.get("ai") or {}
oai = ((ai.get("stt") or {}).get("openai")) or {}
prov  = ai.get("current_stt_provider", "")
model = ai.get("current_stt_model", "")
url   = oai.get("base_url", "")
api_key = oai.get("api_key", "")
expected_url = f"http://127.0.0.1:{expected_port}/v1"

# Auth drift: Char's stored api_key has to match the current ASR
# token (HKDF-derived from the Keychain master key) or every Generate
# click returns 401. Compute the expected token *without* prompting
# Touch ID -- we use the test-env shortcut if it's set, or skip the
# auth check entirely if neither side is available.
expected_token = None
if not service_auth.is_bypass_enabled():
    mk_hex = (os.environ.get("LOCAL_SCRIBE_MASTER_KEY_HEX")
              or os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX"))
    if mk_hex:
        try:
            expected_token = service_auth.derive_service_token(
                bytes.fromhex(mk_hex.strip()), "asr",
            )
        except Exception:  # noqa: BLE001
            expected_token = None
    # We DON'T fetch the token via Touch ID here -- doctor is a
    # non-interactive check. If neither env var is set we just skip
    # the api_key drift assertion and tell the user to compare
    # fingerprints via `./run.sh status`.

if prov == "openai" and model == "gpt-4o-transcribe" and url == expected_url:
    print(f"  {G}\u25cf{Z} Char transcriber configured for this server")
    if expected_token is not None:
        if api_key == expected_token:
            print(f"  {G}\u25cf{Z} Char api_key matches current ASR token "
                  f"(fp={service_auth.token_fingerprint(expected_token)})")
        else:
            print(f"  {Y}\u25cb{Z} Char api_key DRIFT -- saved key doesn't match "
                  f"the current ASR token (fp={service_auth.token_fingerprint(expected_token)})")
            print(f"      run `./run.sh configure-char` to rewrite Char's settings.json")
    elif api_key and not service_auth.is_bypass_enabled():
        # Best-effort sanity check: at least the api_key should LOOK
        # like one of our tokens (ls_asr_<hex>) rather than a real
        # OpenAI key. A real OpenAI key here is a privacy red flag.
        if not api_key.startswith("ls_asr_") and api_key != "local-auth-bypassed":
            print(f"  {Y}\u25cb{Z} Char api_key is set but doesn't look like our "
                  f"derived token (got {api_key[:8]!r}...) -- did configure-char run?")
        else:
            print(f"  {G}\u25cf{Z} Char api_key has the right shape "
                  f"(verify via `./run.sh status` fingerprint)")
else:
    print(f"  {Y}\u25cb{Z} Char transcriber NOT pointed here -- run `./run.sh configure-char`")
    print(f"      provider : {prov!r:<25s} (want 'openai')")
    print(f"      model    : {model!r:<25s} (want 'gpt-4o-transcribe')")
    print(f"      base_url : {url!r:<25s} (want '{expected_url}')")
PY
    else
      printf "  "; warn; printf "Char settings.json missing -- open Char once, then \`./run.sh configure-char\`\n"
    fi
    # Char's PostHog analytics toggle (CHAR_REVIEW.md). Sentry has no toggle.
    if [[ -f "$CHAR_STORE" ]]; then
      "$VENV_PY" - "$CHAR_STORE" <<'PY'
import json, sys, pathlib
G,Y,Z = ("\033[32m","\033[33m","\033[0m") if sys.stdout.isatty() else ("","","")
try:
    d = json.loads(pathlib.Path(sys.argv[1]).read_text() or "{}")
except json.JSONDecodeError:
    d = {}
raw = d.get("analytics") or "{}"
try:
    inner = json.loads(raw) if isinstance(raw, str) else dict(raw)
except json.JSONDecodeError:
    inner = {}
if inner.get("Disabled") is True:
    print(f"  {G}\u25cf{Z} Char PostHog analytics disabled in store.json")
else:
    print(f"  {Y}\u25cb{Z} Char PostHog analytics ENABLED -- run `./run.sh configure-char` to disable")
PY
    fi
  else
    printf "  "; bad; printf "Char.app NOT installed (run \`./run.sh install-char\` to fetch v%s)\n" "$CHAR_KNOWN_GOOD_VERSION"
  fi

  printf "\n%sinspector:%s\n" "$c_bold" "$c_reset"
  if inspector_pid >/dev/null; then
    printf "  "; ok; printf "running at http://%s:%s/   (pid %s)\n" \
                          "$INSPECTOR_BIND" "$INSPECTOR_PORT" "$(inspector_pid)"
    printf "                   open with: %s./run.sh inspector open%s\n" "$c_bold" "$c_reset"
  else
    printf "  "; warn; printf "not running -- start with %s./run.sh inspector start%s (or %s./run.sh start%s)\n" \
                            "$c_bold" "$c_reset" "$c_bold" "$c_reset"
  fi

  printf "\n%soutbound firewall (per-Char proxy + sandbox):%s\n" "$c_bold" "$c_reset"
  # Show:
  #   1. Egress proxy state (process-mode enforcement)
  #   2. Sandbox profile state (process-mode containment)
  #   3. System-hosts coverage if it's also installed (opt-in mode)
  if egress_proxy_pid >/dev/null; then
    printf "  "; ok; printf "egress proxy running on :%s (pid %s)\n" \
                           "$EGRESS_PROXY_PORT" "$(egress_proxy_pid)"
  else
    printf "  "; warn; printf "egress proxy NOT running — Char would not be filtered\n"
    printf "      start it with: %s./run.sh proxy start%s (or %s./run.sh start%s)\n" \
           "$c_bold" "$c_reset" "$c_bold" "$c_reset"
  fi
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import sys, json
from local_scribe.egress import char_sandbox
from local_scribe.egress import firewall
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
# --- sandbox profile ---
p = char_sandbox.profile_path()
if not char_sandbox.is_available():
    print(f"  {Y}\u25cb{Z} sandbox-exec not available — Char's containment layer is OFF")
elif not p.is_file():
    print(f"  {Y}\u25cb{Z} sandbox profile not yet written at {p}")
    print(f"      first ./run.sh char launch will create it")
else:
    ok, msg = char_sandbox.validate_profile(p)
    if ok:
        print(f"  {G}\u25cf{Z} sandbox profile valid ({p})")
    else:
        print(f"  {R}\u25cb{Z} sandbox profile present but invalid: {msg}")
# --- system-hosts mode (opt-in) ---
try:
    s = firewall.status()
except Exception as exc:  # noqa: BLE001
    print(f"  {R}\u25cb{Z} system-hosts status check failed: {exc}")
    sys.exit(0)
if s.installed:
    print(f"  {G}\u25cf{Z} system-hosts block ALSO installed "
          f"({len(s.blocked_hostnames)} hostnames blackholed; affects ALL apps)")
    for cat, c in s.coverage_by_category.items():
        if c["blocked"] == c["expected"]:
            print(f"      {cat:11s} {c['blocked']}/{c['expected']}  (full coverage)")
        else:
            print(f"      {Y}{cat:11s}{Z} {c['blocked']}/{c['expected']}  "
                  f"(drift; re-run ./run.sh firewall enable --mode system)")
PY
  fi

  printf "\n%schar binary integrity:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import sys
from local_scribe.char import char_integrity as ci
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
rep = ci.verify()
fp = rep.fingerprint
if fp is None:
    if rep.note:
        print(f"  {Y}\u25cb{Z} {rep.note}")
    else:
        for d in rep.drifts:
            print(f"  {R}\u25cb{Z} {d.kind:8s}  {d.message}")
    sys.exit(0)
if rep.clean:
    print(f"  {G}\u25cf{Z} signed by team {fp.team_id} (Fastrepl, Inc.), bundle {fp.bundle_id}")
    print(f"      CDHash    : {fp.cdhash_sha256_full[:16]}\u2026")
    print(f"      Gatekeeper: {fp.spctl_source or '-'}")
    print(f"      tracked   : {len(fp.mach_os)} Mach-O binar{'y' if len(fp.mach_os)==1 else 'ies'} (sha256 + linked-framework allow-list)")
else:
    print(f"  {R}\u25cb{Z} Char integrity FAILED ({len(rep.drifts)} drift item(s))")
    for d in rep.drifts[:6]:
        print(f"      {d.kind:8s}  {d.message[:80]}")
    if any(d.kind == "cdhash" and "no recorded" in d.message for d in rep.drifts):
        print(f"      \u2192 first-time use: run `./run.sh char baseline-set`")
    else:
        print(f"      \u2192 run `./run.sh char status` for full detail")
PY
  fi

  printf "\n%slaunch session:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import sys, json
from local_scribe.common import launch_session as L
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
s = L.status()
if s.get("gate_disabled"):
    print(f"  {Y}\u25cb{Z} gate disabled (LOCAL_SCRIBE_DISABLE_LAUNCH_GATE=1)")
elif s.get("lock_present"):
    sess = s["session"]
    print(f"  {G}\u25cf{Z} active launch session  short_id={sess['short_id']}  pid={sess.get('parent_pid')}")
    print(f"      lock path: {s['lock_path']}")
else:
    print(f"  {Y}\u25cb{Z} no active launch lock (run `./run.sh start` to mint one)")
    print(f"      Char\u2019s saved api_key carrying a `.ls\u2026` suffix will be 403'd")
    print(f"      until services are started via run.sh.")
PY
  fi

  printf "\n%sauthentication:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import json, sys
from local_scribe.security import service_auth
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")

if service_auth.is_bypass_enabled():
    print(f"  {Y}\u25cb{Z} AUTH BYPASS ENABLED via LOCAL_SCRIBE_DISABLE_AUTH=1")
    print(f"      every /api/* and /v1/* endpoint is OPEN -- unset for production")
    sys.exit(0)

try:
    from local_scribe.security.secret_store import has_kc_half, has_master_key, helper_path
    from local_scribe.security import yubikey_backup
except Exception as exc:  # noqa: BLE001
    print(f"  {R}\u25cb{Z} secret_store unimportable: {exc}")
    sys.exit(0)

hp = helper_path()
if not hp.is_file():
    print(f"  {R}\u25cb{Z} Touch ID helper missing at {hp}")
    print(f"      run `./run.sh bootstrap` to compile bin/touchid_keychain.swift")
    sys.exit(0)
print(f"  {G}\u25cf{Z} Touch ID helper present ({hp})")

# Option C (v2 split-key) state first; legacy v1 only if v2 is absent.
try:
    v2 = has_kc_half()
    v1 = has_master_key()
    yk = yubikey_backup.has_yk_half()
except Exception as exc:  # noqa: BLE001
    print(f"  {R}\u25cb{Z} cannot probe key state: {exc}")
    sys.exit(0)

if v2:
    print(f"  {G}\u25cf{Z} kc_half present in Keychain (Option C, Touch ID-gated)")
    if yk:
        print(f"  {G}\u25cf{Z} yk_half.age present at {yubikey_backup.YK_HALF_PATH}")
        print(f"      unlock requires BOTH halves: Touch ID + YubiKey tap")
    else:
        print(f"  {R}\u25cb{Z} yk_half.age MISSING — the split-key is unrecoverable.")
        print(f"      run `./run.sh key dr-restore` or `./run.sh yubikey restore <snap>`")
elif v1:
    print(f"  {Y}\u25cb{Z} legacy v1 master key present (single Keychain item)")
    print(f"      will auto-migrate to Option C on next `./run.sh key unlock`")
else:
    print(f"  {Y}\u25cb{Z} no master key on this machine")
    print(f"      `./run.sh start` will REFUSE TO LAUNCH until you run:")
    print(f"        ./run.sh key init       (first-time enrollment)")
    print(f"        ./run.sh key dr-restore (if you have a DR passphrase + file)")
    sys.exit(0)
print(f"      tokens are HKDF-derived per service; run `./run.sh status` for fingerprints.")
PY
  fi

  printf "\n%sencrypted vault:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import json, sys
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stdout.isatty() else ("","","","")
try:
    from local_scribe.security import vault
    from local_scribe.security import vault_unlock
except Exception as exc:  # noqa: BLE001
    print(f"  {R}\u25cb{Z} vault module unimportable: {exc}")
    sys.exit(0)

s = vault.status()
if s["exists"]:
    size_mb = s["bundle_size_bytes"] / (1024 * 1024)
    print(f"  {G}\u25cf{Z} bundle present at {s['bundle_path']} ({size_mb:.1f} MB on disk)")
    if s["mounted"]:
        print(f"  {G}\u25cf{Z} mounted at {s['mount_path']}")
    else:
        print(f"  {Y}\u25cb{Z} not mounted -- run `./run.sh vault unlock` (Touch ID + YubiKey)")
    if s["char_data_relocated"]:
        print(f"  {G}\u25cf{Z} Char data lives INSIDE the vault (encrypted at rest)")
    else:
        print(f"  {Y}\u25cb{Z} Char data NOT relocated -- run `./run.sh vault unlock` to move it in")
else:
    print(f"  {Y}\u25cb{Z} no vault on disk yet")
    print(f"      run `./run.sh vault init` to create one (will derive its")
    print(f"      passphrase from your master key via HKDF -- Touch ID +")
    print(f"      YubiKey tap on every unlock).")
PY
  fi

  printf "\n%schar config hints (manual fallback):%s\n" "$c_bold" "$c_reset"
  printf "  %sprefer:%s %s./run.sh configure-char%s   (sets the right api key automatically)\n" \
         "$c_dim" "$c_reset" "$c_bold" "$c_reset"
  printf "  Live recording  : Custom provider, Base URL http://127.0.0.1:%s\n" "$ASR_PORT"
  printf "  Generate (file) : OpenAI provider, Model gpt-4o-transcribe, Base URL http://127.0.0.1:%s/v1\n" "$ASR_PORT"
  printf "  api key (both)  : the per-service ASR token (run \`./run.sh status\` to see the\n"
  printf "                    fingerprint; configure-char writes it for you)\n"
  printf "  intelligence    : LM Studio @ http://127.0.0.1:%s   model=%s\n" "$LMSTUDIO_PORT" "$LLM_MODEL"
  printf "\n"
}

cmd_setup() {
  printf "%ssetup%s — force reinstall + redownload\n" "$c_bold" "$c_reset"
  rm -f "$DEPS_STAMP"
  ensure_pip_deps             || return 1
  ensure_parakeet_model       || return 1
  ensure_whisper_model        || true   # opt-in backend; don't fail run
  ensure_diarization_models   || true
  printf "\n%sready.%s run %s./run.sh start%s next.\n" "$c_green" "$c_reset" "$c_bold" "$c_reset"
}

cmd_bootstrap() {
  printf "%sbootstrap%s — first-time setup for a fresh clone\n\n" "$c_bold" "$c_reset"

  # SIP MUST be on before we install dependencies, fetch models, or
  # create any Keychain item. Bootstrap creates the master key; doing
  # so on a SIP-disabled host means the key was generated in an
  # environment where it could be exfiltrated before the install
  # completes. Force the operator to fix this first.
  sip_gate || return 1

  printf "%s(1/10) python venv + pip deps + Touch ID helper%s\n" "$c_bold" "$c_reset"
  ensure_pip_deps             || return 1
  ensure_touchid_helper       || true   # best-effort; doctor will flag

  printf "\n%s(2/10) key-management tools (age, age-plugin-yubikey, ykman)%s\n" \
         "$c_bold" "$c_reset"
  # Most-secure default install: refuse to proceed without these. The
  # split-key flow CANNOT run without age + age-plugin-yubikey, and
  # offering a "skip" path would mean shipping users a half-configured
  # install where ./run.sh start works but the keys never got created.
  ensure_age_tools            || return 1

  printf "\n%s(3/10) master key (Option C split-key: Touch ID ⊕ YubiKey)%s\n" \
         "$c_bold" "$c_reset"
  # Idempotent: if a kc_half is already in Keychain (or a legacy v1
  # whole-key item is), skip and let the operator know. Otherwise
  # walk the Option C init flow inline.
  if "$VENV_PY" -c '
import sys
from local_scribe.security import secret_store
sys.exit(0 if (secret_store.has_kc_half() or secret_store.has_master_key()) else 1)
' 2>/dev/null; then
    printf "  %s● master key already present%s — skipping init\n" "$c_green" "$c_reset"
    printf "    inspect: %s./run.sh key status%s\n" "$c_bold" "$c_reset"
    printf "    rotate:  %s./run.sh key rotate%s\n" "$c_bold" "$c_reset"
  else
    printf "  We'll generate a 256-bit master key, split it via XOR, and\n"
    printf "  persist the halves so EITHER half alone is useless:\n"
    printf "    kc_half  →  macOS Keychain (Touch ID-gated)\n"
    printf "    yk_half  →  age-encrypted file on disk (YubiKey-decryptable)\n"
    printf "  Plus an optional passphrase-encrypted disaster-recovery copy.\n\n"
    printf "  Insert your YubiKey now. (If you don't have one, see KEY_SAFETY.md\n"
    printf "  → 'YubiKey-less builds' for the supported degraded path.)\n\n"
    if ask_yn "  Initialise the master key now? (strongly recommended)" y; then
      printf "\n"
      # Drive cmd_key init through its own UX (DR passphrase prompt,
      # YubiKey enrollment). The function calls `exec` only on its
      # final happy paths, so we use a subshell to capture failures
      # without taking down the bootstrap process.
      if ! ( cmd_key key init ); then
        say "${c_red}master key init failed — bootstrap cannot continue${c_reset}"
        say "  Run ${c_bold}./run.sh key init${c_reset} once you've resolved the issue."
        return 1
      fi
    else
      say "${c_red}refusing to continue without a master key${c_reset}"
      say "  The most-secure default install requires it. Re-run bootstrap"
      say "  when you're ready to enroll, or run ${c_bold}./run.sh key init${c_reset} manually."
      return 1
    fi
  fi

  printf "\n%s(4/10) encrypted vault (AES-256 sparse bundle keyed off the master)%s\n" \
         "$c_bold" "$c_reset"
  # The vault is the canonical at-rest container for Char session data
  # and our transcripts. The hdiutil passphrase is HKDF-derived from
  # the master key (see vault_unlock.py), so unlocking the vault
  # requires Touch ID + YubiKey tap. Idempotent.
  #
  # Two-step: CREATE the sparse bundle if it doesn't exist, then
  # MOUNT + RELOCATE Char's data dir into it. The second step is
  # what actually buys encryption-at-rest — without it the bundle
  # sits empty and Char keeps writing plaintext to ~/Library/...
  # The 2026-05-11 audit found 6 unencrypted sessions in that exact
  # state because earlier bootstrap only did step 1.
  local _vault_exists=0
  if "$VENV_PY" -c '
import sys
from local_scribe.security import vault
sys.exit(0 if vault.exists() else 1)
' 2>/dev/null; then
    _vault_exists=1
    printf "  %s● encrypted vault already present%s — skipping create\n" "$c_green" "$c_reset"
  else
    printf "  Creating ~/Library/Application Support/local_scribe-vault.sparsebundle\n"
    printf "  (AES-256 sparse — starts at ~4 MB, grows on demand up to 100 GB).\n"
    printf "  You'll be prompted for Touch ID + a YubiKey tap to derive the\n"
    printf "  hdiutil passphrase.\n\n"
    if ask_yn "  Initialise the encrypted vault now? (strongly recommended)" y; then
      printf "\n"
      if "$VENV_PY" -m local_scribe.security.vault_unlock init; then
        _vault_exists=1
      else
        say "${c_yellow}vault init failed — pipeline can still start, but on-disk data WILL be unencrypted until you fix this${c_reset}"
        say "  Re-run with: ${c_bold}./run.sh vault init${c_reset}"
      fi
    else
      say "${c_yellow}skipped — on-disk transcripts and Char data will be in plaintext${c_reset}"
      say "  Run ${c_bold}./run.sh vault init${c_reset} any time to enable encryption."
    fi
  fi

  # Step 2 of stage 4: mount + relocate. Idempotent at both layers
  # (vault.mount no-ops when already mounted; vault.relocate_char_data
  # no-ops when the symlink is already in place). Skipped iff the
  # bundle isn't on disk (operator declined to create one). The
  # operator's data has to be QUIESCED for this step — Char must not
  # be running, or its open SQLite WAL handle on app.db will corrupt
  # when we mv the file out from under it. We don't have a reliable
  # way to detect Char-is-open here (Char doesn't take a flock), so
  # we ask outright if hyprnote/ already has content, then defer to
  # the operator. First-time bootstraps where hyprnote/ doesn't exist
  # yet sail through without prompting.
  if (( _vault_exists == 1 )); then
    local _char_data="$HOME/Library/Application Support/hyprnote"
    if "$VENV_PY" -c '
import sys
from local_scribe.security import vault
sys.exit(0 if vault.char_data_relocated() else 1)
' 2>/dev/null; then
      printf "  %s● Char data already lives inside the vault%s\n" "$c_green" "$c_reset"
    else
      if [[ -d "$_char_data" && ! -L "$_char_data" ]]; then
        # Existing plaintext data dir. Warn loudly + require explicit ack.
        printf "\n"
        printf "  %s⚠  Char data dir exists at %s%s\n" \
               "$c_yellow" "$_char_data" "$c_reset"
        printf "      and is NOT yet relocated into the vault — current sessions are PLAINTEXT.\n"
        printf "      We can move it in now (the move is atomic + reversible:\n"
        printf "      original is renamed to .pre_vault_backup.<timestamp> until you delete it).\n"
        printf "      %sQuit Char.app first (Cmd-Q) — open SQLite handles would corrupt.%s\n" \
               "$c_red" "$c_reset"
        if ask_yn "  Relocate Char data into the vault now? (Touch ID + YubiKey tap required)" y; then
          if "$VENV_PY" -m local_scribe.security.vault_unlock unlock; then
            printf "  %s● Char data relocated; original backed up next to it%s\n" "$c_green" "$c_reset"
          else
            say "${c_yellow}vault unlock/relocate failed; rerun \`./run.sh vault unlock\` after quitting Char${c_reset}"
          fi
        else
          say "${c_yellow}skipped — \`./run.sh start\` will refuse until you run \`./run.sh vault unlock\`${c_reset}"
        fi
      else
        # First-time setup: no data on disk yet. Mount + pre-create the
        # symlink so Char starts writing INTO the vault from day 1.
        printf "  No Char data on disk yet — mounting vault + pre-symlinking destination\n"
        if "$VENV_PY" -m local_scribe.security.vault_unlock unlock; then
          printf "  %s● vault mounted; %s -> vault%s\n" \
                 "$c_green" "$_char_data" "$c_reset"
        else
          say "${c_yellow}vault unlock failed; rerun \`./run.sh vault unlock\` before \`./run.sh start\`${c_reset}"
        fi
      fi
    fi
  fi

  printf "\n%s(5/10) parakeet ASR weights%s\n" "$c_bold" "$c_reset"
  ensure_parakeet_model       || return 1

  printf "\n%s(6/10) sherpa-onnx diarization models%s\n" "$c_bold" "$c_reset"
  ensure_diarization_models   || true   # best-effort

  printf "\n%s(7/10) ~/.config/local_scribe/config.json%s\n" "$c_bold" "$c_reset"
  ensure_config_json          || true   # best-effort

  printf "\n%s(8/10) LM Studio.app + Qwen LLM%s\n" "$c_bold" "$c_reset"
  if ! lmstudio_full_bootstrap; then
    say "${c_yellow}LM Studio bootstrap incomplete — Char's summary step will fail${c_reset}"
    say "  fix the issues above (or load the model from LM Studio.app), then re-run"
  fi

  printf "\n%s(9/10) Char.app — install + auto-config%s\n" "$c_bold" "$c_reset"
  if ! char_installed; then
    printf "  Char.app not installed at /Applications/Char.app.\n"
    printf "  We can fetch the pinned version (%s) from GitHub Releases:\n" "$CHAR_KNOWN_GOOD_VERSION"
    printf "    %s%s/hyprnote-macos-<arch>.dmg%s\n" \
           "$c_dim" "$CHAR_RELEASE_BASE_URL" "$c_reset"
    if ask_yn "  Download and install Char $CHAR_KNOWN_GOOD_VERSION now?" y; then
      printf "\n"
      if char_install_pinned; then
        # Capture the freshly-installed Char as the trusted baseline
        # for the Layer B integrity check. We do this BEFORE any
        # subsequent ``./run.sh start`` so the operator doesn't see
        # the "no recorded Char baseline" drift on their first run.
        printf "\n  recording Char binary baseline for integrity checks...\n"
        "$VENV_PY" -m local_scribe.char.char_integrity --baseline-set || \
          say "${c_yellow}baseline-set failed; run \`./run.sh char baseline-set\` manually${c_reset}"
        printf "\n"
        if ask_yn "  Now configure Char to send transcripts to this server?" y; then
          printf "\n"
          cmd_configure_char || say "${c_yellow}Char config skipped/failed; rerun with ./run.sh configure-char${c_reset}"
        fi
      else
        say "${c_yellow}install failed; install Char manually then run \`./run.sh configure-char\`${c_reset}"
      fi
    else
      printf "  skipped — install Char manually, then: %s./run.sh configure-char%s\n" "$c_bold" "$c_reset"
    fi
  else
    local cur; cur="$(char_installed_version)"
    if [[ "$cur" == "$CHAR_KNOWN_GOOD_VERSION" ]]; then
      printf "  Char %s already installed (matches pinned).\n" "$cur"
    else
      printf "  %sChar %s installed; pinned %s.%s\n" \
             "$c_yellow" "$cur" "$CHAR_KNOWN_GOOD_VERSION" "$c_reset"
      printf "  Auto-config has only been tested against %s.\n" "$CHAR_KNOWN_GOOD_VERSION"
      if ask_yn "  Replace with pinned $CHAR_KNOWN_GOOD_VERSION?" n; then
        printf "\n"
        char_install_pinned || say "${c_yellow}install failed; keeping existing Char $cur${c_reset}"
      fi
    fi
    if ask_yn "  Configure Char.app now to send transcripts to this server?" y; then
      printf "\n"
      cmd_configure_char || say "${c_yellow}Char config skipped/failed; rerun with ./run.sh configure-char${c_reset}"
    else
      printf "  skipped — run %s./run.sh configure-char%s any time\n" "$c_bold" "$c_reset"
    fi
  fi

  printf "\n%s(10/10) per-Char outbound firewall — sandbox + egress proxy%s\n" \
         "$c_bold" "$c_reset"
  # This step is intentionally NON-INTERACTIVE and runs without sudo.
  # We just render the sandbox profile and confirm the proxy module
  # is importable. The proxy itself starts on the next ./run.sh
  # start (alongside the ASR + Inspector services).
  #
  # The legacy /etc/hosts mode (./run.sh firewall enable --mode system)
  # is still available for operators who want the machine-wide block,
  # but it's no longer the default -- it would break OTHER apps on
  # this machine that legitimately use the same providers.
  printf "  Composing macOS's two available per-process primitives:\n"
  printf "    %s•%s sandbox-exec  → restricts Char's network reach to loopback\n" "$c_dim" "$c_reset"
  printf "    %s•%s HTTPS_PROXY   → routes Char's HTTP(S) through 127.0.0.1:%s\n" \
         "$c_dim" "$c_reset" "$EGRESS_PROXY_PORT"
  printf "  Block list catalog: %s./run.sh firewall list%s\n" \
         "$c_bold" "$c_reset"
  printf "  Full rationale + threat model: %sSECURITY.md%s\n\n" "$c_bold" "$c_reset"

  # Render the SBPL profile so it's on disk before the first start.
  if "$VENV_PY" -m local_scribe.egress.char_sandbox write >/dev/null 2>&1; then
    local profile_path
    profile_path="$("$VENV_PY" -c 'from local_scribe.egress import char_sandbox; print(char_sandbox.profile_path())' 2>/dev/null)"
    say "${c_green}wrote sandbox profile to $profile_path${c_reset}"
    # Validate while we're here so a bad render fails loudly NOW
    # rather than at the first ./run.sh char launch.
    local validate_out
    if validate_out="$("$VENV_PY" -m local_scribe.egress.char_sandbox validate 2>&1)"; then
      say "${c_green}sandbox profile validates cleanly${c_reset}"
    else
      say "${c_yellow}sandbox validation produced warnings:${c_reset}"
      echo "$validate_out" | sed 's/^/    /'
    fi
  else
    say "${c_yellow}couldn't write sandbox profile; run \`./run.sh char sandbox write\` manually${c_reset}"
  fi

  printf "\n  Launch Char (after %s./run.sh start%s) with:\n" "$c_bold" "$c_reset"
  printf "    %s./run.sh char launch%s\n" "$c_bold" "$c_reset"
  printf "  Dock / Spotlight launches bypass the firewall — only the wrapper applies it.\n"
  printf "  %s./run.sh char firewall-status%s shows whether the running Char is filtered.\n" \
         "$c_bold" "$c_reset"
  printf "\n"
  printf "  %sOpt-in:%s if you want a machine-wide block (affects ALL apps, not just Char),\n" \
         "$c_dim" "$c_reset"
  printf "         run: %s./run.sh firewall enable --mode system%s   (asks for admin pwd)\n" \
         "$c_bold" "$c_reset"

  # Contributor-only convenience: if this clone is a git working
  # tree (not a downloaded tarball) install the secret-scan
  # pre-commit hook. Operators who never `git commit` notice
  # nothing; contributors get a hard guardrail against ever
  # pushing key material / API tokens upstream.
  #
  # See tools/secret_scan.sh and SECURITY.md → "Defense layer 7 —
  # secret-scan pre-commit hook" for the threat model. The hook
  # itself is a thin shim, so future edits to the scanner take
  # effect with no re-install.
  if [[ -d "$REPO/.git" && -x "$REPO/tools/install_git_hooks.sh" ]]; then
    if "$REPO/tools/install_git_hooks.sh" >/dev/null 2>&1; then
      say "${c_green}installed git pre-commit secret-scan hook${c_reset}"
    else
      say "${c_yellow}couldn't install pre-commit hook; run \`./tools/install_git_hooks.sh\` manually${c_reset}"
    fi
  fi

  # Auto-sign the pinned config + char baseline so the start-time
  # signature gate (pinned_config_gate) is immediately satisfied. Without
  # this, ``./run.sh start`` refuses with:
  #
  #   FAIL [pinned] no signature; run `./run.sh config sign`
  #   FAIL [char_baseline] no signature; run `./run.sh config sign`
  #
  # We do this AFTER stage 10 (so pinned.json + char_baseline.json are
  # both on disk) and BEFORE the completion banner (so the operator's
  # next-step hint doesn't immediately fail on first invocation). The
  # operator already did Touch ID + YubiKey for stage 3 (key init) and
  # stage 9 (configure-char); one more tap here is the price of admission
  # for a runnable pipeline. Failures here are non-fatal: bootstrap
  # itself still succeeded, the operator just has to manually run
  # ``./run.sh config sign`` before ``./run.sh start``.
  #
  # Skipped if:
  #   - venv python isn't on disk (we couldn't possibly have run the
  #     prior stages anyway — defensive only)
  #   - master key gate refuses (key_lifecycle has its own loud banner)
  #   - signatures are ALREADY valid (idempotent re-bootstrap path —
  #     don't ask for a redundant tap)
  printf "\n%ssigning pinned config + char baseline (Touch ID + YubiKey)…%s\n" \
         "$c_dim" "$c_reset"
  local sign_skip=0
  if [[ ! -x "$VENV_PY" ]]; then
    sign_skip=1
  elif "$VENV_PY" -m local_scribe config verify >/dev/null 2>&1; then
    printf "  %s● signatures already valid — skipping%s\n" "$c_green" "$c_reset"
    sign_skip=1
  fi
  if (( sign_skip == 0 )); then
    if "$VENV_PY" -m local_scribe config sign; then
      printf "  %s● pinned + baseline signed%s\n" "$c_green" "$c_reset"
    else
      say "${c_yellow}config sign failed; ./run.sh start will refuse until you run \`./run.sh config sign\` manually${c_reset}"
    fi
  fi

  printf "\n%s════════ bootstrap complete ════════%s\n\n" "$c_green" "$c_reset"

  # Anything we couldn't fully automate gets flagged here. Bootstrap covers:
  #   - Python venv + pip deps
  #   - Parakeet ASR weights + sherpa-onnx diarization models
  #   - LM Studio.app (homebrew cask) + lms CLI + Qwen download + load
  #   - Char.app (pinned DMG) + auto-config of Char's settings.json
  #   - Pinned config + Char baseline signature (Touch ID + YubiKey)
  # The only thing that's reliably *not* automated is wiring Char's
  # Intelligence (LLM) provider in the Char UI — that one tab needs a click.
  printf "%sFinal touch — wire Char's LLM tab (one click):%s\n" "$c_bold" "$c_reset"
  printf "  Char → Settings → Intelligence → provider = %sLM Studio%s,\n" "$c_bold" "$c_reset"
  printf "  base URL = %shttp://127.0.0.1:%s%s, model = %s%s%s\n" \
         "$c_bold" "$LMSTUDIO_PORT" "$c_reset" "$c_bold" "$LLM_MODEL" "$c_reset"

  printf "\n%sStart the pipeline:%s\n" "$c_bold" "$c_reset"
  printf "    %s./run.sh start%s\n" "$c_bold" "$c_reset"
  printf "\nVerify any time with: %s./run.sh doctor%s\n\n" "$c_bold" "$c_reset"
}

# --- Char configuration ---
#
# Char is a Tauri app whose settings live as JSON on disk under the legacy
# bundle id `com.hyprnote.stable`. We only touch the `ai.stt.openai.*` and
# `ai.current_stt_*` keys here; LLM provider, templates, etc. are left alone.

CHAR_APP="/Applications/Char.app"
CHAR_INFO_PLIST="$CHAR_APP/Contents/Info.plist"
CHAR_DATA_DIR="$HOME/Library/Application Support/hyprnote"
CHAR_SETTINGS="$CHAR_DATA_DIR/settings.json"
# tauri-plugin-store2 scoped store. Char's analytics-disable toggle lives at
# `analytics` -> `{"Disabled": true}`. PostHog short-circuits at the
# is_disabled() check inside tauri_plugin_analytics; see CHAR_REVIEW.md.
CHAR_STORE="$CHAR_DATA_DIR/store.json"

char_installed() { [[ -d "$CHAR_APP" ]]; }

# Read the installed Char's CFBundleShortVersionString. Echoes the version
# string on stdout (e.g. "1.0.24") or empty if Char isn't installed. Does NOT
# fail; callers compare with $CHAR_KNOWN_GOOD_VERSION themselves.
char_installed_version() {
  [[ -f "$CHAR_INFO_PLIST" ]] || return 0
  defaults read "$CHAR_INFO_PLIST" CFBundleShortVersionString 2>/dev/null || true
}

# Print a one-liner about version drift. Returns 0 when in-pin or Char is
# missing entirely; returns 1 only when we detect a mismatch (so callers can
# colorize accordingly). Never blocks.
char_version_status() {
  local v
  v="$(char_installed_version)"
  if [[ -z "$v" ]]; then
    return 0  # not installed; not our problem here
  fi
  if [[ "$v" == "$CHAR_KNOWN_GOOD_VERSION" ]]; then
    printf "Char %s (matches pinned)" "$v"
    return 0
  fi
  printf "Char %s installed; %s pinned -- auto-config tested against the pinned version only" \
         "$v" "$CHAR_KNOWN_GOOD_VERSION"
  return 1
}

char_running() {
  pgrep -f "$CHAR_APP/Contents/MacOS/char" >/dev/null 2>&1
}

char_quit() {
  osascript -e 'tell application "Char" to quit' >/dev/null 2>&1 || true
  for _ in {1..10}; do
    char_running || return 0
    sleep 0.5
  done
  pkill -f "$CHAR_APP/Contents/MacOS/char" 2>/dev/null || true
  sleep 1
}

char_relaunch() { open -ga Char; }

# Download + install the pinned Char.app from the GitHub Release attached to
# this commit. We refuse to overwrite an existing /Applications/Char.app
# without explicit confirmation. Returns 0 on success.
char_install_pinned() {
  local arch dmg_name expected_sha
  case "$(uname -m)" in
    arm64)  arch="aarch64"; expected_sha="$CHAR_DMG_SHA256_AARCH64" ;;
    x86_64) arch="x86_64";  expected_sha="$CHAR_DMG_SHA256_X86_64"  ;;
    *)
      say "${c_red}unsupported architecture: $(uname -m)${c_reset}"
      say "  Char DMGs are published for arm64 and x86_64 only"
      return 1 ;;
  esac
  dmg_name="hyprnote-macos-${arch}.dmg"
  local url="${CHAR_RELEASE_BASE_URL}/${dmg_name}"

  printf "  pinned version : %s\n" "$CHAR_KNOWN_GOOD_VERSION"
  printf "  arch           : %s\n" "$arch"
  printf "  download URL   : %s\n" "$url"
  printf "  expected sha256: %s\n" "$expected_sha"

  if char_installed; then
    local cur; cur="$(char_installed_version)"
    if [[ "$cur" == "$CHAR_KNOWN_GOOD_VERSION" ]]; then
      say "${c_green}Char $cur already installed and matches pin -- nothing to do${c_reset}"
      return 0
    fi
    if ! ask_yn "  Char $cur is installed; replace with pinned $CHAR_KNOWN_GOOD_VERSION?" n; then
      say "  keeping existing Char $cur"
      return 0
    fi
  fi

  local tmpdir; tmpdir="$(mktemp -d -t local_scribe_char.XXXXXX)"
  trap 'rm -rf "$tmpdir"; hdiutil detach "$tmpdir/mount" -quiet >/dev/null 2>&1 || true' RETURN
  local dmg_path="$tmpdir/$dmg_name"

  say "downloading $dmg_name (~600 MB on Apple Silicon, this takes a minute) ..."
  if ! curl -fL --progress-bar -o "$dmg_path" "$url"; then
    say "${c_red}download failed${c_reset}"
    return 1
  fi

  say "verifying sha256 ..."
  local actual_sha
  actual_sha="$(shasum -a 256 "$dmg_path" | awk '{print $1}')"
  if [[ "$actual_sha" != "$expected_sha" ]]; then
    say "${c_red}sha256 mismatch -- refusing to install${c_reset}"
    say "  expected: $expected_sha"
    say "  got     : $actual_sha"
    say "  this could mean the release was retagged, or the download was tampered with."
    return 1
  fi
  say "${c_green}sha256 ok${c_reset}"

  local mountpoint="$tmpdir/mount"
  mkdir -p "$mountpoint"
  say "mounting DMG ..."
  if ! hdiutil attach "$dmg_path" -mountpoint "$mountpoint" -nobrowse -quiet; then
    say "${c_red}failed to mount $dmg_path${c_reset}"
    return 1
  fi

  local src_app
  src_app="$(find "$mountpoint" -maxdepth 2 -name "Char.app" -print -quit 2>/dev/null)"
  if [[ -z "$src_app" ]]; then
    src_app="$(find "$mountpoint" -maxdepth 2 -name "Hyprnote.app" -print -quit 2>/dev/null)"
  fi
  if [[ -z "$src_app" ]]; then
    say "${c_red}couldn't find Char.app or Hyprnote.app inside the DMG${c_reset}"
    hdiutil detach "$mountpoint" -quiet >/dev/null 2>&1 || true
    return 1
  fi

  if char_running; then
    say "Char is running; quitting before replace ..."
    char_quit
  fi

  say "installing to $CHAR_APP ..."
  rm -rf "$CHAR_APP"
  if ! cp -R "$src_app" "$CHAR_APP"; then
    say "${c_red}cp failed -- you may need to grant your terminal Full Disk Access${c_reset}"
    hdiutil detach "$mountpoint" -quiet >/dev/null 2>&1 || true
    return 1
  fi
  hdiutil detach "$mountpoint" -quiet >/dev/null 2>&1 || true

  # Strip macOS quarantine so Gatekeeper doesn't pop the "downloaded from
  # internet" prompt on first launch. (We just verified the SHA pin manually,
  # so the user has already opted in to trusting this artifact.)
  xattr -dr com.apple.quarantine "$CHAR_APP" 2>/dev/null || true

  local new_v; new_v="$(char_installed_version)"
  say "${c_green}Char ${new_v} installed at $CHAR_APP${c_reset}"
}

# Yes/no prompt with default. $1=question, $2=default ("y" or "n").
# Returns 0 for yes, 1 for no. Falls back to default when there's no tty
# (so the script remains usable from CI / piped invocations).
ask_yn() {
  local prompt="$1" default="${2:-y}" reply
  local hint="[Y/n]"
  [[ "$default" == "n" ]] && hint="[y/N]"
  # /dev/tty is a *device file* — exists even on detached subshells where it
  # can't actually be opened. Probe by trying to open it; fall back to the
  # default if the open fails (no controlling terminal).
  if ! { : </dev/tty; } 2>/dev/null; then
    [[ "$default" == "y" ]]
    return $?
  fi
  printf "%s %s " "$prompt" "$hint" >/dev/tty 2>/dev/null
  IFS= read -r reply </dev/tty 2>/dev/null || reply=""
  reply="${reply:-$default}"
  case "${reply,,}" in
    y|yes) return 0 ;;
    *)     return 1 ;;
  esac
}

# Mask a secret-looking string for display.
mask_secret() {
  local s="$1"
  local n=${#s}
  if (( n < 12 )); then
    printf '<%d chars>' "$n"
  else
    printf '%s…%s (%d chars)' "${s:0:8}" "${s: -4}" "$n"
  fi
}

cmd_configure_char() {
  printf "%sconfigure char%s — point Char's OpenAI transcriber at this server\n\n" \
         "$c_bold" "$c_reset"

  if ! char_installed; then
    say "${c_red}Char.app not found at $CHAR_APP${c_reset}"
    say "  install Char from https://char.com (or https://github.com/fastrepl/anarlog),"
    say "  or run \`./run.sh install-char\` to fetch the pinned version $CHAR_KNOWN_GOOD_VERSION"
    return 1
  fi
  if [[ ! -f "$CHAR_SETTINGS" ]]; then
    say "${c_yellow}Char settings.json not found at $CHAR_SETTINGS${c_reset}"
    say "  open Char.app once so it creates its config dir, then re-run"
    return 1
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi
  # configure-char derives the ASR bearer token from the master key
  # (which requires Touch ID + YubiKey to unlock). That derivation
  # reconstitutes the master in memory — refuse if SIP is off.
  sip_gate || return 1

  local installed_v; installed_v="$(char_installed_version)"
  if [[ "$installed_v" != "$CHAR_KNOWN_GOOD_VERSION" ]]; then
    say "${c_yellow}WARNING: Char $installed_v installed; auto-config tested only against pinned $CHAR_KNOWN_GOOD_VERSION${c_reset}"
    say "  the four settings.json keys we patch are stable across recent versions,"
    say "  but if Char misbehaves after this, downgrade with: ./run.sh install-char"
    say ""
  fi

  if char_running; then
    say "Char is running; quitting it so settings.json edits stick ..."
    char_quit
  fi

  # Read current values (one per line) so we can show + decide whether to back up.
  local snapshot
  snapshot="$("$VENV_PY" - "$CHAR_SETTINGS" <<'PY'
import json, sys, pathlib
d = json.loads(pathlib.Path(sys.argv[1]).read_text())
ai  = d.get("ai") or {}
oai = ((ai.get("stt") or {}).get("openai")) or {}
print(ai.get("current_stt_provider") or "")
print(ai.get("current_stt_model") or "")
print(oai.get("base_url") or "")
print(oai.get("api_key") or "")
PY
)"
  local cur_prov cur_model cur_url cur_key
  cur_prov="$(printf '%s' "$snapshot"  | sed -n '1p')"
  cur_model="$(printf '%s' "$snapshot" | sed -n '2p')"
  cur_url="$(printf '%s' "$snapshot"   | sed -n '3p')"
  cur_key="$(printf '%s' "$snapshot"   | sed -n '4p')"

  printf "  current Char settings:\n"
  printf "    current_stt_provider : %s\n" "${cur_prov:-<unset>}"
  printf "    current_stt_model    : %s\n" "${cur_model:-<unset>}"
  printf "    stt.openai.base_url  : %s\n" "${cur_url:-<unset>}"
  if [[ -z "$cur_key" ]]; then
    printf "    stt.openai.api_key   : <unset>\n"
  elif [[ "$cur_key" == "local" ]]; then
    printf "    stt.openai.api_key   : local (already our placeholder)\n"
  else
    printf "    stt.openai.api_key   : %s  %s(looks like a real key)%s\n" \
           "$(mask_secret "$cur_key")" "$c_yellow" "$c_reset"
  fi
  printf "\n"

  # Offer to back up the existing key only if it's a non-placeholder value.
  local key_backup_path=""
  if [[ -n "$cur_key" && "$cur_key" != "local" ]]; then
    if ask_yn "  Save existing OpenAI API key to a file before replacing it?" y; then
      mkdir -p "$HOME/.config/local_scribe"
      chmod 700 "$HOME/.config/local_scribe" 2>/dev/null || true
      key_backup_path="$HOME/.config/local_scribe/char-openai-key.$(date +%Y%m%d-%H%M%S).txt"
      printf '%s\n' "$cur_key" > "$key_backup_path"
      chmod 600 "$key_backup_path"
      printf "  saved to %s (chmod 600)\n" "$key_backup_path"
      printf "  %sNOTE:%s if this is a real OpenAI key, rotate it on platform.openai.com — it sat unencrypted in Char's settings.json.\n" \
             "$c_yellow" "$c_reset"
    else
      printf "  skipping backup — existing key will be overwritten\n"
    fi
  fi

  # Always back up the whole settings file (cheap insurance).
  local settings_backup
  settings_backup="$CHAR_SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$CHAR_SETTINGS" "$settings_backup"
  printf "  settings.json backup : %s\n" "$settings_backup"

  # Fetch the current ASR token by unlocking the Option C split-key
  # (kc_half via Touch ID + yk_half via YubiKey tap) and HKDF-deriving
  # the per-service token from the master in memory. Both prompts may
  # appear on first call this shell session; Keychain ACL caches the
  # Touch ID grant for subsequent reads within the cache window.
  # This is the bearer token every request to the ASR server must carry.
  #
  # If the Keychain item doesn't exist yet, we surface a clear hint
  # and bail -- the user needs to run `./run.sh key init` first.
  printf "  Deriving ASR bearer token (Touch ID + YubiKey tap may be required) ...\n"
  local asr_token
  if ! asr_token="$("$VENV_PY" -m local_scribe.security.service_auth token asr 2>&1)"; then
    say "${c_red}failed to derive ASR token: $asr_token${c_reset}"
    say "  run \`./run.sh key init\` to create the Keychain master key first"
    return 1
  fi
  if [[ -z "$asr_token" ]]; then
    # Bypass mode -> use a placeholder value so Char's input field
    # isn't empty (some Char versions refuse to save an empty key).
    asr_token="local-auth-bypassed"
    say "${c_yellow}auth bypass enabled — Char's api_key set to a placeholder${c_reset}"
  fi

  # Patch the four keys we care about. Everything else (LLM,
  # templates, general.*, calendars, etc.) is left untouched.
  #
  # The ASR token is sent on stdin (as up to four newline-separated
  # values: settings_path, port, token, [launch_id]) to ``python -m
  # local_scribe.char.char_settings_writer`` so it never appears in
  # argv / a ``ps`` listing. ``printf`` is a bash builtin — it doesn't
  # fork, so even ``$asr_token`` as its argument lives only in this
  # shell's memory.
  #
  # When called from ``cmd_start`` the launch_id is set via the
  # ``LOCAL_SCRIBE_LAUNCH_ID`` env var (exported by ``cmd_start``).
  # In that case we append a 4th stdin line so the writer attaches
  # ``.ls<short_id>`` to the saved api_key — see Layer C
  # (launch_session.py). For a manual ``./run.sh configure-char``
  # outside of an active ``./run.sh start``, no env var is set, the
  # 4th line is omitted, and the writer falls back to the legacy
  # unbound-token form.
  local launch_id_for_writer="${LOCAL_SCRIBE_LAUNCH_ID:-}"
  if [[ -n "$launch_id_for_writer" ]]; then
    if ! printf '%s\n%s\n%s\n%s\n' \
            "$CHAR_SETTINGS" "$ASR_PORT" "$asr_token" "$launch_id_for_writer" \
         | "$VENV_PY" -m local_scribe.char.char_settings_writer; then
      say "${c_red}failed to write settings.json — your backup is at $settings_backup${c_reset}"
      return 1
    fi
  else
    if ! printf '%s\n%s\n%s\n' "$CHAR_SETTINGS" "$ASR_PORT" "$asr_token" \
         | "$VENV_PY" -m local_scribe.char.char_settings_writer; then
      say "${c_red}failed to write settings.json — your backup is at $settings_backup${c_reset}"
      return 1
    fi
  fi
  # Scrub the local token variable so a downstream `set` / `env` /
  # accidental subprocess inherit doesn't see it. Best-effort: the
  # ASR token is short-lived; the user can revoke it any time by
  # rotating the master key (`./run.sh key rotate`).
  unset asr_token

  # Disable PostHog analytics in Char's tauri-plugin-store2 scoped store. This
  # is the ONLY in-app telemetry kill-switch -- Sentry's DSN is compile-time
  # baked, see CHAR_REVIEW.md. The plugin reads `analytics.Disabled` as a
  # JSON-string-encoded value (Char wraps each scoped-store dict as a string).
  local analytics_status=""
  if [[ -f "$CHAR_STORE" ]]; then
    if "$VENV_PY" - "$CHAR_STORE" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text() or "{}")
except json.JSONDecodeError:
    d = {}
raw = d.get("analytics") or "{}"
try:
    inner = json.loads(raw) if isinstance(raw, str) else dict(raw)
except json.JSONDecodeError:
    inner = {}
inner["Disabled"] = True
d["analytics"] = json.dumps(inner, separators=(",", ":"))
p.write_text(json.dumps(d, indent=4) + "\n")
PY
    then
      analytics_status="disabled"
    else
      analytics_status="failed"
    fi
  else
    # File doesn't exist yet (Char hasn't run since install). Create it with
    # the disable flag pre-populated so PostHog never gets a chance.
    if "$VENV_PY" - "$CHAR_STORE" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"analytics": "{\"Disabled\":true}"}, indent=4) + "\n")
PY
    then
      analytics_status="disabled (created)"
    else
      analytics_status="failed"
    fi
  fi

  # Compute the token fingerprint (first 6 hex chars after the prefix)
  # for a safe-to-log identifier the user can match against
  # `./run.sh status`.
  local asr_fp
  asr_fp="$("$VENV_PY" -m local_scribe.security.service_auth fingerprint asr 2>/dev/null || true)"
  printf "\n  %s● char configured%s\n" "$c_green" "$c_reset"
  printf "    current_stt_provider : openai\n"
  printf "    current_stt_model    : gpt-4o-transcribe           (progressive/SSE -- no 60s client-side timeout)\n"
  printf "    stt.openai.base_url  : http://127.0.0.1:%s/v1\n" "$ASR_PORT"
  printf "    stt.openai.api_key   : ls_asr_…  (Keychain-derived; fingerprint=%s)\n" \
         "${asr_fp:-?}"
  printf "    posthog analytics    : %s\n" "$analytics_status"
  if [[ -n "$key_backup_path" ]]; then
    printf "    previous key saved   : %s\n" "$key_backup_path"
  fi
  printf "  %sNOTE:%s Sentry crash reporting + the auto-updater have NO in-app toggle.\n" "$c_yellow" "$c_reset"
  printf "        See CHAR_REVIEW.md for firewall / hosts-file mitigations.\n"
  printf "\n"

  if ask_yn "  Relaunch Char now?" y; then
    char_relaunch
    sleep 2
    if char_running; then
      say "${c_green}Char relaunched${c_reset}"
    else
      say "${c_yellow}Char did not relaunch automatically — open it manually${c_reset}"
    fi
  fi
}

cmd_install_char() {
  printf "%sinstall-char%s — fetch the pinned Char.app DMG and install to /Applications\n\n" \
         "$c_bold" "$c_reset"
  char_install_pinned
}

cmd_install_llm() {
  printf "%sinstall-llm%s — install LM Studio.app + lms CLI + download/load %s\n\n" \
         "$c_bold" "$c_reset" "$LLM_MODEL"
  if lmstudio_full_bootstrap; then
    printf "\n%sLM Studio + Qwen ready.%s\n" "$c_green" "$c_reset"
    return 0
  fi
  printf "\n%sLLM stack incomplete; fix the issues above and re-run.%s\n" \
         "$c_yellow" "$c_reset"
  return 1
}

# --- Service lifecycle (asr + inspector + egress proxy) ---
#
# As of the package refactor the actual start/stop/readiness-probe code
# lives in ``local_scribe.cli`` (Python). These shell helpers are thin
# delegators kept for backward compatibility with the many call sites
# above that say ``asr_start``, ``inspector_pid``, etc. Each ``_pid``
# helper stays in shell because it's a 4-line read-then-``kill -0``
# that 30+ status-display call sites rely on; each ``_start``/``_stop``
# delegates to ``python -m local_scribe <svc> <verb>`` so the readiness
# probe, log redirection, signal handling, and SIGKILL fallback are
# implemented once, in Python, and used identically whether the
# operator types ``./run.sh start`` or ``python -m local_scribe start``.
# See ``local_scribe/cli/_services.py`` for the canonical implementation.

asr_pid() {
  [[ -f "$ASR_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$ASR_PID_FILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

asr_start() { "$VENV_PY" -m local_scribe asr start; }
asr_stop()  { "$VENV_PY" -m local_scribe asr stop;  }

# Single-unlock bearer-token warmup. Sets the named bash variable
# (by NAMEREF, so the caller doesn't need to capture stdout) to
# the JSON blob ``{"asr": "ls_asr_...", "inspector": "ls_inspector_..."}``
# emitted by ``service_auth warm``.
#
# Honours the same env-var bypasses as the service workers:
#
#   * LOCAL_SCRIBE_DISABLE_AUTH=1 → empty JSON ``{}``; caller
#     skips the per-subprocess env injection and each service's
#     ``service_auth.is_bypass_enabled()`` short-circuit takes
#     over.
#   * LOCAL_SCRIBE_<SERVICE>_TOKEN already set → that service
#     is omitted from the unlock list and passes through.
#   * LOCAL_SCRIBE_MASTER_KEY_HEX / LOCAL_SCRIBE_TEST_MASTER_KEY_HEX
#     set → derive without Touch ID.
#
# The Touch ID + YubiKey banners are printed by
# ``unlock_master_key`` (inside the python subprocess), but those
# banners stream to OUR stderr — they're not redirected to a log
# file like the daemonised service spawns. So the operator sees
# the prompts in the foreground terminal, exactly where the
# 2026-05-11 audit asked us to put them.
#
# We print our own short heads-up FIRST so the operator knows
# this round-trip is coming before the system Touch ID modal
# appears.
warmup_service_tokens() {
  local -n _out="$1"
  _out=""

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  # Bypass mode: no unlock needed, no banner needed.
  if [[ "${LOCAL_SCRIBE_DISABLE_AUTH:-0}" != "0" ]]; then
    _out="{}"
    return 0
  fi

  # Heads-up banner. The actual Touch ID / YubiKey banners come
  # from inside the python helper (touch_prompts module); they
  # bracket the specific blocking step (Keychain read → YubiKey
  # tap → master-key combine). Our banner here covers the
  # higher-level intent ("you're about to be asked, here's why").
  printf '\n'
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_bold" "$c_reset"
  printf "%s%sAuthentication warmup%s\n" "$c_bold" "$c_yellow" "$c_reset"
  printf '\n'
  printf "  The ASR + inspector services each need a bearer token derived\n"
  printf "  from your master key. To avoid hitting you with two Touch ID\n"
  printf "  modals and two YubiKey taps in a row, we derive both tokens\n"
  printf "  from a single unlock right here.\n\n"
  printf "  Next steps (watch for them in this terminal):\n"
  printf "    1.  %sTouch ID modal%s pops up — authenticate to unlock the\n" \
         "$c_bold" "$c_reset"
  printf "        Keychain half of the master key.\n"
  printf "    2.  %sYubiKey LED starts flashing%s — tap it to release the\n" \
         "$c_bold" "$c_reset"
  printf "        YubiKey half. Total wait: typically <5 s after the tap.\n"
  printf '\n'
  printf "  Each service will then pick its token up from its own\n"
  printf "  per-subprocess environment and start up silently.\n"
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" \
         "$c_bold" "$c_reset"
  printf '\n'

  # Run the warm verb. Stdout = JSON; stderr = banners + errors.
  # We capture stdout into the nameref and let stderr stream
  # through to the terminal so the touch_prompts banners are
  # visible in real time.
  local _json
  if ! _json="$("$VENV_PY" -m local_scribe.security.service_auth warm asr inspector)"; then
    say "${c_red}token warmup failed — see the error above.${c_reset}"
    say "  Recover by re-running ${c_bold}./run.sh start${c_reset}, or use"
    say "  ${c_bold}LOCAL_SCRIBE_DISABLE_AUTH=1 ./run.sh start${c_reset} to bypass auth"
    say "  entirely (NOT recommended; every endpoint becomes open)."
    return 1
  fi
  _out="$_json"
  printf "  %stokens derived%s — starting services\n\n" \
         "$c_green" "$c_reset"
  return 0
}

inspector_pid() {
  [[ -f "$INSPECTOR_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$INSPECTOR_PID_FILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

inspector_start() { "$VENV_PY" -m local_scribe inspector start; }
inspector_stop()  { "$VENV_PY" -m local_scribe inspector stop;  }

egress_proxy_pid() {
  [[ -f "$EGRESS_PROXY_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$EGRESS_PROXY_PID_FILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

egress_proxy_start() { "$VENV_PY" -m local_scribe egress-proxy start; }
egress_proxy_stop()  { "$VENV_PY" -m local_scribe egress-proxy stop;  }

cmd_inspector() {
  case "${1:-status}" in
    start|stop|restart|status|open|logs|log)
      # All inspector verbs flow through the Python CLI so the contract
      # ``./run.sh inspector X`` ≡ ``python -m local_scribe inspector X``.
      exec "$VENV_PY" -m local_scribe inspector "${1:-status}"
      ;;
    *)
      printf "usage: %s./run.sh inspector%s {start|stop|restart|status|open|logs}\n" "$c_bold" "$c_reset"
      return 2
      ;;
  esac
}

# --- LM Studio ---

# Path to the `lms` CLI binary. LM Studio.app stages it in two different
# spots depending on the version it shipped with:
#   - ~/.cache/lm-studio/bin/lms   (current; 0.4.x)
#   - ~/.lmstudio/bin/lms          (legacy; 0.3.x and earlier)
# Both are populated by `lms bootstrap`, which the .app runs on first launch
# (or which we run explicitly during `./run.sh bootstrap`).
# Returns 0 on found, prints the path on stdout.
lms_path() {
  if command -v lms >/dev/null 2>&1; then command -v lms; return 0; fi
  for c in "$HOME/.cache/lm-studio/bin/lms" "$HOME/.lmstudio/bin/lms"; do
    [[ -x "$c" ]] && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

lmstudio_app_installed() { [[ -d "$LMSTUDIO_APP_PATH" ]]; }

lmstudio_app_version() {
  defaults read "$LMSTUDIO_APP_PATH/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null
}

# Install LM Studio.app via Homebrew cask (preferred — auto-updating, signed,
# small DMG-equivalent). Returns 1 if Homebrew isn't available so the caller
# can point the user at the manual download.
lmstudio_install_app() {
  if ! command -v brew >/dev/null 2>&1; then
    say "${c_yellow}Homebrew not installed${c_reset}"
    say "  install Homebrew (https://brew.sh) and re-run, or download LM Studio"
    say "  manually from https://lmstudio.ai/download"
    return 1
  fi
  say "installing LM Studio.app via 'brew install --cask lm-studio' ..."
  if ! brew install --cask lm-studio; then
    say "${c_red}brew install --cask lm-studio failed${c_reset}"
    return 1
  fi
  say "${c_green}LM Studio.app installed${c_reset}"
  return 0
}

# Symlink the bundled lms binary into ~/.cache/lm-studio/bin/ so it's on
# PATH. This is what LM Studio.app does on first GUI launch; we run it
# explicitly so bootstrap doesn't require the user to open the app first.
lmstudio_bootstrap_cli() {
  if lms_path >/dev/null; then return 0; fi
  if ! lmstudio_app_installed; then
    say "${c_red}LM Studio.app not installed; can't bootstrap lms CLI${c_reset}"
    return 1
  fi
  # Path inside the bundle has shifted across versions; this glob covers
  # every layout we've seen from 0.3.x through 0.4.x.
  local bundled
  bundled="$(find "$LMSTUDIO_APP_PATH/Contents/Resources" -type f -name 'lms' -perm -u+x 2>/dev/null | head -1)"
  if [[ -z "$bundled" ]]; then
    say "${c_yellow}couldn't find lms inside LM Studio.app bundle${c_reset}"
    say "  open LM Studio.app once (it auto-symlinks lms on first launch), then re-run"
    return 1
  fi
  if ! "$bundled" bootstrap >/dev/null 2>&1; then
    say "${c_yellow}'$bundled bootstrap' failed${c_reset}"
    say "  fall back: open LM Studio.app once, then re-run \`./run.sh bootstrap\`"
    return 1
  fi
  say "${c_green}lms CLI bootstrapped → $(lms_path)${c_reset}"
  return 0
}

lmstudio_running() {
  curl -sf "http://127.0.0.1:$LMSTUDIO_PORT/v1/models" >/dev/null 2>&1
}

lmstudio_model_loaded() {
  local model="$1"
  curl -s "http://127.0.0.1:$LMSTUDIO_PORT/api/v0/models" \
    | "$VENV_PY" -c "
import json, sys
m = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for entry in data.get('data', []):
    if entry.get('id') == m and entry.get('state') == 'loaded':
        sys.exit(0)
sys.exit(1)
" "$model"
}

# Has $1=model been downloaded into LM Studio's local store on disk?
# Different from lmstudio_model_loaded() which checks if it's in RAM right now.
#
# Uses LM Studio's HTTP API (/api/v0/models) rather than parsing the human-
# friendly `lms ls` table output, since the API gives us clean model keys
# regardless of whether they came in as a repo path ("qwen/qwen3-4b") or
# as a flat identifier ("qwen3-30b-a3b-instruct-2507"). Requires the LM
# Studio HTTP server to be running — which the bootstrap step ensures
# before calling us.
lmstudio_model_downloaded() {
  local model="$1"
  curl -s "http://127.0.0.1:$LMSTUDIO_PORT/api/v0/models" 2>/dev/null \
    | "$VENV_PY" -c "
import json, sys
m = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for entry in data.get('data', []):
    eid = entry.get('id') or ''
    # Match either an exact id or a repo-prefixed variant ('qwen/' + key).
    if eid == m or eid.endswith('/' + m) or m.endswith('/' + eid):
        sys.exit(0)
sys.exit(1)
" "$model"
}

# Download $1=model_repo (e.g. "qwen/qwen3-30b-a3b-instruct-2507") via lms.
# Always pulls the MLX variant since we're on Apple Silicon. -y auto-accepts
# the variant prompt so this is non-interactive.
lmstudio_model_download() {
  local repo="$1" lms
  lms="$(lms_path)" || return 1
  say "downloading $repo (MLX) — large file, will take a few minutes ..."
  if ! "$lms" get "$repo" --mlx -y; then
    say "${c_red}lms get $repo failed${c_reset}"
    return 1
  fi
  return 0
}

# End-to-end LM Studio + Qwen bootstrap. Idempotent — each step short-circuits
# with a green checkmark when nothing's needed.
#
# Steps:
#   1. Install LM Studio.app via brew cask (skipped if /Applications/LM Studio.app exists).
#   2. Bootstrap the lms CLI (skipped if `lms` is on PATH).
#   3. Start the local HTTP server on $LMSTUDIO_PORT (skipped if already up).
#   4. Choose a model based on system RAM:
#        - ≥$LLM_MIN_RAM_GB GB → $LLM_MODEL_REPO (default: qwen3-30b-a3b-instruct-2507, ~32 GB)
#        - <$LLM_MIN_RAM_GB GB → offer $LLM_MODEL_SMALL_REPO (~2.3 GB) instead
#      Confirm the download (size-aware prompt) before pulling.
#   5. Load the chosen model with $LLM_CONTEXT context length.
#
# Returns 0 if everything ended up in a usable state, non-zero otherwise.
lmstudio_full_bootstrap() {
  if ! lmstudio_app_installed; then
    printf "  LM Studio.app not installed at %s.\n" "$LMSTUDIO_APP_PATH"
    if ask_yn "  Install LM Studio.app via 'brew install --cask lm-studio' now?" y; then
      lmstudio_install_app || return 1
    else
      say "  skipped — install manually from https://lmstudio.ai/download then re-run"
      return 1
    fi
  else
    local v v_short
    v="$(lmstudio_app_version)"
    # CFBundleShortVersionString sometimes carries a build suffix
    # (e.g. "0.4.12+1"); strip it for a friendlier match against our pin.
    v_short="${v%%+*}"; v_short="${v_short%%-*}"
    if [[ -z "$v" ]]; then
      printf "  LM Studio.app installed at %s.\n" "$LMSTUDIO_APP_PATH"
    elif [[ "$v_short" == "$LMSTUDIO_KNOWN_GOOD_VERSION" ]]; then
      printf "  LM Studio.app %s already installed (matches pinned %s).\n" \
             "$v" "$LMSTUDIO_KNOWN_GOOD_VERSION"
    else
      printf "  LM Studio.app %s installed (pinned %s — usually compatible).\n" \
             "$v" "$LMSTUDIO_KNOWN_GOOD_VERSION"
    fi
  fi

  if ! lms_path >/dev/null; then
    printf "  bootstrapping lms CLI ...\n"
    lmstudio_bootstrap_cli || return 1
  else
    printf "  lms CLI present at %s\n" "$(lms_path)"
  fi
  local LMS; LMS="$(lms_path)"

  if lmstudio_running; then
    printf "  LM Studio HTTP server already up on :%s\n" "$LMSTUDIO_PORT"
  else
    say "  starting LM Studio HTTP server on :$LMSTUDIO_PORT ..."
    "$LMS" server start --port "$LMSTUDIO_PORT" >/dev/null 2>&1 || true
    for _ in {1..15}; do lmstudio_running && break; sleep 1; done
    if ! lmstudio_running; then
      say "${c_red}  LM Studio API didn't come up on :$LMSTUDIO_PORT${c_reset}"
      return 1
    fi
    say "${c_green}  LM Studio HTTP server up on :$LMSTUDIO_PORT${c_reset}"
  fi

  local ram; ram="$(machine_ram_gb)"
  printf "  detected %s GB unified memory.\n" "$ram"
  if [[ "$ram" -gt 0 && "$ram" -lt $LLM_MIN_RAM_GB && "$LLM_MODEL" != "$LLM_MODEL_SMALL" ]]; then
    if lmstudio_model_downloaded "$LLM_MODEL_SMALL"; then
      printf "  ${c_yellow}<%s GB unified memory; using smaller %s (already downloaded).${c_reset}\n" \
             "$LLM_MIN_RAM_GB" "$LLM_MODEL_SMALL"
      LLM_MODEL="$LLM_MODEL_SMALL"
    elif ask_yn "  $ram GB RAM is below the ${LLM_MIN_RAM_GB} GB threshold for the 30B; download $LLM_MODEL_SMALL_REPO (~2.3 GB) instead?" y; then
      LLM_MODEL="$LLM_MODEL_SMALL"
      lmstudio_model_download "$LLM_MODEL_SMALL_REPO" || return 1
    fi
  fi

  if ! lmstudio_model_downloaded "$LLM_MODEL"; then
    local repo size
    if [[ "$LLM_MODEL" == "$LLM_MODEL_SMALL" ]]; then
      repo="$LLM_MODEL_SMALL_REPO"; size="2.3 GB"
    else
      repo="$LLM_MODEL_REPO"; size="32 GB"
    fi
    if ask_yn "  Download $repo (~$size MLX) into LM Studio now?" y; then
      lmstudio_model_download "$repo" || return 1
    else
      say "  skipped — load it later from LM Studio.app's Discover tab"
      return 1
    fi
  else
    printf "  %s already downloaded.\n" "$LLM_MODEL"
  fi

  if lmstudio_model_loaded "$LLM_MODEL"; then
    printf "  %s already loaded into RAM.\n" "$LLM_MODEL"
  else
    say "  loading $LLM_MODEL (context=$LLM_CONTEXT) ..."
    if ! "$LMS" load "$LLM_MODEL" -y --context-length "$LLM_CONTEXT" >/dev/null 2>&1; then
      say "${c_red}  lms load $LLM_MODEL failed — try loading it from LM Studio.app${c_reset}"
      return 1
    fi
    say "${c_green}  $LLM_MODEL loaded${c_reset}"
  fi

  return 0
}

# Bring LM Studio + Qwen up. Returns:
#   0 - LM Studio reachable AND $LLM_MODEL loaded (fully ready)
#   1 - LM Studio not reachable
#   2 - LM Studio reachable but $LLM_MODEL not loaded
# Callers use the exit code to decide how to render the readiness banner.
lmstudio_start() {
  local LMS=""
  LMS="$(lms_path 2>/dev/null)" || true

  if ! lmstudio_running; then
    if [[ -n "$LMS" ]]; then
      say "starting LM Studio HTTP server ..."
      "$LMS" server start --port "$LMSTUDIO_PORT" >/dev/null 2>&1 || true
      for _ in {1..15}; do
        lmstudio_running && break
        sleep 1
      done
    fi
    if ! lmstudio_running; then
      say "${c_red}LM Studio API not reachable on :$LMSTUDIO_PORT${c_reset}"
      if [[ -z "$LMS" ]]; then
        say "  run \`./run.sh bootstrap\` to install LM Studio.app + the lms CLI"
        say "  (or open LM Studio.app and turn on Developer > Local Server)"
      else
        say "  is LM Studio.app installed? open it once to grant permissions"
      fi
      return 1
    fi
  fi
  say "${c_green}LM Studio API up on :$LMSTUDIO_PORT${c_reset}"

  if lmstudio_model_loaded "$LLM_MODEL"; then
    say "${c_green}$LLM_MODEL already loaded${c_reset}"
    return 0
  fi

  if [[ -z "$LMS" ]]; then
    say "${c_yellow}$LLM_MODEL not loaded${c_reset}"
    say "  run \`./run.sh bootstrap\` to install + load it, or load it from LM Studio.app's UI"
    return 2
  fi

  say "loading $LLM_MODEL with context-length=$LLM_CONTEXT (this can take a minute) ..."
  if ! "$LMS" load "$LLM_MODEL" -y --context-length "$LLM_CONTEXT" >/dev/null 2>&1; then
    say "${c_red}failed to load $LLM_MODEL via lms${c_reset}"
    say "  is it downloaded? run \`./run.sh bootstrap\` (it'll fetch + load it)"
    say "  or check with: $LMS ls"
    return 2
  fi
  say "${c_green}$LLM_MODEL loaded${c_reset}"
  return 0
}

# --- top-level commands ---

cmd_start() {
  # Optional ``--dev`` flag: shorthand for setting
  # LOCAL_SCRIBE_DEV_MODE=1 for this invocation + every subprocess
  # we spawn (uvicorn workers inherit the env). Loud-but-explicit:
  # the gate banner still fires, the inspector still renders the
  # red banner, and ./run.sh status / doctor still surface "dev
  # mode active". See SECURITY.md § 'Dev mode' for the threat
  # implications.
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dev|--dev-mode)
        export LOCAL_SCRIBE_DEV_MODE=1
        printf "%s%s[--dev] setting LOCAL_SCRIBE_DEV_MODE=1 for this run.%s\n" \
               "$c_red" "$c_bold" "$c_reset"
        shift
        ;;
      --)
        shift; break
        ;;
      -*)
        say "${c_red}unknown flag to start: $1${c_reset}"
        say "  supported flags: --dev  (alias --dev-mode)"
        return 2
        ;;
      *)
        break
        ;;
    esac
  done

  printf "%s%s%s\n" "$c_bold" "starting transcription pipeline" "$c_reset"
  sip_gate || return 1
  # Master-key gate runs *after* SIP (because reading the gate's
  # answer is itself a Keychain probe -- meaningless if SIP is off)
  # and *before* script integrity (because there's no point checking
  # script hashes if we'd refuse to launch anyway).
  master_key_gate || return 1
  script_integrity_gate || return 1
  pinned_config_gate || return 1
  # Vault-relocation gate sits BEFORE char_integrity_gate so we don't
  # fingerprint a Char.app that's about to be told it can't run.
  # If the Char data dir is still plaintext on disk, we refuse here
  # with explicit recovery steps (see vault_relocation_gate above).
  vault_relocation_gate || return 1
  # Auto-unlock gate: ``cmd_stop`` dismounts the vault, so by the
  # time the operator runs ``cmd_start`` again the symlink may point
  # into an unmounted volume. The gate detects that case and
  # transparently prompts for Touch ID + YubiKey to re-mount before
  # any service tries to read through the symlink.
  vault_auto_unlock_gate || return 1
  char_integrity_gate || return 1
  preflight || {
    say "${c_red}preflight failed${c_reset}"
    say "  fix the errors above (or run \`./run.sh setup\`) before starting"
    return 1
  }

  # Layer C — mint a launch session BEFORE services come up so the
  # services pick up LOCAL_SCRIBE_LAUNCH_ID from their env. The trap
  # removes the lock on EXIT / INT / TERM so a kill -9 still leaves a
  # closed-state file behind (close_lock writes status=closed before
  # unlinking, mitigating the "kill -9 leaks lock" race).
  launch_session_mint || return 1
  trap 'launch_session_close' EXIT INT TERM

  # Token warmup — derive bearer tokens for every service that
  # needs one BEFORE we spawn anything. Each spawned service
  # picks up its token from its own per-subprocess environ and
  # skips its own (otherwise-inevitable) Touch ID + YubiKey
  # round-trip. Without this:
  #
  #   * ASR's lifespan hook → unlock #1 (Touch ID + YubiKey)
  #   * Inspector's startup → unlock #2 (Touch ID + YubiKey)
  #
  # …and both prompts fire while the operator is staring at
  # "starting asr ..." with no textual cue. The banners ARE
  # printed by ``touch_prompts`` but they land in the service's
  # log file (stdout/stderr are redirected for the daemonised
  # spawn), not in the operator's terminal.
  #
  # With the warmup: ONE Touch ID + ONE YubiKey tap up front,
  # visible banner in run.sh's foreground, and the spawned
  # services come up silently because their tokens are already
  # in env.
  local _warmup_json="" _asr_tok="" _inspector_tok=""
  if ! warmup_service_tokens _warmup_json; then
    return 1
  fi
  # Parse JSON into bash locals. We pipe through python rather
  # than shelling out to jq (which isn't a hard dependency) and
  # rather than parsing JSON in bash by hand (which is a footgun).
  if [[ -n "$_warmup_json" && "$_warmup_json" != "{}" ]]; then
    _asr_tok="$("$VENV_PY" -c '
import json, sys
print(json.loads(sys.argv[1]).get("asr", ""))
' "$_warmup_json")"
    _inspector_tok="$("$VENV_PY" -c '
import json, sys
print(json.loads(sys.argv[1]).get("inspector", ""))
' "$_warmup_json")"
  fi
  # Scrub the JSON blob — we don't want the full token JSON
  # lingering in the parent shell's environ / locals.
  unset _warmup_json

  lmstudio_start
  local lms_rc=$?    # 0 ok, 1 unreachable, 2 reachable-but-no-model

  # Per-subprocess env: ``LOCAL_SCRIBE_ASR_TOKEN=val asr_start``
  # exports the var into ``asr_start``'s environ ONLY, so the
  # token never enters this parent shell's environ and the
  # ``exec tail -F`` at the end of cmd_start doesn't inherit it.
  # The bash ``VAR=val funcname`` quirk is exactly what we want
  # here -- see SECURITY.md § "Defense layer 2" for the threat
  # model around env-var token passing.
  LOCAL_SCRIBE_ASR_TOKEN="$_asr_tok" asr_start || return 1
  # Inspector is best-effort: if it fails to start, the ASR pipeline
  # still works -- just no web UI. Don't fail the whole `start` for it.
  LOCAL_SCRIBE_INSPECTOR_TOKEN="$_inspector_tok" inspector_start \
    || say "${c_yellow}inspector failed to start; ASR pipeline still ok${c_reset}"
  unset _asr_tok _inspector_tok
  # Egress proxy is also best-effort: ASR / LM Studio still work
  # without it, but Char loses its outbound firewall. Surface the
  # failure prominently though, since the most-secure default expects
  # the proxy to be the gate.
  if egress_proxy_start; then
    :
  else
    say "${c_red}egress proxy failed to start — Char's outbound firewall is OFF${c_reset}"
    say "  recover with: ${c_bold}./run.sh restart${c_reset}  (or see $EGRESS_PROXY_LOG_FILE)"
  fi
  printf "\n"

  if [[ $lms_rc -eq 0 ]]; then
    printf "%s──── pipeline ready ────%s\n" "$c_bold" "$c_reset"
    printf "  ASR server (Parakeet TDT v3) : %shttp://127.0.0.1:%s%s   (Char's transcription endpoint)\n" \
           "$c_green" "$ASR_PORT" "$c_reset"
    printf "  LM Studio API (Qwen3-30B)    : %shttp://127.0.0.1:%s%s   (summary + speaker naming)\n" \
           "$c_green" "$LMSTUDIO_PORT" "$c_reset"
    if egress_proxy_pid >/dev/null; then
      printf "  Egress proxy (per-Char fw)   : %shttp://127.0.0.1:%s%s   (catalog: %s./run.sh firewall list%s)\n" \
             "$c_green" "$EGRESS_PROXY_PORT" "$c_reset" "$c_bold" "$c_reset"
    else
      printf "  Egress proxy (per-Char fw)   : %sNOT RUNNING%s   (Char traffic is NOT being filtered)\n" \
             "$c_red" "$c_reset"
    fi
    if inspector_pid >/dev/null; then
      printf "  Inspector (web UI)           : %shttp://127.0.0.1:%s/%s   (sessions, config, char audit)\n" \
             "$c_green" "$INSPECTOR_PORT" "$c_reset"
      # First-run auth: print the clickable URL that sets the
      # inspector cookie. After the browser visits this once, the
      # cookie persists 30 days so subsequent visits to / just work.
      local inspector_url=""
      if [[ -x "$VENV_PY" ]]; then
        inspector_url="$("$VENV_PY" -m local_scribe.security.service_auth url inspector 2>/dev/null || true)"
      fi
      if [[ -n "$inspector_url" && "$inspector_url" != *"<bypass>"* ]]; then
        printf "  First-time browser auth      : %s%s%s\n" \
               "$c_bold" "$inspector_url" "$c_reset"
        printf "                                 (⌘-click once to set the cookie)\n"
      fi
    fi
  else
    printf "%s──── pipeline %sPARTIALLY%s ready ────%s\n" \
           "$c_bold" "$c_yellow" "$c_reset$c_bold" "$c_reset"
    printf "  ASR server (Parakeet TDT v3) : %shttp://127.0.0.1:%s%s   (transcription works)\n" \
           "$c_green" "$ASR_PORT" "$c_reset"
    if [[ $lms_rc -eq 1 ]]; then
      printf "  LM Studio API                : %sNOT REACHABLE on :%s%s\n" \
             "$c_red" "$LMSTUDIO_PORT" "$c_reset"
      printf "                                 → Char's summary step will fail until you start LM Studio\n"
    else
      printf "  LM Studio API                : %shttp://127.0.0.1:%s%s   (reachable)\n" \
             "$c_green" "$LMSTUDIO_PORT" "$c_reset"
      printf "  %s                          : %sNOT LOADED%s\n" \
             "$LLM_MODEL" "$c_red" "$c_reset"
      printf "                                 → Char's summary step will fail; load the model in LM Studio.app\n"
    fi
  fi
  printf "  log file                     : %s\n" "$ASR_LOG_FILE"
  printf "\n"
  printf "  launch Char:  %s./run.sh char launch%s   ${c_dim}(routes Char through the egress proxy + sandbox)${c_reset}\n" \
         "$c_bold" "$c_reset"
  printf "  on-demand:    %s./run.sh transcribe ~/Desktop/call.m4a%s\n" "$c_bold" "$c_reset"
  printf "  status:       %s./run.sh status%s\n" "$c_bold" "$c_reset"
  printf "  doctor:       %s./run.sh doctor%s   (full health report)\n" "$c_bold" "$c_reset"
  printf "  stop:         %s./run.sh stop%s\n" "$c_bold" "$c_reset"
  printf "\n"
  printf "tailing ASR server log; %sCtrl+C detaches without stopping%s\n" \
         "$c_yellow" "$c_reset"
  printf "\n"
  exec tail -F "$ASR_LOG_FILE"
}

cmd_stop() {
  asr_stop
  inspector_stop
  egress_proxy_stop
  launch_session_close
  # Lock-on-stop: the whole point of the encrypted vault is that
  # plaintext only exists on disk while the operator is actively
  # using the pipeline. Leaving it mounted after ``stop`` defeats
  # that guarantee for anyone who can read the filesystem as our
  # UID (including a curious shell session, a misconfigured backup
  # agent, or any compromised app running as the user).
  #
  # ``vault_lock_on_stop`` is idempotent + polite-only: it skips when
  # Char.app is still running (refuses to risk SQLite corruption on
  # ``app.db``), when the vault is already unmounted, and when there
  # is no vault at all. The operator can always force a lock with
  # ``./run.sh vault lock`` after quitting Char.
  vault_lock_on_stop
  printf "  %sLM Studio left running%s; use \`lms server stop\` to free GPU memory\n" \
         "$c_dim" "$c_reset"
}

cmd_status() {
  printf "%spipeline status%s\n" "$c_bold" "$c_reset"

  # Dev-mode marker before the service table. The env var visible
  # here only reflects the *current shell*; a service that was
  # started with --dev exports the var into its own environment
  # and the inspector's /api/dev_mode/status endpoint is the
  # authoritative source for whether the running services are in
  # dev mode. We surface both signals.
  local _devmode="${LOCAL_SCRIBE_DEV_MODE:-}"
  local _devmode_lower
  _devmode_lower="$(printf '%s' "$_devmode" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "$_devmode" && "$_devmode_lower" != "0" && "$_devmode_lower" != "false" \
        && "$_devmode_lower" != "no" && "$_devmode_lower" != "off" ]]; then
    printf "  %s%s[DEV MODE] this shell has LOCAL_SCRIBE_DEV_MODE=%s%s\n" \
           "$c_red" "$c_bold" "$_devmode" "$c_reset"
    printf "             SIP gates are bypassed; see SECURITY.md § 'Dev mode'.\n"
  fi

  if asr_pid >/dev/null; then
    local backend="?" model="?"
    local health_json; health_json="$(curl -sf "http://127.0.0.1:$ASR_PORT/health" 2>/dev/null || true)"
    if [[ -n "$health_json" ]]; then
      backend="$(printf '%s' "$health_json" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('asr_backend','?'))" 2>/dev/null || echo "?")"
      model="$(printf '%s' "$health_json"   | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('model','?'))" 2>/dev/null || echo "?")"
    fi
    printf "  "; ok; printf "ASR server       pid=%-7s port=%s   backend=%s   model=%s\n" \
                          "$(asr_pid)" "$ASR_PORT" "$backend" "$model"
    printf "                   log=%s\n" "$ASR_LOG_FILE"
  else
    printf "  "; bad; printf "ASR server       not running\n"
  fi
  if lmstudio_running; then
    printf "  "; ok; printf "LM Studio API    port=%s\n" "$LMSTUDIO_PORT"
    if lmstudio_model_loaded "$LLM_MODEL"; then
      printf "  "; ok; printf "%s   loaded\n" "$LLM_MODEL"
    else
      printf "  "; warn; printf "%s   not loaded\n" "$LLM_MODEL"
    fi
  else
    printf "  "; bad; printf "LM Studio API    not running on :%s\n" "$LMSTUDIO_PORT"
  fi
  if inspector_pid >/dev/null; then
    printf "  "; ok; printf "Inspector        pid=%-7s url=http://%s:%s\n" \
                          "$(inspector_pid)" "$INSPECTOR_BIND" "$INSPECTOR_PORT"
  else
    printf "  "; warn; printf "Inspector        not running (\`./run.sh inspector start\`)\n"
  fi

  # ---- Per-service auth: token fingerprints + inspector URL --------
  #
  # We print the fingerprint (first 6 hex chars after the prefix) so
  # the operator can match what Char's settings.json has against what
  # the server is currently enforcing -- a drift here is the most
  # common cause of "Char's Generate just spins forever".
  #
  # The inspector URL is the one-shot link that sets the auth cookie.
  # Printed verbatim so the user can ⌘-click it from the terminal.
  printf "\n  %sAuthentication%s (per-service tokens; HKDF-derived from Keychain master key)\n" \
         "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    local asr_fp inspector_url
    asr_fp="$("$VENV_PY" -m local_scribe.security.service_auth fingerprint asr 2>/dev/null || echo "?")"
    inspector_url="$("$VENV_PY" -m local_scribe.security.service_auth url inspector 2>/dev/null || echo "?")"
    if [[ "$asr_fp" == "<bypass>" ]]; then
      printf "  "; warn; printf "AUTH BYPASS enabled (LOCAL_SCRIBE_DISABLE_AUTH=1) — every endpoint is OPEN\n"
    else
      printf "    ASR token fingerprint    : %s\n" "$asr_fp"
      printf "    Inspector auth URL       : %s\n" "$inspector_url"
      printf "    (⌘-click the URL once in the browser to set the inspector cookie.)\n"
    fi
  fi

  # Encryption-at-rest summary. Whether the vault is mounted +
  # whether Char's data dir actually lives inside it is the same
  # question ``vault_relocation_gate`` asks at start time; surfacing
  # it here means an operator who runs ``./run.sh status`` between
  # bootstraps notices the missing relocation BEFORE they accumulate
  # plaintext sessions. The 2026-05-11 audit found 6 sessions
  # plaintext on disk because nothing forced the operator's eye to
  # this row.
  printf "\n  %sEncryption at rest%s (Char data + transcripts)\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<'PY'
import sys
G,Y,R,Z = ("\033[32m","\033[33m","\033[31m","\033[0m") if sys.stderr.isatty() else ("","","","")
try:
    from local_scribe.security import vault
except Exception as exc:  # noqa: BLE001
    print(f"    {R}\u25cb{Z} vault module unimportable: {exc}")
    sys.exit(0)
s = vault.status()
if not s["exists"]:
    print(f"    {R}\u25cb{Z} no vault on disk — run `./run.sh vault init`")
    sys.exit(0)
if s["char_data_relocated"]:
    print(f"    {G}\u25cf{Z} Char data lives INSIDE the vault (encrypted at rest)")
else:
    print(f"    {R}\u25cb{Z} Char data is PLAINTEXT at "
          f"{s['char_data_path']}")
    print(f"        run `./run.sh vault unlock` (quit Char first)")
if s["mounted"]:
    print(f"    {G}\u25cf{Z} vault mounted at {s['mount_path']}")
else:
    print(f"    {Y}\u25cb{Z} vault not mounted — `./run.sh vault unlock`")
PY
  fi
}

cmd_logs() {
  if [[ -f "$ASR_LOG_FILE" ]]; then
    exec tail -F "$ASR_LOG_FILE"
  else
    say "no log file at $ASR_LOG_FILE"
    say "(start the pipeline with './run.sh start' first)"
    exit 1
  fi
}

cmd_health() {
  local rc=0
  if curl -sf "http://127.0.0.1:$ASR_PORT/health" -o /dev/null; then
    printf "  "; ok; printf "ASR @ :%s\n" "$ASR_PORT"
  else
    printf "  "; bad; printf "ASR @ :%s\n" "$ASR_PORT"; rc=1
  fi
  if curl -sf "http://127.0.0.1:$LMSTUDIO_PORT/v1/models" -o /dev/null; then
    printf "  "; ok; printf "LM Studio @ :%s\n" "$LMSTUDIO_PORT"
  else
    printf "  "; bad; printf "LM Studio @ :%s\n" "$LMSTUDIO_PORT"; rc=1
  fi
  return $rc
}

cmd_transcribe() {
  shift  # drop "transcribe"
  if [[ $# -eq 0 ]]; then
    say "usage: ./run.sh transcribe FILE [transcribe_file.py args]"
    return 1
  fi
  exec "$VENV_PY" -u -m local_scribe.asr.transcribe_file "$@"
}

cmd_firewall() {
  shift  # drop "firewall"
  local sub="${1:-status}"
  shift || true

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  case "$sub" in
    status|list|verify|mode)
      # Read-only paths -- no sudo needed.
      exec "$VENV_PY" -m local_scribe.egress.firewall "$sub" "$@"
      ;;
    enable|disable)
      # Two modes:
      #   * process (default) — no sudo. Sets up the per-Char proxy
      #     enforcement. We don't need to do anything special here:
      #     just forward to python which will print the "launch Char
      #     via the wrapper" message.
      #   * system            — sudo required. Edits /etc/hosts.
      # Detect whether --mode system was passed so we can show the
      # sudo banner only in that case.
      local needs_sudo=0
      for arg in "$@"; do
        if [[ "$arg" == "system" || "$arg" == "--mode=system" ]]; then
          needs_sudo=1; break
        fi
      done
      if [[ $needs_sudo -eq 1 ]]; then
        printf "%s--mode system edits /etc/hosts, which requires admin privileges.%s\n" "$c_bold" "$c_reset"
        printf "  We'll either prompt for your sudo password (if you ran this in a\n"
        printf "  terminal) or pop the macOS admin dialog (if not). The diff is\n"
        printf "  preserved verbatim in /etc/hosts.local_scribe.bak.* so you can\n"
        printf "  always inspect or roll back what we changed.\n"
        printf "  Note: --mode system affects EVERY app on this machine, not just Char.\n"
        printf "\n"
      fi
      exec "$VENV_PY" -m local_scribe.egress.firewall "$sub" "$@"
      ;;
    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh firewall {status|enable|disable|list|verify|mode} [--strict] [--mode MODE]

Subcommands:
  status         show current /etc/hosts coverage (no sudo)
  list           print the would-be-blocked host catalog (no sudo)
  mode           show the effective enforcement mode (process vs system)
  enable         install the block list
                 [--mode process] (default) per-Char filtering via the local
                                  egress proxy (no sudo; only affects Char).
                                  Pairs with ./run.sh char launch.
                 [--mode system]  machine-wide block via /etc/hosts (sudo;
                                  affects ALL apps on this machine).
                 [--strict]       also block api.char.com (calendars, OAuth)
                 [--gui]          force the AppleScript admin dialog
                 [--no-backup]    skip the /etc/hosts.local_scribe.bak.* file
  disable        remove the block list (autodetects mode if --mode omitted)
  verify         DNS-probe the catalog and report what's actually blocked

The default per-Char mode does NOT touch /etc/hosts -- it relies on the
sandbox + egress proxy that ./run.sh char launch wires up. See SECURITY.md
for the full threat model.
EOF
      ;;
    *)
      say "unknown firewall subcommand: $sub"
      say "  run \`./run.sh firewall help\` for usage"
      return 1
      ;;
  esac
}

cmd_key() {
  shift  # drop "key"
  local sub="${1:-status}"
  shift || true

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  # SIP gate on EVERY key operation except read-only status. The
  # split-key unlock reconstitutes the master key in process memory;
  # without SIP another user-space process can read those bytes
  # straight out of our heap. Even `status` reads no secrets but
  # we leave it ungated so the operator can introspect from a
  # SIP-disabled box prior to fixing things.
  case "$sub" in
    status|backups|backup|-h|--help|help|"") ;;
    *) sip_gate || return 1 ;;
  esac

  # All key-lifecycle CLI lives in ``python -m local_scribe.security.key_lifecycle <cmd>``
  # so secrets (passphrase, master key) flow over stdin / Keychain
  # ACL only -- never argv, never env. The shell handles UX (TTY
  # prompts, colour, confirmation) and forwards bytes via pipes.

  case "$sub" in
    status)
      exec "$VENV_PY" -m local_scribe.security.key_lifecycle status
      ;;
    init)
      # First-time setup. Walks the user through:
      #   1. YubiKey enrollment (if not already enrolled)
      #   2. Master-key generation + split
      #   3. kc_half → Keychain (Touch ID ACL attached)
      #   4. yk_half → age file encrypted to the YubiKey
      #   5. Optional: passphrase-encrypted disaster-recovery backup
      local want_dr="yes"
      local force_mode="no"
      local cli_args=()
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --no-dr) want_dr="no"; cli_args+=("--no-dr"); shift ;;
          --force) force_mode="yes"; cli_args+=("--force"); shift ;;
          *) say "unknown key init flag: $1"; return 1 ;;
        esac
      done

      # --force overwrites an existing v2 install. The OLD halves
      # vanish; any data encrypted under the old key (vault, audio,
      # transcripts) is unreadable unless the DR passphrase is
      # known. Demand a typed confirmation BEFORE running the CLI
      # so the operator sees the warning at the latest possible
      # moment, after every prior step.
      if [[ "$force_mode" == "yes" ]] && "$VENV_PY" -c '
import sys
from local_scribe.security import secret_store
sys.exit(0 if secret_store.has_kc_half() else 1)
' 2>/dev/null; then
        printf "\n%s━━━ DANGER: --force will overwrite an existing v2 install ━━━%s\n" \
               "$c_red" "$c_reset"
        printf "%s  This destroys the current master key. Vault data + audio +\n" "$c_red"
        printf "  transcripts encrypted under it become permanently unreadable\n"
        printf "  unless you can DR-restore from your disaster-recovery passphrase.%s\n" "$c_reset"
        printf "\n  A pre-flight snapshot of the old halves will be written to:\n"
        printf "    %s%s/.config/local_scribe/key-backups/<ts>-init-force/%s\n" \
               "$c_dim" "$HOME" "$c_reset"
        printf "  You can roll back with %s./run.sh key backups restore <id>%s.\n" \
               "$c_bold" "$c_reset"
        printf "\n  Physical presence: you will be asked to tap the CURRENT YubiKey\n"
        printf "  to prove ownership before the snapshot is taken.\n\n"
        printf "  Type %sREPLACE%s to confirm: " "$c_bold" "$c_reset"
        local fconfirm=""
        IFS= read -r fconfirm </dev/tty
        if [[ "$fconfirm" != "REPLACE" ]]; then
          say "aborted (you must type REPLACE exactly)"
          return 1
        fi
      fi

      printf "%sInitialising local_scribe master key%s\n" "$c_bold" "$c_reset"
      printf "  This will generate a 256-bit master key, split it via XOR into\n"
      printf "  two halves, and persist:\n"
      printf "    1. kc_half  → macOS Keychain (Touch ID-gated)\n"
      printf "    2. yk_half  → age-encrypted file on disk (YubiKey-decryptable)\n"
      if [[ "$want_dr" == "yes" ]]; then
        printf "    3. master   → passphrase-encrypted age file (disaster recovery)\n"
      fi
      printf "\n  Insert your YubiKey before continuing.\n\n"

      local dr_pass=""
      if [[ "$want_dr" == "yes" ]]; then
        printf "  Disaster-recovery passphrase (won't echo; press Enter to skip): "
        IFS= read -rs dr_pass </dev/tty
        printf "\n"
        if [[ -n "$dr_pass" ]]; then
          printf "  Re-enter to confirm: "
          local dr_pass2=""
          IFS= read -rs dr_pass2 </dev/tty
          printf "\n"
          if [[ "$dr_pass" != "$dr_pass2" ]]; then
            say "${c_red}passphrases do not match; aborting${c_reset}"
            unset dr_pass dr_pass2
            return 1
          fi
          unset dr_pass2
        fi
      fi

      # Pipe the passphrase (possibly empty) to the CLI on stdin; the
      # CLI treats empty stdin as "no DR backup". The passphrase
      # never appears in argv or the environment.
      if ! printf '%s' "$dr_pass" | "$VENV_PY" -m local_scribe.security.key_lifecycle init "${cli_args[@]}"; then
        unset dr_pass
        say "${c_red}key init failed${c_reset}"
        return 1
      fi
      unset dr_pass
      if [[ "$want_dr" == "yes" ]]; then
        printf "\n  %s● master key initialised%s (with disaster-recovery backup)\n" "$c_green" "$c_reset"
      else
        printf "\n  %s● master key initialised%s (no DR backup -- consider re-running with --dr later)\n" "$c_green" "$c_reset"
      fi
      ;;
    unlock)
      exec "$VENV_PY" -m local_scribe.security.key_lifecycle unlock
      ;;
    rotate)
      printf "%sRotating master key%s\n" "$c_bold" "$c_reset"
      printf "  This unlocks the current key (Touch ID + YubiKey tap), draws a\n"
      printf "  fresh master, re-splits, and replaces both halves.\n\n"
      printf "  %sDATA-LOSS WARNING:%s any vault/keybag encrypted under the OLD\n" \
             "$c_yellow" "$c_reset"
      printf "  master key must be re-encrypted before the old key is forgotten.\n"
      printf "  The vault re-key step is not yet wired; for now rotation is a\n"
      printf "  developer smoke test of the split-key plumbing.\n\n"
      printf "  Safety mechanisms applied automatically:\n"
      printf "    1. YubiKey tap is required to unlock the CURRENT key (proves\n"
      printf "       physical possession).\n"
      printf "    2. A pre-flight snapshot of both halves + DR file is written\n"
      printf "       to ~/.config/local_scribe/key-backups/<ts>-rotate/ so the\n"
      printf "       rotation is reversible until you prune the snapshot.\n\n"
      printf "  Type %sROTATE%s to confirm: " "$c_bold" "$c_reset"
      local rconfirm=""
      IFS= read -r rconfirm </dev/tty
      if [[ "$rconfirm" != "ROTATE" ]]; then
        say "aborted (you must type ROTATE exactly)"
        return 1
      fi
      exec "$VENV_PY" -m local_scribe.security.key_lifecycle rotate
      ;;
    add-yubikey|add_yubikey)
      # Enroll a second YubiKey so either YubiKey can decrypt yk_half.
      if [[ $# -lt 1 ]]; then
        say "usage: ./run.sh key add-yubikey age1yubikey1..."
        say "  obtain the recipient via:"
        say "    age-plugin-yubikey --generate --slot 1 --name local_scribe_backup"
        return 1
      fi
      exec "$VENV_PY" -m local_scribe.security.key_lifecycle add-yubikey "$1"
      ;;
    dr-restore|dr_restore)
      printf "%sDisaster-recovery restore%s\n" "$c_bold" "$c_reset"
      printf "  Reads the passphrase-encrypted master key on disk and (by\n"
      printf "  default) re-initialises the split-key flow with a fresh\n"
      printf "  kc_half + yk_half so routine unlock works again.\n\n"

      # Detect a live v2 install BEFORE asking for the passphrase --
      # the operator's confirmation is op-specific.
      local live_v2="no"
      if "$VENV_PY" -c '
import sys
from local_scribe.security import secret_store
from local_scribe.security import yubikey_backup
sys.exit(0 if secret_store.has_kc_half() or yubikey_backup.has_yk_half() else 1)
' 2>/dev/null; then
        live_v2="yes"
      fi
      local cli_args=()
      if [[ "$live_v2" == "yes" ]]; then
        printf "%s━━━ A LIVE v2 INSTALL IS ALREADY ON THIS MACHINE ━━━%s\n" \
               "$c_red" "$c_reset"
        printf "  Restoring from DR will overwrite the live kc_half + yk_half\n"
        printf "  with halves derived from the DR passphrase. If your DR file\n"
        printf "  is older than the live key (you rotated since the DR was\n"
        printf "  written), any data encrypted under the live key will become\n"
        printf "  permanently unreadable.\n\n"
        printf "  Safety mechanisms applied automatically:\n"
        printf "    1. YubiKey tap is required to prove ownership of the live\n"
        printf "       install before it's overwritten.\n"
        printf "    2. A pre-flight snapshot of the live halves + DR file is\n"
        printf "       written to ~/.config/local_scribe/key-backups/<ts>-\n"
        printf "       dr-restore-overwrite/.\n\n"
        printf "  If you are recovering because you LOST the YubiKey, the tap\n"
        printf "  check will fail. Run with %s--no-reinit%s to skip the\n" \
               "$c_bold" "$c_reset"
        printf "  re-initialisation (you'll only get the in-memory key for\n"
        printf "  this session; routine unlock won't work until you re-enroll).\n\n"
        printf "  Type %sRESTORE-AND-OVERWRITE%s to confirm: " "$c_bold" "$c_reset"
        local drconfirm=""
        IFS= read -r drconfirm </dev/tty
        if [[ "$drconfirm" != "RESTORE-AND-OVERWRITE" ]]; then
          say "aborted (you must type RESTORE-AND-OVERWRITE exactly)"
          return 1
        fi
        cli_args+=(--overwrite-existing-v2)
      fi

      # Optional: ``--no-reinit`` for the lost-YubiKey recovery path.
      if [[ "${1:-}" == "--no-reinit" ]]; then
        cli_args+=(--no-reinit)
      fi

      printf "\n  Passphrase (won't echo): "
      local dr_pass=""
      IFS= read -rs dr_pass </dev/tty
      printf "\n"
      if [[ -z "$dr_pass" ]]; then
        say "${c_red}empty passphrase; aborting${c_reset}"
        return 1
      fi
      if ! printf '%s' "$dr_pass" | "$VENV_PY" -m local_scribe.security.key_lifecycle dr-restore "${cli_args[@]}"; then
        unset dr_pass
        say "${c_red}dr-restore failed (wrong passphrase? missing DR file?)${c_reset}"
        return 1
      fi
      unset dr_pass
      printf "\n  %s● master key recovered%s\n" "$c_green" "$c_reset"
      ;;
    migrate)
      exec "$VENV_PY" -m local_scribe.security.key_lifecycle migrate
      ;;
    backups|backup)
      # `./run.sh key backups {list|prune <id>|restore-kc-half <account>}`.
      # The "restore" subcommand replays the rollback cookbook for a
      # given snapshot. We DON'T allow restoring yk_half.age via this
      # surface (just `cp` it back yourself from the snapshot dir) --
      # giving us a one-shot button to overwrite live keys with backup
      # values is the opposite of what we want here.
      local bsub="${1:-list}"
      shift 2>/dev/null || true
      case "$bsub" in
        list|ls)
          printf "%sKey backup snapshots%s  (newest first)\n\n" "$c_bold" "$c_reset"
          "$VENV_PY" - <<'PY'
import json, sys
from local_scribe.security import key_safety
rows = []
for b in key_safety.list_backups():
    rows.append((b.id, b.label, b.scope.value, b.iso_timestamp, len(b.artefacts), b.kc_half_backup_account or "—"))
if not rows:
    print("  (no snapshots — destructive ops haven't been run yet)")
    sys.exit(0)
print(f"  {'id':40s}  {'label':22s}  {'scope':14s}  {'artefacts':9s}  kc_half_backup_account")
for r in rows:
    print(f"  {r[0]:40s}  {r[1]:22s}  {r[2]:14s}  {r[4]:9d}  {r[5]}")
PY
          ;;
        prune)
          if [[ $# -lt 1 ]]; then
            say "usage: ./run.sh key backups prune <snapshot-id>"
            return 1
          fi
          local snap_id="$1"
          printf "%s━━━ Pruning snapshot %q ━━━%s\n" "$c_red" "$snap_id" "$c_reset"
          printf "  This removes the snapshot directory + the associated Keychain\n"
          printf "  backup account (if any). After this point the rollback path\n"
          printf "  for that operation no longer exists.\n\n"
          printf "  Type %sDELETE%s to confirm: " "$c_bold" "$c_reset"
          local dconfirm=""
          IFS= read -r dconfirm </dev/tty
          if [[ "$dconfirm" != "DELETE" ]]; then
            say "aborted (you must type DELETE exactly)"
            return 1
          fi
          exec "$VENV_PY" -m local_scribe.security.key_safety prune "$snap_id"
          ;;
        restore-kc-half)
          if [[ $# -lt 1 ]]; then
            say "usage: ./run.sh key backups restore-kc-half <keychain-account>"
            return 1
          fi
          local kc_account="$1"
          printf "%s━━━ Restoring kc_half from %q ━━━%s\n" "$c_yellow" "$kc_account" "$c_reset"
          printf "  This overwrites the live kc_half in the Keychain with the\n"
          printf "  value from the backup account. yk_half.age is NOT touched\n"
          printf "  by this command — copy it back from the snapshot dir if\n"
          printf "  you need to.\n\n"
          printf "  You'll be prompted for Touch ID to read the backup account.\n\n"
          printf "  Type %sRESTORE%s to confirm: " "$c_bold" "$c_reset"
          local rsconfirm=""
          IFS= read -r rsconfirm </dev/tty
          if [[ "$rsconfirm" != "RESTORE" ]]; then
            say "aborted (you must type RESTORE exactly)"
            return 1
          fi
          exec "$VENV_PY" -m local_scribe.security.key_safety restore-kc-half "$kc_account"
          ;;
        -h|--help|help|"")
          cat <<EOF
usage: ./run.sh key backups {list|prune <id>|restore-kc-half <account>}

list                          list all backup snapshots (newest first)
prune <id>                    delete a snapshot dir + its Keychain backup
                              account (requires typed DELETE)
restore-kc-half <account>     copy the named Keychain backup account back
                              into the live kc_half (requires typed
                              RESTORE + Touch ID).
EOF
          ;;
        *) say "unknown backups subcommand: $bsub"; return 1 ;;
      esac
      ;;
    destroy)
      # Two modes:
      #   default              keep the pre-flight backup snapshot so destroy
      #                        is reversible. Requires YubiKey tap.
      #   --purge-everything   ALSO delete every prior backup snapshot. True
      #                        zero state. Requires a second typed confirm
      #                        AND a YubiKey tap.
      local destroy_args=()
      local purge_everything="no"
      local no_presence="no"
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --purge-everything|--purge) purge_everything="yes"; shift ;;
          --no-presence)              no_presence="yes"; destroy_args+=("--no-presence"); shift ;;
          *) say "unknown destroy flag: $1"; return 1 ;;
        esac
      done

      printf "%s━━━ DANGER: destroying every master-key artefact ━━━%s\n" \
             "$c_red" "$c_reset"
      printf "  All vault / audio / transcripts encrypted under the master key\n"
      printf "  will become permanently unreadable unless you have:\n"
      printf "    *  A YubiKey-decryptable copy on a different machine, OR\n"
      printf "    *  Your disaster-recovery passphrase + the DR file, OR\n"
      if [[ "$purge_everything" == "no" ]]; then
        printf "    *  The pre-flight snapshot at\n"
        printf "       ~/.config/local_scribe/key-backups/<ts>-destroy/\n"
        printf "       (default behaviour: snapshot is kept until you explicitly\n"
        printf "       run %s./run.sh key backups prune%s).\n\n" "$c_bold" "$c_reset"
      else
        printf "    %s━ NONE: --purge-everything also wipes the snapshots ━%s\n\n" \
               "$c_red" "$c_reset"
      fi
      printf "  Physical presence: you will be asked to tap the YubiKey to\n"
      printf "  prove ownership before anything is deleted"
      if [[ "$no_presence" == "yes" ]]; then
        printf " (skipped: --no-presence).\n"
        printf "    %sNOTE: --no-presence is only for the case where you lost the\n" "$c_yellow"
        printf "    YubiKey and want to wipe the now-useless artefacts.%s\n" "$c_reset"
      else
        printf ".\n"
      fi
      printf "\n  Type %sDESTROY%s to confirm: " "$c_bold" "$c_reset"
      local confirm=""
      IFS= read -r confirm </dev/tty
      if [[ "$confirm" != "DESTROY" ]]; then
        say "aborted (you must type DESTROY exactly)"
        return 1
      fi

      if [[ "$purge_everything" == "yes" ]]; then
        printf "\n  %s━━━ second confirmation required ━━━%s\n" "$c_red" "$c_reset"
        printf "  --purge-everything will ALSO delete every backup snapshot under\n"
        printf "    ~/.config/local_scribe/key-backups/\n"
        printf "  This is the only key-management operation that is COMPLETELY\n"
        printf "  irreversible -- no rollback is possible after this point.\n\n"
        printf "  Type %sPURGE-EVERYTHING%s to proceed: " "$c_bold" "$c_reset"
        local pconfirm=""
        IFS= read -r pconfirm </dev/tty
        if [[ "$pconfirm" != "PURGE-EVERYTHING" ]]; then
          say "aborted (you must type PURGE-EVERYTHING exactly)"
          return 1
        fi
        # Force --no-backup on the underlying CLI because we're
        # about to wipe the backups dir entirely anyway.
        destroy_args+=("--no-backup")
      fi

      "$VENV_PY" -m local_scribe.security.key_lifecycle destroy "${destroy_args[@]}" || {
        say "${c_red}destroy failed${c_reset}"
        return 1
      }

      if [[ "$purge_everything" == "yes" ]]; then
        local backups_dir="$HOME/.config/local_scribe/key-backups"
        if [[ -d "$backups_dir" ]]; then
          rm -rf "$backups_dir"
          printf "  also purged: %s%s%s\n" "$c_dim" "$backups_dir" "$c_reset"
        fi
        # Also delete any Keychain backup accounts.
        "$VENV_PY" - <<'PY'
import subprocess
from local_scribe.security import secret_store
# List Keychain items matching our backup-account prefix and delete each.
prefix = "master_key_kc_half_v2_backup_"
v1_prefix = "master_key_v1_backup_"
# We don't have a list API; iterate via `security find-generic-password`.
out = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
import re
accts = set()
for m in re.finditer(r'"acct"<blob>="([^"]+)"', out):
    a = m.group(1)
    if a.startswith(prefix) or a.startswith(v1_prefix):
        accts.add(a)
for a in accts:
    try:
        secret_store._delete_item(account=a)
        print(f"  purged Keychain backup account: {a}")
    except Exception as exc:
        print(f"  could not delete {a}: {exc}")
PY
      fi
      ;;
    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh key {status|init|unlock|rotate|add-yubikey|dr-restore|migrate|destroy|backups}

Subcommands:
  status         JSON snapshot of the key lifecycle (no Touch ID / no YubiKey)
  init           first-time setup: enroll YubiKey, split key, persist halves
                   --no-dr     skip the passphrase-encrypted DR backup
                   --force     replace an existing v2 install — requires
                               typed REPLACE confirmation + YubiKey tap +
                               pre-flight snapshot
  unlock         smoke-test the unlock path; prints service-token fingerprints
  rotate         generate a fresh master key + rewrite both halves
                   requires typed ROTATE + YubiKey tap (via unlock of OLD)
                   + pre-flight snapshot of both halves
  add-yubikey R  enroll a second YubiKey (R is its age recipient); re-wraps
                 yk_half so either YubiKey can decrypt it
                   requires YubiKey tap on CURRENT key + pre-flight snapshot
                   of yk_half.age
  dr-restore     recover the master key from the on-disk DR file via passphrase
                   when a live v2 install exists: requires typed
                   RESTORE-AND-OVERWRITE + YubiKey tap + pre-flight snapshot
                   --no-reinit   skip re-init of split-key flow (lost-yubikey
                                 path; you get the master in-memory only)
  migrate        walk a legacy v1 (whole-key Keychain) install over to v2
                   pre-flight snapshot of the v1 Keychain item is always taken
  destroy        delete every key artefact (irreversible)
                   requires typed DESTROY + YubiKey tap + pre-flight snapshot
                   --purge-everything   also deletes every prior snapshot;
                                        requires a SECOND typed
                                        PURGE-EVERYTHING confirm
                   --no-presence        skip the YubiKey tap (lost-yubikey
                                        path)
  backups        list / prune / restore from pre-flight snapshots
                   list                  show snapshots newest-first
                   prune <id>            delete one snapshot (typed DELETE)
                   restore-kc-half X     promote a Keychain backup account
                                         back to the live kc_half (typed
                                         RESTORE + Touch ID)

Safety invariants:
  1. Every destructive op requires physical possession of the CURRENT YubiKey
     (a tap, before any state changes). The only ops that skip this are
     dr-restore --no-reinit (you've lost the key — that's the recovery path)
     and destroy --no-presence (same).
  2. Every destructive op writes a pre-flight snapshot to
     ~/.config/local_scribe/key-backups/<ts>-<op>/ BEFORE mutating state.
     The snapshot contains the about-to-be-replaced yk_half.age, the
     disaster_recovery.age, and a copy of kc_half in a versioned Keychain
     backup account. ALWAYS roll-back-recoverable.
  3. Snapshots are NEVER auto-pruned. The operator must explicitly run
     \`./run.sh key backups prune <id>\` to dispose of one.
  4. The only zero-state op is \`destroy --purge-everything\`, which
     requires TWO typed confirmations (DESTROY and PURGE-EVERYTHING) plus
     a YubiKey tap. Even \`destroy\` alone keeps the snapshot.

All passphrases are read from /dev/tty (no echo, never on argv). All
master-key bytes are passed via stdin / Keychain ACL — they never
appear in process listings, environment variables, or logs.

See KEY_SAFETY.md for the complete enumeration of data-loss scenarios
and the recovery flowcharts. See SECURITY.md and ARCHITECTURE.md §4
for the broader threat model.
EOF
      ;;
    *)
      say "unknown key subcommand: $sub"
      say "  run \`./run.sh key help\` for usage"
      return 1
      ;;
  esac
}


# `./run.sh vault {init|unlock|lock|status}` — operator-facing surface
# for vault.py + vault_unlock.py. All key-touching subcommands are
# gated on SIP (because the master key is reconstituted in process
# memory; without SIP another user-space process could read it).
# `status` is the only ungated subcommand; it does no decryption and
# is useful for diagnostics on a misconfigured box.
cmd_vault() {
  shift  # drop "vault"
  local sub="${1:-status}"
  shift || true

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  case "$sub" in
    status|-h|--help|help|"") ;;
    *) sip_gate || return 1 ;;
  esac

  case "$sub" in
    status)
      exec "$VENV_PY" -m local_scribe.security.vault_unlock status
      ;;
    init)
      # First-time creation of the encrypted sparse bundle. We prompt
      # for Touch ID + YubiKey tap inside vault_unlock; no extra
      # confirmation is needed (this is non-destructive — if a vault
      # already exists, vault.create raises and we report it).
      printf "%sInitialising encrypted vault%s\n" "$c_bold" "$c_reset"
      printf "  Creates ~/Library/Application Support/local_scribe-vault.sparsebundle\n"
      printf "  (AES-256 sparse bundle, 100 GB ceiling, grows on demand).\n"
      printf "  hdiutil passphrase is HKDF-derived from the master key, so\n"
      printf "  every unlock requires Touch ID + a YubiKey tap.\n\n"
      printf "  Tap your YubiKey when prompted.\n\n"
      exec "$VENV_PY" -m local_scribe.security.vault_unlock init "$@"
      ;;
    unlock|mount)
      # Idempotent: returns the mount path if already mounted. Always
      # relocates Char's data dir into the vault on first unlock.
      local relocate_arg=()
      if [[ "${1:-}" == "--no-relocate" ]]; then
        relocate_arg+=("--no-relocate")
        shift
      fi
      printf "%sUnlocking vault%s — Touch ID + YubiKey tap required\n" \
             "$c_bold" "$c_reset"
      exec "$VENV_PY" -m local_scribe.security.vault_unlock unlock "${relocate_arg[@]}"
      ;;
    lock|unmount)
      # No prompts on the lock path; we're protecting plaintext from
      # the local user, not unlocking anything new.
      exec "$VENV_PY" -m local_scribe.security.vault_unlock lock
      ;;
    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh vault {init|unlock|lock|status}

Subcommands:
  init                   create the AES-256 sparse-bundle vault. Touch ID
                         + YubiKey tap to derive the hdiutil passphrase.
                         Idempotent: no-op if a vault already exists.
  unlock [--no-relocate] mount the vault and (by default) relocate Char's
                         data dir (~/Library/Application Support/hyprnote)
                         INTO the vault, replacing it with a symlink so
                         Char keeps working transparently. --no-relocate
                         skips that step.
                         Alias: \`mount\`.
  lock                   detach the mounted sparse bundle. Idempotent.
                         Alias: \`unmount\`.
  status                 JSON snapshot of vault state (no prompts).

The vault's hdiutil passphrase is HKDF-SHA256-derived from the master
key with the label "local_scribe.vault.passphrase.v1". This means:

  * The passphrase is never written to disk or shown to the operator.
  * Unlocking the master key (Touch ID + YubiKey) is sufficient.
  * Rotating the master key (\`./run.sh key rotate\`) automatically
    re-keys the vault envelope via vault.rotate_password().

See vault.py and vault_unlock.py for the full implementation; see
SECURITY.md § 'Defense layer 4 — encrypted vault' for the threat model.
EOF
      ;;
    *)
      say "unknown vault subcommand: $sub"
      say "  run \`./run.sh vault help\` for usage"
      return 1
      ;;
  esac
}


# `./run.sh yubikey {enroll|verify|restore|status}` — operator-facing
# surface for yubikey_backup.py. Thin wrappers around the Python
# module; the heavy lifting (age-plugin-yubikey shell-outs, ykman
# probes, recipient extraction) all happens there.
cmd_config() {
  # ``./run.sh config {show,sign,verify,status}`` — operator-facing
  # surface for the pinned-config signature flow. Delegates to the
  # Python CLI; sub-verbs that mutate state (``sign``) require SIP
  # because they touch Keychain + YubiKey.
  local sub="${1:-status}"
  shift || true

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  case "$sub" in
    show|status|verify|""|-h|--help|help) ;;
    sign) sip_gate || return 1 ;;
    *)
      say "unknown: ./run.sh config $sub"
      say "  verbs: show [--shell|--json] | status | verify | sign"
      return 2
      ;;
  esac

  case "$sub" in
    ""|status)   exec "$VENV_PY" -m local_scribe config status ;;
    show)        exec "$VENV_PY" -m local_scribe config show "$@" ;;
    verify)      exec "$VENV_PY" -m local_scribe config verify ;;
    sign)        exec "$VENV_PY" -m local_scribe config sign ;;
    -h|--help|help)
      cat <<EOF
./run.sh config — signed pinned distribution constants
  show [--shell|--json]   print pinned values (default: JSON)
  status                  show signature state (no master-key unlock)
  verify                  verify HMAC; refuses to start if invalid
  sign                    bless current pinned.json with Touch ID + YubiKey

The pinned data lives at local_scribe/common/pinned.json. After editing
that file (e.g. bumping CHAR_KNOWN_GOOD_VERSION), run \`./run.sh config sign\`
to re-bless. The server refuses to start with an invalid signature; see
SECURITY.md § "Defense layer 6 — Signed pinned config" for the threat model.
EOF
      return 0
      ;;
  esac
}

cmd_yubikey() {
  shift  # drop "yubikey"
  local sub="${1:-status}"
  shift || true

  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing — run \`./run.sh bootstrap\` first${c_reset}"
    return 1
  fi

  # YubiKey-touching subcommands need SIP (they invoke age-plugin-yubikey
  # which materialises secrets in our memory space). `status` is read-
  # only metadata and is ungated for the same reason `key status` is.
  case "$sub" in
    status|list|recipients|-h|--help|help|"") ;;
    *) sip_gate || return 1 ;;
  esac

  case "$sub" in
    status)
      exec "$VENV_PY" - <<'PY'
import json
from local_scribe.security import yubikey_backup
print(json.dumps(yubikey_backup.status(), indent=2))
PY
      ;;
    enroll)
      # Two distinct paths:
      #   * fresh enroll (no v2 install yet)        -> defer to `./run.sh key init`
      #   * additional enroll (second/backup YubiKey) -> `./run.sh key add-yubikey`
      # If a v2 install IS present and the operator runs `yubikey enroll`
      # standalone, we'll prompt for the recipient + chain into the
      # `key add-yubikey` flow. We do NOT silently regenerate the slot
      # of an already-enrolled YubiKey -- that would brick the
      # decryption of the current yk_half.age.
      printf "%sEnrolling a YubiKey%s\n" "$c_bold" "$c_reset"
      printf "  Insert the YubiKey you want to enroll, then press Enter.\n"
      printf "  (PIV slot 1 will be initialised with a fresh age identity.)\n\n"
      local _ack
      IFS= read -r _ack </dev/tty || true
      # Detect whether the master key already exists. If not, we run
      # the full `key init` flow; this also handles the case where a
      # user runs `yubikey enroll` as their FIRST action.
      if "$VENV_PY" -c '
import sys
from local_scribe.security import secret_store
sys.exit(0 if (secret_store.has_kc_half() or secret_store.has_master_key()) else 1)
' 2>/dev/null; then
        # Master key exists -> this is a "register an additional
        # YubiKey to the existing yk_half.age". Generate a fresh
        # identity on the inserted key, then hand its recipient to
        # `key add-yubikey`. We deliberately don't pass --force so
        # an already-enrolled key isn't wiped accidentally.
        printf "  Generating age identity on the inserted YubiKey (slot 1, touch=cached)...\n"
        if ! age-plugin-yubikey \
              --generate --slot 1 \
              --pin-policy once --touch-policy cached \
              --name local_scribe_secondary 2>&1; then
          say "${c_red}age-plugin-yubikey --generate failed${c_reset}"
          say "  remove + reinsert the YubiKey and try again, or run:"
          say "    age-plugin-yubikey --generate --slot 1 --name local_scribe_secondary"
          return 1
        fi
        printf "\n  Find the new recipient (starts with 'age1yubikey1...') in the output\n"
        printf "  above, then run:\n"
        printf "    %s./run.sh key add-yubikey <recipient>%s\n" "$c_bold" "$c_reset"
      else
        printf "  No master key yet — running the full key-init flow with this YubiKey.\n\n"
        # Chain into cmd_key. Subshell isolates any `exec` it might
        # do, so a caller of `cmd_yubikey` retains control.
        if ! ( cmd_key key init ); then
          return 1
        fi
      fi
      ;;
    verify|tap-test)
      # Round-trip test: decrypt yk_half.age via the inserted YubiKey.
      # If it returns 32 bytes we're good. We do NOT print the bytes
      # themselves; the test passes or fails on exit code + a short
      # confirmation line.
      printf "%sVerifying YubiKey tap%s — touch the key when it blinks\n" \
             "$c_bold" "$c_reset"
      "$VENV_PY" - <<'PY'
import sys
from local_scribe.security import yubikey_backup
try:
    yk = yubikey_backup.restore_yk_half(
        on_touch_prompt=lambda msg: print(f"  [yubikey] {msg}", file=sys.stderr),
    )
except yubikey_backup.YubiKeyError as exc:
    print(f"  FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(yk, (bytes, bytearray)) or len(yk) != 32:
    print(f"  FAIL: yk_half had wrong length: {len(yk)}", file=sys.stderr)
    sys.exit(1)
print(f"  OK: decrypted yk_half ({len(yk)} bytes) via inserted YubiKey")
PY
      ;;
    restore)
      # Restore yk_half.age from a key-safety snapshot. Useful when:
      #   * yk_half.age was accidentally deleted
      #   * disk corruption munged the file
      #   * the user wants to roll back to a prior age-recipient set
      # The snapshot id comes from `./run.sh key backups list`.
      if [[ $# -lt 1 ]]; then
        say "usage: ./run.sh yubikey restore <snapshot-id>"
        say "  list available snapshots with: ./run.sh key backups list"
        return 1
      fi
      local snap_id="$1"
      printf "%s━━━ Restoring yk_half.age from snapshot %q ━━━%s\n" \
             "$c_yellow" "$snap_id" "$c_reset"
      printf "  This overwrites the current yk_half.age (and the recipients\n"
      printf "  file, if present in the snapshot) with the values from the\n"
      printf "  named pre-flight snapshot. If the master key has rotated\n"
      printf "  since the snapshot was taken, the restored half will no\n"
      printf "  longer XOR with the live kc_half — vault becomes unreadable.\n\n"
      printf "  Recommended: take a fresh snapshot first via\n"
      printf "    %s./run.sh key rotate%s    (no-op rotate is fine: just creates a snapshot)\n\n" \
             "$c_bold" "$c_reset"
      printf "  Type %sRESTORE%s to confirm: " "$c_bold" "$c_reset"
      local rconfirm=""
      IFS= read -r rconfirm </dev/tty
      if [[ "$rconfirm" != "RESTORE" ]]; then
        say "aborted (you must type RESTORE exactly)"
        return 1
      fi
      "$VENV_PY" - "$snap_id" <<'PY'
import shutil, sys
from local_scribe.security import key_safety
from local_scribe.security import yubikey_backup
sid = sys.argv[1]
matches = [b for b in key_safety.list_backups() if b.id == sid or b.id.startswith(sid)]
if not matches:
    print(f"  no snapshot matches {sid!r}; run `./run.sh key backups list`",
          file=sys.stderr)
    sys.exit(1)
if len(matches) > 1:
    print(f"  ambiguous: {len(matches)} snapshots match {sid!r}; use full id",
          file=sys.stderr)
    sys.exit(1)
snap = matches[0]
snap_yk_half = snap.dir / "yk_half.age"
if not snap_yk_half.is_file():
    print(f"  snapshot {snap.id} does not contain yk_half.age "
          f"(scope={snap.scope.value}); pick a different one", file=sys.stderr)
    sys.exit(1)
yubikey_backup.YK_HALF_PATH.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(snap_yk_half, yubikey_backup.YK_HALF_PATH)
print(f"  restored yk_half.age from {snap.id}")
snap_recipients = snap.dir / "yubikey_recipients.txt"
if snap_recipients.is_file():
    shutil.copy2(snap_recipients, yubikey_backup.RECIPIENTS_PATH)
    print(f"  restored yubikey_recipients.txt from {snap.id}")
print(f"  smoke-test the result with: ./run.sh yubikey verify")
PY
      ;;
    list|recipients)
      "$VENV_PY" - <<'PY'
from local_scribe.security import yubikey_backup
rs = yubikey_backup.list_recipients()
if not rs:
    print("  (no YubiKey recipients enrolled — run `./run.sh key init`)")
else:
    print(f"  {len(rs)} enrolled YubiKey recipient(s):")
    for i, r in enumerate(rs, 1):
        print(f"    [{i}] {r}")
PY
      ;;
    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh yubikey {enroll|verify|restore|list|status}

Subcommands:
  status                 JSON status: tools present, key inserted, enrolled,
                         recipient count, yk_half.age presence/size.
  list                   show all enrolled age recipients (one line each).
                         Alias: \`recipients\`.
  enroll                 generate an age identity on the inserted YubiKey's
                         PIV slot. If no master key exists yet, this chains
                         into \`key init\` (full first-time enrollment).
                         If a master key already exists, this is the path
                         for adding a SECOND/BACKUP YubiKey -- the command
                         prints the new recipient and tells you to run
                         \`./run.sh key add-yubikey <recipient>\`.
  verify                 round-trip test: decrypt yk_half.age with the
                         inserted YubiKey. Verifies the tap path is live
                         without touching any state.  Alias: \`tap-test\`.
  restore <snap-id>      copy yk_half.age (and yubikey_recipients.txt) from
                         a pre-flight backup snapshot. Use after accidentally
                         deleting / corrupting yk_half.age. Requires typed
                         RESTORE confirmation. List snapshots with:
                           \`./run.sh key backups list\`

See KEY_SAFETY.md for the data-loss recovery flowcharts. See
yubikey_backup.py for the full implementation.
EOF
      ;;
    *)
      say "unknown yubikey subcommand: $sub"
      say "  run \`./run.sh yubikey help\` for usage"
      return 1
      ;;
  esac
}


# --- egress_proxy operator surface ------------------------------------------
#
# Thin pass-through to ``python -m local_scribe.egress.egress_proxy``
# plus the start / stop / restart pid-file lifecycle that runs alongside the
# ASR + Inspector services. Lets operators inspect the proxy's audit
# state without remembering the python invocation.

cmd_egress_proxy() {
  local sub="${1:-status}"
  shift 2>/dev/null || true
  case "$sub" in
    start)
      egress_proxy_start
      ;;
    stop)
      egress_proxy_stop
      ;;
    restart)
      egress_proxy_stop
      egress_proxy_start
      ;;
    status)
      if egress_proxy_pid >/dev/null; then
        printf "  "; ok; printf "egress proxy    pid=%-7s :%s\n" \
                               "$(egress_proxy_pid)" "$EGRESS_PROXY_PORT"
      else
        printf "  "; bad; printf "egress proxy    not running\n"
      fi
      "$VENV_PY" -m local_scribe.egress.egress_proxy status 2>/dev/null || true
      ;;
    verify)
      # Send a synthetic CONNECT to a known-blocked host and assert
      # the proxy refuses it. Used by doctor and CI smoke tests.
      "$VENV_PY" -m local_scribe.egress.egress_proxy verify "$@"
      ;;
    recent|tail)
      # Tail the proxy log for recent decisions.
      if [[ -f "$EGRESS_PROXY_LOG_FILE" ]]; then
        /usr/bin/grep -E ' (DENY|ALLOW|ERROR) ' "$EGRESS_PROXY_LOG_FILE" 2>/dev/null \
          | /usr/bin/tail -n 20 \
          || say "(no decisions logged yet)"
      else
        say "no egress proxy log file at $EGRESS_PROXY_LOG_FILE"
      fi
      ;;
    log|logs)
      if [[ -f "$EGRESS_PROXY_LOG_FILE" ]]; then
        /usr/bin/tail -f "$EGRESS_PROXY_LOG_FILE"
      else
        say "no egress proxy log file at $EGRESS_PROXY_LOG_FILE"
      fi
      ;;
    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh proxy {start|stop|restart|status|verify|recent|log}

The egress proxy is the per-Char outbound firewall. Char is wired to
it via the HTTPS_PROXY env var injected by ./run.sh char launch, AND
contained by char_sandbox so its only network path is loopback. The
proxy refuses CONNECT requests for any host in firewall.BLOCK_CATALOG.

Subcommands:
  start     Start the proxy on :$EGRESS_PROXY_PORT (no-op if already up).
  stop      Stop the proxy. Char traffic will lose its outbound filter.
  restart   stop + start.
  status    Show pid + listening state + JSON counts.
  verify    Send a CONNECT to api.openai.com:443 and assert 403.
            Use this as a CI smoke test after edits to BLOCK_CATALOG.
  recent    Show the last 20 decisions (ALLOW / DENY / ERROR) from the
            proxy log.
  log       Tail the proxy log (Ctrl+C to detach).
EOF
      ;;
    *)
      say "unknown proxy subcommand: $sub"
      say "  run \`./run.sh proxy help\` for usage"
      return 1
      ;;
  esac
}

cmd_char() {
  # Operator-facing surface for:
  #   * Layer B (Char binary verification, char_integrity.py)
  #   * The per-Char outbound firewall (char_sandbox.py +
  #     egress_proxy.py + HTTPS_PROXY env injection).
  local sub="${1:-help}"
  shift 2>/dev/null || true
  case "$sub" in
    status|check)
      exec "$VENV_PY" -m local_scribe.char.char_integrity --check
      ;;
    fingerprint|show)
      exec "$VENV_PY" -m local_scribe.char.char_integrity --show-fingerprint
      ;;
    baseline-set|baseline)
      # First-time setup: record the current Char.app's CDHash +
      # Mach-O sha256s. ``baseline-set`` and ``baseline-update`` are
      # the same operation; the latter is just the name an operator
      # reaches for after a known-good upgrade.
      "$VENV_PY" -m local_scribe.char.char_integrity --baseline-set || return $?
      # Auto-sign so the freshly-written baseline immediately
      # satisfies the start-time signature gate. Touch ID + YubiKey
      # tap; ./run.sh config sign signs every file in the roster
      # (pinned.json + char_baseline.json), which is what we want
      # because operators bumping the baseline have likely just
      # bumped pinned.json too.
      printf "%sauto-signing pinned + baseline (Touch ID + YubiKey)…%s\n" \
             "$c_dim" "$c_reset"
      "$VENV_PY" -m local_scribe config sign || \
        say "${c_yellow}config sign failed; run \`./run.sh config sign\` manually before \`./run.sh start\`${c_reset}"
      ;;
    baseline-update)
      "$VENV_PY" -m local_scribe.char.char_integrity --baseline-update || return $?
      printf "%sauto-signing pinned + baseline (Touch ID + YubiKey)…%s\n" \
             "$c_dim" "$c_reset"
      "$VENV_PY" -m local_scribe config sign || \
        say "${c_yellow}config sign failed; run \`./run.sh config sign\` manually before \`./run.sh start\`${c_reset}"
      ;;
    baseline-clear)
      "$VENV_PY" -m local_scribe.char.char_integrity --baseline-clear
      ;;

    launch)
      # Per-Char outbound firewall: launch Char wrapped in
      # sandbox-exec (loopback-only network) with HTTPS_PROXY
      # pointing at our local egress proxy. This is the ONLY
      # supported way to start Char if you want the firewall to
      # apply -- Dock / Spotlight launches bypass both layers.
      cmd_char_launch "$@"
      ;;
    sandbox)
      # Subcommands for the SBPL profile (render / write / validate
      # / status). Forwards to char_sandbox.py.
      exec "$VENV_PY" -m local_scribe.egress.char_sandbox "$@"
      ;;
    firewall-status)
      # Quick triage view: is the proxy listening? is the sandbox
      # profile valid? is Char running, and through us?
      cmd_char_firewall_status
      ;;

    -h|--help|help|"")
      cat <<EOF
usage: ./run.sh char {launch|status|fingerprint|sandbox|firewall-status|...}

Char launch (per-Char outbound firewall):
  launch [...]        Start Char under sandbox-exec + HTTPS_PROXY so its only
                      outbound path is the local egress proxy. Trailing args
                      are forwarded to the Char binary verbatim.

Binary verification (Layer B):
  status              Verify the installed Char.app against the recorded baseline
                      (codesign --deep --strict, Gatekeeper, linked-Mach-O hashes,
                      pinned Team ID + bundle identifier).
  fingerprint         Print the JSON fingerprint of the currently-installed
                      Char.app (no comparison; useful for diffing).
  baseline-set        Record the CURRENT bundle as the trusted baseline. Only
                      run this once after a clean ./run.sh install-char.
  baseline-update     Same as baseline-set; use this label when you've just
                      verified a legitimate Char upgrade and want to roll the
                      recorded baseline forward.
  baseline-clear      Forget the recorded baseline (you'll be re-prompted to
                      set one on the next ./run.sh start).

Sandbox profile management:
  sandbox render      Print the SBPL profile that 'char launch' applies.
  sandbox write       Persist the SBPL profile to ~/.config/local_scribe/char.sb.
  sandbox validate    Smoke-test the profile against /usr/bin/true.
  sandbox status      Show profile path, presence, and parse status.

Firewall diagnostics:
  firewall-status     Combined status: proxy listening? sandbox profile valid?
                      Char running? Is the running Char going through us?

See CHAR_REVIEW.md and SECURITY.md for the full threat model. Override on a
known-bad state (DANGEROUS): LOCAL_SCRIBE_ALLOW_DIRTY_CHAR=1 ./run.sh start
EOF
      ;;
    *)
      say "unknown char subcommand: $sub"
      say "  run \`./run.sh char help\` for usage"
      return 1
      ;;
  esac
}

# --- char launch wrapper ----------------------------------------------------
#
# Composes the two macOS primitives we have ('sandbox-exec' kernel
# policy + 'HTTPS_PROXY' env var) into a per-Char outbound firewall.
# The two layers reinforce each other:
#   * The proxy enforces hostname-level blocks (firewall.BLOCK_CATALOG).
#   * The sandbox restricts Char's network reach to loopback, so even
#     a Char build that ignored HTTPS_PROXY can't bypass the proxy --
#     its only network path IS the loopback proxy.
# This function is the ONLY supported launch path if you want the
# firewall to apply. Dock / Spotlight launches inherit neither the env
# vars nor the sandbox.

cmd_char_launch() {
  if [[ ! -d "$CHAR_APP" ]]; then
    say "${c_red}Char.app is not installed at $CHAR_APP${c_reset}"
    say "  run ${c_bold}./run.sh install-char${c_reset} first."
    return 1
  fi
  local char_bin="$CHAR_APP/Contents/MacOS/hyprnote"
  if [[ ! -x "$char_bin" ]]; then
    # Some versions ship as ``Char`` rather than ``hyprnote``;
    # fall back to whatever Contents/MacOS contains.
    char_bin="$(/bin/ls "$CHAR_APP/Contents/MacOS/" 2>/dev/null | head -n1 | sed "s|^|$CHAR_APP/Contents/MacOS/|")"
    if [[ ! -x "$char_bin" ]]; then
      say "${c_red}can't find the Char binary under $CHAR_APP/Contents/MacOS${c_reset}"
      return 1
    fi
  fi
  if ! command -v /usr/bin/sandbox-exec >/dev/null 2>&1; then
    say "${c_red}/usr/bin/sandbox-exec is missing — can't enforce the firewall${c_reset}"
    say "  This macOS is unusual; you can launch Char unsandboxed from the Dock,"
    say "  but its outbound traffic will NOT be filtered."
    return 1
  fi

  # Make sure the proxy + the SBPL profile are both in place.
  if ! egress_proxy_pid >/dev/null; then
    say "egress proxy is not running; starting it now ..."
    egress_proxy_start || {
      say "${c_red}refusing to launch Char without the egress proxy${c_reset}"
      say "  see $EGRESS_PROXY_LOG_FILE for the failure reason"
      return 1
    }
  fi
  "$VENV_PY" -m local_scribe.egress.char_sandbox write >/dev/null
  local profile; profile="$(
    "$VENV_PY" -c 'from local_scribe.egress import char_sandbox; print(char_sandbox.profile_path())'
  )"
  if [[ ! -f "$profile" ]]; then
    say "${c_red}sandbox profile missing at $profile after write${c_reset}"
    return 1
  fi
  # validate_profile runs `sandbox-exec -f profile /usr/bin/true`. If
  # that succeeds, we know the kernel accepts the policy -- so the
  # imminent launch won't half-apply it.
  local validate_json
  validate_json="$("$VENV_PY" -m local_scribe.egress.char_sandbox validate 2>&1)" || {
    say "${c_red}sandbox profile failed to validate:${c_reset}"
    say "  $validate_json"
    return 1
  }

  say "${c_green}launching Char under sandbox-exec + HTTPS_PROXY=...:$EGRESS_PROXY_PORT${c_reset}"
  say "  binary  : $char_bin"
  say "  profile : $profile"
  say "  proxy   : http://127.0.0.1:$EGRESS_PROXY_PORT"
  say "  ${c_dim}detach with Ctrl+C; the sandbox + proxy stay active for the running Char${c_reset}"
  # NO_PROXY keeps our own loopback services reachable without
  # tunnelling through the proxy (which we'd also allow, but it
  # would needlessly add a hop).
  exec /usr/bin/sandbox-exec -f "$profile" \
       /usr/bin/env \
         "HTTPS_PROXY=http://127.0.0.1:$EGRESS_PROXY_PORT" \
         "HTTP_PROXY=http://127.0.0.1:$EGRESS_PROXY_PORT" \
         "NO_PROXY=127.0.0.1,localhost,::1" \
         "ALL_PROXY=http://127.0.0.1:$EGRESS_PROXY_PORT" \
         "$char_bin" "$@"
}

# --- char firewall-status ---------------------------------------------------

cmd_char_firewall_status() {
  printf "%s──── per-Char outbound firewall ────%s\n" "$c_bold" "$c_reset"

  # 1. Proxy.
  if egress_proxy_pid >/dev/null; then
    printf "  "; ok; printf "egress proxy    pid=%-7s listening on :%s\n" \
                           "$(egress_proxy_pid)" "$EGRESS_PROXY_PORT"
  else
    printf "  "; bad; printf "egress proxy    NOT RUNNING (Char traffic not filtered)\n"
  fi

  # 2. Sandbox profile.
  local sb_status
  sb_status="$("$VENV_PY" -m local_scribe.egress.char_sandbox status 2>/dev/null || echo '{}')"
  local sb_present sb_valid
  sb_present="$("$VENV_PY" -c "
import json, sys
try:
  d=json.loads('''$sb_status''')
  print('1' if d.get('profile_present') else '0')
except Exception: print('0')
" 2>/dev/null)"
  sb_valid="$("$VENV_PY" -c "
import json, sys
try:
  d=json.loads('''$sb_status''')
  print('1' if d.get('profile_valid') else '0')
except Exception: print('0')
" 2>/dev/null)"
  if [[ "$sb_present" == "1" && "$sb_valid" == "1" ]]; then
    printf "  "; ok; printf "sandbox profile valid and ready\n"
  elif [[ "$sb_present" == "1" ]]; then
    printf "  "; warn; printf "sandbox profile present but FAILED to parse\n"
  else
    printf "  "; warn; printf "sandbox profile not yet written (run ./run.sh char launch)\n"
  fi

  # 3. Is Char running? If so, was it launched through us?
  local char_pids
  char_pids="$(pgrep -fl 'Char.app/Contents/MacOS' 2>/dev/null || true)"
  if [[ -z "$char_pids" ]]; then
    printf "  "; printf "%s%s%s Char           is not currently running\n" \
                       "$c_dim" "•" "$c_reset"
  else
    while IFS= read -r line; do
      local pid="${line%% *}"
      # ``ps -o command= -p $pid | head -c 1`` is "/" if the leaf
      # process was exec'd directly, but to detect our wrapper we
      # have to look at the *parent* lineage. The cheapest signal
      # is "does HTTPS_PROXY=127.0.0.1:8889 appear in /proc-like
      # env?". macOS doesn't expose env via /proc, but we can use
      # ``ps eww`` to capture the env of the process.
      local env_dump
      env_dump="$(ps eww -p "$pid" 2>/dev/null | tail -n +2 || true)"
      if [[ "$env_dump" == *"HTTPS_PROXY=http://127.0.0.1:$EGRESS_PROXY_PORT"* ]]; then
        printf "  "; ok; printf "Char pid %-7s through our wrapper (firewall active)\n" "$pid"
      else
        printf "  "; bad; printf "Char pid %-7s NOT through our wrapper — egress is NOT filtered\n" "$pid"
        printf "         %skill it and relaunch with %s./run.sh char launch%s%s\n" \
               "$c_dim" "$c_bold" "$c_reset" "$c_dim$c_reset"
      fi
    done <<< "$char_pids"
  fi

  # 4. Recent proxy decisions. The audit ring lives in the proxy
  # process's address space, so we can't read it from this shell;
  # tail the structured log file instead. Lines of interest are
  # tagged with "DENY" / "ALLOW" / "ERROR" at INFO/WARN level.
  if egress_proxy_pid >/dev/null && [[ -f "$EGRESS_PROXY_LOG_FILE" ]]; then
    printf "\n%s──── last 5 egress decisions (from %s) ────%s\n" \
           "$c_bold" "$EGRESS_PROXY_LOG_FILE" "$c_reset"
    /usr/bin/grep -E ' (DENY|ALLOW|ERROR) ' "$EGRESS_PROXY_LOG_FILE" 2>/dev/null \
      | /usr/bin/tail -n 5 \
      | /usr/bin/sed 's/^/  /' \
      || printf "  ${c_dim}(no decisions logged yet)${c_reset}\n"
  fi
}


cmd_redo_session() {
  shift  # drop "redo-session"
  if [[ $# -eq 0 ]]; then
    cat <<EOF
usage: ./run.sh redo-session SESSION [--speakers N] [--cluster-threshold F]
                                     [--no-diarize] [--asr-url URL]

Re-run ASR + diarization on an existing Char session and overwrite its
transcript.json. Useful when the original "Generate" produced the wrong
number of speakers (e.g. a 1:1 call that came back as one big blob, or
a long meeting that over-clustered into 30+ phantom speakers).

Examples:

  ./run.sh redo-session 77f87727-c9b8-4bac-bbfa-26934c8b4ba7 --speakers 2
  ./run.sh redo-session "Maus Meeting" --speakers 2 --cluster-threshold 0.85
  ./run.sh redo-session test --no-diarize     # rewrite as single speaker

The ASR server must be running (./run.sh start). The session is matched
by full UUID, UUID prefix, or session-title substring.
EOF
    return 1
  fi
  if ! curl -fsS "http://127.0.0.1:${ASR_PORT}/health" >/dev/null 2>&1; then
    say "ASR server is not running. Start it with: ./run.sh start"
    return 1
  fi
  # redo_session.py derives the ASR bearer token in-process, which
  # unlocks the master key. SIP must be on.
  sip_gate || return 1
  exec "$VENV_PY" -u -m local_scribe.asr.redo_session "$@"
}

# Dispatcher. The ``BASH_SOURCE`` guard is the bash equivalent of
# Python's ``if __name__ == "__main__"`` — only run the dispatcher
# when this file is executed directly, not when something else
# ``source``s it. That makes the file safe to source from test
# harnesses (tests/bootstrap/*) and shell-completion scripts without
# kicking off the help text + ``exit 0`` path.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    start)      shift; cmd_start "$@" ;;
    stop)       cmd_stop ;;
    restart)    shift; cmd_stop; cmd_start "$@" ;;
    status)     cmd_status ;;
    logs)       cmd_logs ;;
    health)     cmd_health ;;
    doctor)     cmd_doctor ;;
    setup)      cmd_setup ;;
    bootstrap)  cmd_bootstrap ;;
    configure-char|configure_char|configure)
                cmd_configure_char ;;
    install-char|install_char)
                cmd_install_char ;;
    install-llm|install_llm|install-lmstudio|install_lmstudio)
                cmd_install_llm ;;
    inspector|web|ui)
                shift; cmd_inspector "$@" ;;
    proxy|egress-proxy|egress_proxy)
                shift; cmd_egress_proxy "$@" ;;
    transcribe) cmd_transcribe "$@" ;;
    redo-session|redo_session|redo) cmd_redo_session "$@" ;;
    firewall|fw) cmd_firewall "$@" ;;
    key|keys)   cmd_key "$@" ;;
    vault)      cmd_vault "$@" ;;
    yubikey|yk) cmd_yubikey "$@" ;;
    config)     shift; cmd_config "$@" ;;
    char)        shift; cmd_char "$@" ;;
    ""|-h|--help|help)
      awk 'NR==1{next}
           /^#/{sub(/^# ?/,""); print; next}
           /^[[:space:]]*$/{print; next}
           {exit}' "$0"
      exit 0
    ;;
    *)
      say "unknown command: $1"
      say "run \`./run.sh\` with no args for help"
      exit 1
    ;;
  esac
fi
