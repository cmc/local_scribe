#!/usr/bin/env bash
# Local transcription pipeline manager.
#
# Services:
#   - LM Studio API @ :1234            (started via `lms server start`)
#     + qwen3-30b-a3b-instruct-2507    (loaded via `lms load`)
#   - Whisper server @ :8000           (uvicorn whisper_server:app)
#
# The ASR backend (parakeet-mlx) and diarization backend (sherpa-onnx)
# both run in-process inside transcribe_file.py - no separate service.
#
# Usage:
#   ./run.sh start          start LM Studio + Whisper server, then tail the
#                            whisper log so you can watch traffic. Ctrl+C
#                            detaches; services keep running.
#   ./run.sh stop           stop the whisper server (LM Studio left alone)
#   ./run.sh restart        stop + start
#   ./run.sh status         show service health, PIDs, ports
#   ./run.sh logs           tail the whisper server log
#   ./run.sh health         one-shot health check
#   ./run.sh transcribe FILE [args...]
#                            run transcribe_file.py FILE with the venv python
#
# Env overrides:
#   WHISPER_PORT      default 8000
#   LMSTUDIO_PORT     default 1234
#   LLM_MODEL         default qwen3-30b-a3b-instruct-2507
#   LLM_CONTEXT       default 65536

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV_PY="$REPO/venv/bin/python"
RUN_DIR="$REPO/.run"
mkdir -p "$RUN_DIR"

WHISPER_PID_FILE="$RUN_DIR/whisper_server.pid"
WHISPER_LOG_FILE="$RUN_DIR/whisper_server.log"

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
