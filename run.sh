#!/usr/bin/env bash
# Local transcription pipeline manager.
#
# Services:
#   - LM Studio API @ :1234            (started via `lms server start`)
#     + qwen3-30b-a3b-instruct-2507    (loaded via `lms load`)
#   - ASR server @ :8000               (uvicorn whisper_server:app, Parakeet by default)
#
# The ASR backend (parakeet-mlx) and diarization backend (sherpa-onnx)
# both run in-process - no separate service.
#
# Usage:
#   ./run.sh start          run preflight (auto-install missing deps + models),
#                            start LM Studio + ASR server, then tail the ASR log
#                            so you can watch traffic. Ctrl+C detaches; services
#                            keep running.
#   ./run.sh stop           stop the ASR server (LM Studio left alone)
#   ./run.sh restart        stop + start
#   ./run.sh status         show service health, PIDs, ports
#   ./run.sh logs           tail the ASR server log
#   ./run.sh health         one-shot health check
#   ./run.sh doctor         run preflight only (deps, models, services) and
#                            print a detailed report - safe to run any time
#   ./run.sh setup          force-reinstall pip deps + (re)download models
#   ./run.sh transcribe FILE [args...]
#                            run transcribe_file.py FILE with the venv python
#
# Env overrides:
#   ASR_BACKEND       default parakeet  (parakeet | whisper)
#   PARAKEET_MODEL    default mlx-community/parakeet-tdt-0.6b-v3
#   WHISPER_MODEL     default large-v3-turbo  (only used if ASR_BACKEND=whisper)
#   WHISPER_PORT      default 8000
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

WHISPER_PID_FILE="$RUN_DIR/whisper_server.pid"
WHISPER_LOG_FILE="$RUN_DIR/whisper_server.log"
DEPS_STAMP="$RUN_DIR/deps.stamp"   # mtime tracks last successful pip install

ASR_BACKEND_DEFAULT="${ASR_BACKEND:-parakeet}"
PARAKEET_MODEL="${PARAKEET_MODEL:-mlx-community/parakeet-tdt-0.6b-v3}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"
WHISPER_PORT="${WHISPER_PORT:-8000}"
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
    "$VENV_PY" - <<'PY'
import importlib
mods = [
    ("fastapi", None), ("uvicorn", None), ("requests", None),
    ("numpy", None), ("soundfile", None), ("librosa", None),
    ("parakeet_mlx", None), ("faster_whisper", None),
    ("sherpa_onnx", None), ("huggingface_hub", None),
]
for name, _ in mods:
    try:
        m = importlib.import_module(name)
        v = getattr(m, "__version__", "ok")
        print(f"  \033[32m●\033[0m {name:18s} {v}")
    except Exception as e:
        print(f"  \033[31m○\033[0m {name:18s} MISSING ({type(e).__name__})")
PY
  else
    printf "  (skip - venv missing)\n"
  fi

  printf "\n%smodels:%s\n" "$c_bold" "$c_reset"
  if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" - <<PY
from pathlib import Path
from huggingface_hub import snapshot_download
def check(repo, label):
    try:
        p = snapshot_download(repo_id=repo, local_files_only=True)
        print(f"  \033[32m●\033[0m {label:30s} cached at {p}")
    except Exception:
        print(f"  \033[33m○\033[0m {label:30s} not yet downloaded")
check("$PARAKEET_MODEL",      "parakeet ($ASR_BACKEND_DEFAULT default)")
import os
diar = Path.home() / ".cache" / "whisper_server" / "diarization"
seg  = diar / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
emb  = diar / "nemo_en_titanet_small.onnx"
mark = lambda b: "\033[32m●\033[0m" if b else "\033[33m○\033[0m"
print(f"  {mark(seg.exists())} pyannote segmentation         {seg}")
print(f"  {mark(emb.exists())} NeMo TitaNet embedding        {emb}")
PY
  fi

  printf "\n%sservices:%s\n" "$c_bold" "$c_reset"
  if curl -sf "http://127.0.0.1:$WHISPER_PORT/health" -o /dev/null 2>&1; then
    printf "  "; ok; printf "ASR server   :%s   reachable\n" "$WHISPER_PORT"
  else
    printf "  "; warn; printf "ASR server   :%s   not running (start with ./run.sh start)\n" "$WHISPER_PORT"
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
  printf "  base URL : http://127.0.0.1:%s\n" "$WHISPER_PORT"
  printf "  api key  : (any non-empty string - auth is ignored locally)\n"
  printf "  intelligence provider : LM Studio @ http://127.0.0.1:%s   model=%s\n" "$LMSTUDIO_PORT" "$LLM_MODEL"
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

# --- whisper server ---

whisper_pid() {
  [[ -f "$WHISPER_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$WHISPER_PID_FILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

whisper_start() {
  if whisper_pid >/dev/null; then
    say "whisper server already running (pid $(whisper_pid))"
    return 0
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    say "${c_red}venv python missing at $VENV_PY${c_reset}"
    return 1
  fi
  say "starting whisper server on :$WHISPER_PORT ..."
  # The Whisper model loads lazily on first request, so startup is instant.
  # We append to the log so previous runs' output stays around for review.
  printf "\n========== started %s ==========\n" "$(date)" >> "$WHISPER_LOG_FILE"
  nohup "$VENV_PY" -u -m uvicorn whisper_server:app \
        --host 0.0.0.0 --port "$WHISPER_PORT" \
        >>"$WHISPER_LOG_FILE" 2>&1 &
  echo $! > "$WHISPER_PID_FILE"
  for _ in {1..30}; do
    if curl -sf "http://127.0.0.1:$WHISPER_PORT/health" >/dev/null 2>&1; then
      say "${c_green}whisper server up on :$WHISPER_PORT (pid $(whisper_pid))${c_reset}"
      return 0
    fi
    sleep 1
  done
  say "${c_red}whisper server didn't respond on :$WHISPER_PORT after 30s${c_reset}"
  say "  see $WHISPER_LOG_FILE"
  return 1
}

whisper_stop() {
  if ! whisper_pid >/dev/null; then
    say "whisper server is not running"
    rm -f "$WHISPER_PID_FILE"
    return 0
  fi
  local pid; pid="$(whisper_pid)"
  say "stopping whisper server (pid $pid) ..."
  kill "$pid" 2>/dev/null || true
  for _ in {1..15}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    say "${c_yellow}forcing kill -9${c_reset}"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$WHISPER_PID_FILE"
  say "${c_green}whisper server stopped${c_reset}"
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

lmstudio_start() {
  if ! command -v lms >/dev/null 2>&1; then
    say "${c_yellow}lms CLI not found - skipping LM Studio orchestration${c_reset}"
    say "  open LM Studio.app and enable Developer > Local Server manually"
    return 0
  fi
  if ! lmstudio_running; then
    say "starting LM Studio HTTP server ..."
    lms server start --port "$LMSTUDIO_PORT" >/dev/null 2>&1 || true
    for _ in {1..15}; do
      lmstudio_running && break
      sleep 1
    done
    if ! lmstudio_running; then
      say "${c_red}LM Studio API isn't reachable on :$LMSTUDIO_PORT${c_reset}"
      say "  open LM Studio.app and turn on the local server"
      return 1
    fi
  fi
  say "${c_green}LM Studio API up on :$LMSTUDIO_PORT${c_reset}"

  if lmstudio_model_loaded "$LLM_MODEL"; then
    say "${c_green}$LLM_MODEL already loaded${c_reset}"
  else
    say "loading $LLM_MODEL with context-length=$LLM_CONTEXT (this can take a minute) ..."
    if ! lms load "$LLM_MODEL" --context-length "$LLM_CONTEXT" >/dev/null 2>&1; then
      say "${c_red}failed to load $LLM_MODEL via lms${c_reset}"
      say "  is it downloaded? run \`lms ls\` to check"
      return 1
    fi
    say "${c_green}$LLM_MODEL loaded${c_reset}"
  fi
}

# --- top-level commands ---

cmd_start() {
  printf "%s%s%s\n" "$c_bold" "starting transcription pipeline" "$c_reset"
  preflight || {
    say "${c_red}preflight failed${c_reset}"
    say "  fix the errors above (or run \`./run.sh setup\`) before starting"
    return 1
  }
  lmstudio_start || true
  whisper_start
  printf "\n"
  printf "%s──── pipeline ready ────%s\n" "$c_bold" "$c_reset"
  printf "  ASR server (Parakeet TDT v3) : %shttp://127.0.0.1:%s%s   (Char's transcription endpoint)\n" \
         "$c_green" "$WHISPER_PORT" "$c_reset"
  printf "  LM Studio API (Qwen3-30B)    : %shttp://127.0.0.1:%s%s   (summary + speaker naming)\n" \
         "$c_green" "$LMSTUDIO_PORT" "$c_reset"
  printf "  log file                     : %s\n" "$WHISPER_LOG_FILE"
  printf "\n"
  printf "  on-demand:    %s./run.sh transcribe ~/Desktop/call.m4a%s\n" "$c_bold" "$c_reset"
  printf "  status:       %s./run.sh status%s\n" "$c_bold" "$c_reset"
  printf "  stop:         %s./run.sh stop%s\n" "$c_bold" "$c_reset"
  printf "\n"
  printf "tailing whisper server log; %sCtrl+C detaches without stopping%s\n" \
         "$c_yellow" "$c_reset"
  printf "\n"
  exec tail -F "$WHISPER_LOG_FILE"
}

cmd_stop() {
  whisper_stop
  printf "  %sLM Studio left running%s; use \`lms server stop\` to free GPU memory\n" \
         "$c_dim" "$c_reset"
}

cmd_status() {
  printf "%spipeline status%s\n" "$c_bold" "$c_reset"
  if whisper_pid >/dev/null; then
    local backend="?" model="?"
    local health_json; health_json="$(curl -sf "http://127.0.0.1:$WHISPER_PORT/health" 2>/dev/null || true)"
    if [[ -n "$health_json" ]]; then
      backend="$(printf '%s' "$health_json" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('asr_backend','?'))" 2>/dev/null || echo "?")"
      model="$(printf '%s' "$health_json"   | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('model','?'))" 2>/dev/null || echo "?")"
    fi
    printf "  "; ok; printf "ASR server       pid=%-7s port=%s   backend=%s   model=%s\n" \
                          "$(whisper_pid)" "$WHISPER_PORT" "$backend" "$model"
    printf "                   log=%s\n" "$WHISPER_LOG_FILE"
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
  if [[ -f "$WHISPER_LOG_FILE" ]]; then
    exec tail -F "$WHISPER_LOG_FILE"
  else
    say "no log file at $WHISPER_LOG_FILE"
    say "(start the pipeline with './run.sh start' first)"
    exit 1
  fi
}

cmd_health() {
  local rc=0
  if curl -sf "http://127.0.0.1:$WHISPER_PORT/health" -o /dev/null; then
    printf "  "; ok; printf "whisper @ :%s\n" "$WHISPER_PORT"
  else
    printf "  "; bad; printf "whisper @ :%s\n" "$WHISPER_PORT"; rc=1
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
