#!/usr/bin/env bash
# tools/run_demo.sh — start an isolated demo inspector on a separate
# port, pointed at the seeded demo Char data dir, with auth bypassed.
#
# This is what powers the screenshots in README.md. It deliberately
# does NOT touch your real ~/Library/Application Support/hyprnote or
# your real ~/.config/local_scribe; everything lives under
# ~/.cache/local_scribe-demo so it can be wiped without consequence.
#
# Usage:
#   tools/run_demo.sh start [PORT]    # default port: 8765
#   tools/run_demo.sh stop
#   tools/run_demo.sh restart [PORT]
#   tools/run_demo.sh status
#   tools/run_demo.sh url             # print the auth-bypass demo URL

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEMO_ROOT="$HOME/.cache/local_scribe-demo"
DEMO_CHAR_DIR="$DEMO_ROOT/hyprnote"
DEMO_CONFIG_DIR="$DEMO_ROOT/config"
DEMO_PID_FILE="$DEMO_ROOT/inspector.pid"
DEMO_LOG_FILE="$DEMO_ROOT/inspector.log"
DEMO_PORT="${LOCAL_SCRIBE_DEMO_PORT:-8765}"

VENV_PY="$ROOT/venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  VENV_PY="$(command -v python3)"
fi

c_green=$'\033[32m'
c_red=$'\033[31m'
c_dim=$'\033[2m'
c_reset=$'\033[0m'

ensure_seeded() {
  if [[ ! -d "$DEMO_CHAR_DIR/sessions" ]] || \
     [[ -z "$(ls -A "$DEMO_CHAR_DIR/sessions" 2>/dev/null)" ]]; then
    echo "${c_dim}demo dir empty — seeding...${c_reset}"
    "$VENV_PY" "$ROOT/tools/seed_demo.py" --target "$DEMO_CHAR_DIR" --clean
  fi
}

ensure_demo_config() {
  mkdir -p "$DEMO_CONFIG_DIR"
  local cfg="$DEMO_CONFIG_DIR/config.json"
  if [[ -f "$cfg" ]]; then return 0; fi
  cat >"$cfg" <<EOF
{
  "asr":        { "bind": "127.0.0.1", "port": 8000, "backend": "parakeet",
                  "parakeet_model": "nvidia/parakeet-tdt-0.6b-v3",
                  "whisper_model": "large-v3-turbo",
                  "stream_heartbeat_seconds": 1.0,
                  "diarization": { "enabled": true, "max_seconds": 7200,
                                   "max_speakers": 6, "num_speakers": null,
                                   "cluster_threshold": null } },
  "llm":        { "host": "127.0.0.1", "port": 1234,
                  "model": "qwen3-30b-a3b-instruct-2507",
                  "max_tokens": 4096, "temperature": 0.2 },
  "inspector":  { "bind": "127.0.0.1", "port": $DEMO_PORT,
                  "auth_token": null },
  "char":       { "data_dir": "$DEMO_CHAR_DIR",
                  "expected_stt_provider": "openai",
                  "expected_stt_model": "gpt-4o-transcribe" }
}
EOF
}

cmd_pid() {
  if [[ -f "$DEMO_PID_FILE" ]]; then
    local pid
    pid="$(cat "$DEMO_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  fi
  return 1
}

cmd_url() {
  echo "http://127.0.0.1:$DEMO_PORT/"
}

cmd_start() {
  if [[ "${1:-}" =~ ^[0-9]+$ ]]; then DEMO_PORT="$1"; fi
  if cmd_pid >/dev/null; then
    echo "${c_dim}demo inspector already running (pid $(cmd_pid)) at $(cmd_url)${c_reset}"
    return 0
  fi
  mkdir -p "$DEMO_ROOT"
  ensure_seeded
  ensure_demo_config

  echo "starting demo inspector on 127.0.0.1:$DEMO_PORT ..."
  printf "\n========== started %s ==========\n" "$(date)" >>"$DEMO_LOG_FILE"

  # Demo bypasses below. None of these are honoured by the production
  # ./run.sh start path; they're test/CI hooks used only here.
  #   - DISABLE_AUTH=1            : skip the HKDF bearer-token gate
  #   - TEST_CSRUTIL_OUTPUT=...   : pretend SIP is enabled (the gate
  #     correctly refuses to start otherwise; for screenshot purposes
  #     we override it with the canonical "enabled" string).
  LOCAL_SCRIBE_DISABLE_AUTH=1 \
  LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT="System Integrity Protection status: enabled." \
  LOCAL_SCRIBE_CONFIG_DIR="$DEMO_CONFIG_DIR" \
  LOCAL_SCRIBE_CHAR_DATA_DIR="$DEMO_CHAR_DIR" \
  LOCAL_SCRIBE_INSPECTOR_PORT="$DEMO_PORT" \
    nohup "$VENV_PY" -u -m uvicorn local_scribe.inspector.inspector_server:app \
      --host 127.0.0.1 --port "$DEMO_PORT" \
      >>"$DEMO_LOG_FILE" 2>&1 &
  echo $! >"$DEMO_PID_FILE"

  for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$DEMO_PORT/api/health" >/dev/null 2>&1; then
      echo "${c_green}demo inspector up at $(cmd_url) (pid $(cmd_pid))${c_reset}"
      echo "  data:  $DEMO_CHAR_DIR"
      echo "  log:   $DEMO_LOG_FILE"
      echo "  auth:  LOCAL_SCRIBE_DISABLE_AUTH=1 (demo only, no real keys)"
      return 0
    fi
    sleep 0.5
  done
  echo "${c_red}demo inspector didn't respond after 10s; see $DEMO_LOG_FILE${c_reset}"
  return 1
}

cmd_stop() {
  if ! cmd_pid >/dev/null; then
    echo "${c_dim}demo inspector not running${c_reset}"
    return 0
  fi
  local pid
  pid="$(cmd_pid)"
  kill "$pid" 2>/dev/null || true
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$DEMO_PID_FILE"
  echo "demo inspector stopped"
}

cmd_status() {
  if cmd_pid >/dev/null; then
    echo "${c_green}running${c_reset}  pid=$(cmd_pid)  url=$(cmd_url)"
  else
    echo "${c_dim}not running${c_reset}"
  fi
}

case "${1:-start}" in
  start)   shift || true; cmd_start "$@" ;;
  stop)    cmd_stop ;;
  restart) shift || true; cmd_stop || true; sleep 0.3; cmd_start "$@" ;;
  status)  cmd_status ;;
  url)     cmd_url ;;
  *)       echo "usage: tools/run_demo.sh {start|stop|restart|status|url} [PORT]"; exit 2 ;;
esac
