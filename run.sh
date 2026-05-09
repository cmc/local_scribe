#!/usr/bin/env bash
# local_scribe - service manager for the local ASR + LLM pipeline.
#
# Services managed:
#   - LM Studio API @ :1234            (started via `lms server start`)
#     + qwen3-30b-a3b-instruct-2507    (loaded via `lms load`)
#   - ASR server @ :8000               (uvicorn asr_server:app)
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
#   ./run.sh configure-char point Char's OpenAI transcriber at this server
#                            (interactive; offers to back up existing API key).
#                            Bootstrap calls this for you on a fresh install.
#   ./run.sh transcribe FILE [args...]
#                            run transcribe_file.py FILE with the venv python
#
# Env overrides:
#   ASR_BACKEND       default parakeet  (parakeet | whisper)
#   ASR_PORT          default 8000      (where the ASR server listens)
#   PARAKEET_MODEL    default mlx-community/parakeet-tdt-0.6b-v3
#   WHISPER_MODEL     default large-v3-turbo  (only used if ASR_BACKEND=whisper)
#   LMSTUDIO_PORT     default 1234
#   LLM_MODEL         default qwen3-30b-a3b-instruct-2507
#   LLM_CONTEXT       default 65536
#   PYTHON            default python3.14 (else python3.12, else python3)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV_DIR="$REPO/venv"
VENV_PY="$VENV_DIR/bin/python"
RUN_DIR="$REPO/.run"
mkdir -p "$RUN_DIR"

ASR_PID_FILE="$RUN_DIR/asr_server.pid"
ASR_LOG_FILE="$RUN_DIR/asr_server.log"
DEPS_STAMP="$RUN_DIR/deps.stamp"   # mtime tracks last successful pip install

ASR_BACKEND_DEFAULT="${ASR_BACKEND:-parakeet}"
PARAKEET_MODEL="${PARAKEET_MODEL:-mlx-community/parakeet-tdt-0.6b-v3}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"
ASR_PORT="${ASR_PORT:-8000}"
LMSTUDIO_PORT="${LMSTUDIO_PORT:-1234}"
LLM_MODEL="${LLM_MODEL:-qwen3-30b-a3b-instruct-2507}"
LLM_CONTEXT="${LLM_CONTEXT:-65536}"

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
from diarization_backend import ensure_models
ensure_models()
PY
  then
    say "${c_green}diarization models ready${c_reset}"
    return 0
  fi
  say "${c_yellow}diarization models couldn't be prefetched${c_reset}"
  say "  they will auto-download on the first --diarize run instead"
}

# Friendly check for the LM Studio CLI. Not strictly required (you can run
# LM Studio.app's local server manually) but the auto-load convenience needs it.
ensure_lms_cli() {
  if command -v lms >/dev/null 2>&1; then
    return 0
  fi
  say "${c_yellow}lms CLI not found${c_reset}"
  say "  to enable auto-start of LM Studio + Qwen, run:"
  say "    ~/.lmstudio/bin/lms bootstrap   (after installing LM Studio.app)"
  say "  or open LM Studio.app and turn on Developer > Local Server manually"
  return 0  # not fatal
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

  printf "%spython:%s\n" "$c_bold" "$c_reset"
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
    if command -v lms >/dev/null 2>&1; then
      printf "         (./run.sh start will auto-start it)\n"
    else
      printf "         install LM Studio.app and run \`lms bootstrap\`\n"
    fi
  fi

  printf "\n%schar config (set these in Char's Settings -> Transcription):%s\n" "$c_bold" "$c_reset"
  printf "  Live recording  : Custom provider, Base URL http://127.0.0.1:%s\n" "$ASR_PORT"
  printf "  Generate (file) : OpenAI provider, Advanced -> Base URL http://127.0.0.1:%s/v1\n" "$ASR_PORT"
  printf "  api key (both)  : any non-empty string (auth is ignored locally)\n"
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

  printf "%s(1/5) python venv + pip deps%s\n" "$c_bold" "$c_reset"
  ensure_pip_deps             || return 1

  printf "\n%s(2/5) parakeet ASR weights%s\n" "$c_bold" "$c_reset"
  ensure_parakeet_model       || return 1

  printf "\n%s(3/5) sherpa-onnx diarization models%s\n" "$c_bold" "$c_reset"
  ensure_diarization_models   || true   # best-effort

  printf "\n%s(4/5) LM Studio CLI check%s\n" "$c_bold" "$c_reset"
  ensure_lms_cli              || true

  printf "\n%s(5/5) Char transcriber config%s\n" "$c_bold" "$c_reset"
  if char_installed; then
    if ask_yn "  Configure Char.app now to send transcripts to this server?" y; then
      printf "\n"
      cmd_configure_char || say "${c_yellow}Char config skipped/failed; rerun with ./run.sh configure-char${c_reset}"
    else
      printf "  skipped — run %s./run.sh configure-char%s any time\n" "$c_bold" "$c_reset"
    fi
  else
    printf "  Char.app not installed at /Applications/Char.app — skipping.\n"
    printf "  After installing Char, run: %s./run.sh configure-char%s\n" "$c_bold" "$c_reset"
  fi

  printf "\n%s════════ bootstrap complete ════════%s\n\n" "$c_green" "$c_reset"

  # What the user still needs to do manually (we said in the brief that Char,
  # LM Studio, and the Qwen model are out of scope to install for them).
  printf "%sNext steps - one-time, manual:%s\n" "$c_bold" "$c_reset"
  printf "  1. Install %sChar.app%s (https://char.com - open-source, https://github.com/fastrepl/anarlog) if you haven't yet.\n" \
         "$c_bold" "$c_reset"
  printf "     Then run %s./run.sh configure-char%s to wire it up automatically.\n" \
         "$c_bold" "$c_reset"
  printf "  2. Install %sLM Studio.app%s (https://lmstudio.ai), then in its\n" \
         "$c_bold" "$c_reset"
  printf "     model browser download %s%s%s.\n" \
         "$c_bold" "$LLM_MODEL" "$c_reset"
  if ! command -v lms >/dev/null 2>&1; then
    printf "     Install the lms CLI so this script can manage it:\n"
    printf "       %s~/.lmstudio/bin/lms bootstrap%s\n" "$c_bold" "$c_reset"
  fi
  printf "  3. In Char → Settings → Intelligence, set provider = LM Studio,\n"
  printf "     base URL = http://127.0.0.1:%s, model = %s\n" \
         "$LMSTUDIO_PORT" "$LLM_MODEL"

  printf "\n%sThen start the pipeline:%s\n" "$c_bold" "$c_reset"
  printf "    %s./run.sh start%s\n" "$c_bold" "$c_reset"
  printf "\nVerify any time with: %s./run.sh doctor%s\n\n" "$c_bold" "$c_reset"
}

# --- Char configuration ---
#
# Char is a Tauri app whose settings live as JSON on disk under the legacy
# bundle id `com.hyprnote.stable`. We only touch the `ai.stt.openai.*` and
# `ai.current_stt_*` keys here; LLM provider, templates, etc. are left alone.

CHAR_APP="/Applications/Char.app"
CHAR_DATA_DIR="$HOME/Library/Application Support/hyprnote"
CHAR_SETTINGS="$CHAR_DATA_DIR/settings.json"

char_installed() { [[ -d "$CHAR_APP" ]]; }

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
    say "  install Char from https://char.com (or https://github.com/fastrepl/anarlog), then re-run"
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

  # Patch the four keys we care about. Everything else (LLM, templates,
  # general.*, calendars, etc.) is left untouched.
  if ! "$VENV_PY" - "$CHAR_SETTINGS" "$ASR_PORT" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); port = sys.argv[2]
d = json.loads(p.read_text())
ai  = d.setdefault("ai", {})
stt = ai.setdefault("stt", {})
oai = stt.setdefault("openai", {})
ai["current_stt_provider"] = "openai"
ai["current_stt_model"]    = "gpt-4o-transcribe-diarize"
oai["base_url"]            = f"http://127.0.0.1:{port}/v1"
oai["api_key"]             = "local"
p.write_text(json.dumps(d, indent=2) + "\n")
PY
  then
    say "${c_red}failed to write settings.json — your backup is at $settings_backup${c_reset}"
    return 1
  fi

  printf "\n  %s● char configured%s\n" "$c_green" "$c_reset"
  printf "    current_stt_provider : openai\n"
  printf "    current_stt_model    : gpt-4o-transcribe-diarize  (non-streaming, with speaker labels)\n"
  printf "    stt.openai.base_url  : http://127.0.0.1:%s/v1\n" "$ASR_PORT"
  printf "    stt.openai.api_key   : local\n"
  if [[ -n "$key_backup_path" ]]; then
    printf "    previous key saved   : %s\n" "$key_backup_path"
  fi
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

# --- ASR server ---

asr_pid() {
  [[ -f "$ASR_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$ASR_PID_FILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

asr_start() {
  if asr_pid >/dev/null; then
    say "ASR server already running (pid $(asr_pid))"
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing at $VENV_PY${c_reset}"
    return 1
  fi
  say "starting ASR server on :$ASR_PORT ..."
  # The Whisper model loads lazily on first request, so startup is instant.
  # We append to the log so previous runs' output stays around for review.
  printf "\n========== started %s ==========\n" "$(date)" >> "$ASR_LOG_FILE"
  nohup "$VENV_PY" -u -m uvicorn asr_server:app \
        --host 0.0.0.0 --port "$ASR_PORT" \
        >>"$ASR_LOG_FILE" 2>&1 &
  echo $! > "$ASR_PID_FILE"
  for _ in {1..30}; do
    if curl -sf "http://127.0.0.1:$ASR_PORT/health" >/dev/null 2>&1; then
      say "${c_green}ASR server up on :$ASR_PORT (pid $(asr_pid))${c_reset}"
      return 0
    fi
    sleep 1
  done
  say "${c_red}ASR server didn't respond on :$ASR_PORT after 30s${c_reset}"
  say "  see $ASR_LOG_FILE"
  return 1
}

asr_stop() {
  if ! asr_pid >/dev/null; then
    say "ASR server is not running"
    rm -f "$ASR_PID_FILE"
    return 0
  fi
  local pid; pid="$(asr_pid)"
  say "stopping ASR server (pid $pid) ..."
  kill "$pid" 2>/dev/null || true
  for _ in {1..15}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    say "${c_yellow}forcing kill -9${c_reset}"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$ASR_PID_FILE"
  say "${c_green}ASR server stopped${c_reset}"
}

# --- LM Studio ---

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

# Bring LM Studio + Qwen up. Returns:
#   0 - LM Studio reachable AND $LLM_MODEL loaded (fully ready)
#   1 - LM Studio not reachable
#   2 - LM Studio reachable but $LLM_MODEL not loaded
# Callers use the exit code to decide how to render the readiness banner.
lmstudio_start() {
  local has_lms=0
  command -v lms >/dev/null 2>&1 && has_lms=1

  if ! lmstudio_running; then
    if [[ $has_lms -eq 1 ]]; then
      say "starting LM Studio HTTP server ..."
      lms server start --port "$LMSTUDIO_PORT" >/dev/null 2>&1 || true
      for _ in {1..15}; do
        lmstudio_running && break
        sleep 1
      done
    fi
    if ! lmstudio_running; then
      say "${c_red}LM Studio API not reachable on :$LMSTUDIO_PORT${c_reset}"
      if [[ $has_lms -eq 0 ]]; then
        say "  install the lms CLI:  ~/.lmstudio/bin/lms bootstrap"
        say "  or open LM Studio.app and turn on Developer > Local Server"
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

  if [[ $has_lms -eq 0 ]]; then
    say "${c_yellow}$LLM_MODEL not loaded${c_reset}"
    say "  install lms CLI to auto-load, or load it from LM Studio.app's UI"
    return 2
  fi

  say "loading $LLM_MODEL with context-length=$LLM_CONTEXT (this can take a minute) ..."
  if ! lms load "$LLM_MODEL" --context-length "$LLM_CONTEXT" >/dev/null 2>&1; then
    say "${c_red}failed to load $LLM_MODEL via lms${c_reset}"
    say "  is it downloaded? run \`lms ls\` to check"
    say "  in LM Studio.app's model browser, search for and download:"
    say "    $LLM_MODEL"
    return 2
  fi
  say "${c_green}$LLM_MODEL loaded${c_reset}"
  return 0
}

# --- top-level commands ---

cmd_start() {
  printf "%s%s%s\n" "$c_bold" "starting transcription pipeline" "$c_reset"
  preflight || {
    say "${c_red}preflight failed${c_reset}"
    say "  fix the errors above (or run \`./run.sh setup\`) before starting"
    return 1
  }

  lmstudio_start
  local lms_rc=$?    # 0 ok, 1 unreachable, 2 reachable-but-no-model

  asr_start || return 1
  printf "\n"

  if [[ $lms_rc -eq 0 ]]; then
    printf "%s──── pipeline ready ────%s\n" "$c_bold" "$c_reset"
    printf "  ASR server (Parakeet TDT v3) : %shttp://127.0.0.1:%s%s   (Char's transcription endpoint)\n" \
           "$c_green" "$ASR_PORT" "$c_reset"
    printf "  LM Studio API (Qwen3-30B)    : %shttp://127.0.0.1:%s%s   (summary + speaker naming)\n" \
           "$c_green" "$LMSTUDIO_PORT" "$c_reset"
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
  printf "  %sLM Studio left running%s; use \`lms server stop\` to free GPU memory\n" \
         "$c_dim" "$c_reset"
}

cmd_status() {
  printf "%spipeline status%s\n" "$c_bold" "$c_reset"
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
  exec "$VENV_PY" -u "$REPO/transcribe_file.py" "$@"
}

case "${1:-}" in
  start)      cmd_start ;;
  stop)       cmd_stop ;;
  restart)    cmd_stop; cmd_start ;;
  status)     cmd_status ;;
  logs)       cmd_logs ;;
  health)     cmd_health ;;
  doctor)     cmd_doctor ;;
  setup)      cmd_setup ;;
  bootstrap)  cmd_bootstrap ;;
  configure-char|configure_char|configure)
              cmd_configure_char ;;
  transcribe) cmd_transcribe "$@" ;;
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
