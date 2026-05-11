#!/usr/bin/env bash
# tools/capture_screenshots.sh — drive headless Chrome against the
# running demo inspector and write PNGs under docs/screenshots/.
#
# This relies on:
#   * the demo inspector already running on 127.0.0.1:8765
#     (tools/run_demo.sh start)
#   * Google Chrome installed at /Applications/Google Chrome.app
#   * the inspector's ?tab=...&session=... deep-link handler (any
#     reasonably recent local_scribe build has it)
#
# Output: one PNG per tab / state, plus an index.json that records the
# (url, file, taken_at) for each shot.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT/docs/screenshots"
DEMO_URL="${DEMO_URL:-http://127.0.0.1:8765}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

if [[ ! -x "$CHROME" ]]; then
  echo "Chrome not found at $CHROME — set CHROME=path/to/Google\ Chrome" >&2
  exit 2
fi

if ! curl -sf "$DEMO_URL/api/health" >/dev/null 2>&1; then
  echo "demo inspector not up at $DEMO_URL — run tools/run_demo.sh start first" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"

WIDTH="${WIDTH:-1440}"
HEIGHT="${HEIGHT:-900}"
SETTLE_MS="${SETTLE_MS:-5000}"

PER_SHOT_TIMEOUT="${PER_SHOT_TIMEOUT:-15}"

shoot() {
  local name="$1" url="$2"
  local settle="${3:-$SETTLE_MS}"
  local out="$OUT_DIR/$name.png"
  local user_data
  user_data="$(mktemp -d)"
  echo "  ${name} ← ${url}"
  rm -f "$out"

  # Run Chrome in the background, then enforce a hard timeout via kill.
  # We need this because chromium's --virtual-time-budget can wait
  # indefinitely on localhost pages that have <audio preload> elements
  # or other resources that never finish loading from its perspective.
  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --disable-features=Translate,BackForwardCache,MediaRouter,PreloadMediaEngagementData \
    --autoplay-policy=user-gesture-required \
    --mute-audio \
    --hide-scrollbars \
    --no-first-run \
    --no-default-browser-check \
    --no-sandbox \
    --user-data-dir="$user_data" \
    --window-size="${WIDTH},${HEIGHT}" \
    --virtual-time-budget="${settle}" \
    --run-all-compositor-stages-before-draw \
    --screenshot="$out" \
    "$url" >/dev/null 2>&1 &
  local pid=$!

  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [[ -s "$out" ]] && [[ "$waited" -ge 2 ]]; then
      # PNG exists and we've waited a bit for the encoder to flush —
      # Chrome is hanging on cleanup, kill it and move on.
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    if [[ "$waited" -ge "$PER_SHOT_TIMEOUT" ]]; then
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" 2>/dev/null || true
  rm -rf "$user_data" 2>/dev/null || true

  if [[ ! -s "$out" ]]; then
    echo "    !! capture failed for $name (no bytes written)" >&2
    return 1
  fi
}

echo "capturing screenshots into $OUT_DIR (${WIDTH}x${HEIGHT}, ${SETTLE_MS}ms settle)"

SETTLE_MS_DETAIL="${SETTLE_MS_DETAIL:-8000}"

shoot "01-sessions-list"      "$DEMO_URL/?tab=sessions"
shoot "02-session-detail"     "$DEMO_URL/?tab=sessions&session=demo-001-q1-product-review&solo=1"  "$SETTLE_MS_DETAIL"
shoot "03-customer-discovery" "$DEMO_URL/?tab=sessions&session=demo-002-customer-discovery&solo=1" "$SETTLE_MS_DETAIL"
shoot "04-char-audit"         "$DEMO_URL/?tab=char"
shoot "05-config"             "$DEMO_URL/?tab=config"
shoot "06-about"              "$DEMO_URL/?tab=about"

cat >"$OUT_DIR/index.json" <<EOF
{
  "captured_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "viewport": "${WIDTH}x${HEIGHT}",
  "demo_url": "$DEMO_URL",
  "shots": [
    {"file": "01-sessions-list.png",     "url": "$DEMO_URL/?tab=sessions",                                                 "caption": "Sessions tab — the card grid of every Char session on disk."},
    {"file": "02-session-detail.png",    "url": "$DEMO_URL/?tab=sessions&session=demo-001-q1-product-review",              "caption": "A team-meeting session with per-speaker diarization + per-paragraph confidence."},
    {"file": "03-customer-discovery.png","url": "$DEMO_URL/?tab=sessions&session=demo-002-customer-discovery",             "caption": "A 2-speaker customer-discovery session with the summary note rendered inline."},
    {"file": "04-char-audit.png",        "url": "$DEMO_URL/?tab=char",                                                     "caption": "Char-audit tab — every setting we care about, OK / WARN / INFO."},
    {"file": "05-config.png",            "url": "$DEMO_URL/?tab=config",                                                   "caption": "Config tab — ground-truth ~/.config/local_scribe/config.json with form-bound editing."},
    {"file": "06-about.png",             "url": "$DEMO_URL/?tab=about",                                                    "caption": "About tab — what local_scribe is, what it isn't, and where its docs live."}
  ]
}
EOF

echo
echo "wrote:"
ls -la "$OUT_DIR"/*.png "$OUT_DIR/index.json" 2>/dev/null | awk '{printf "  %6s  %s\n", $5, $NF}'
echo
echo "preview locally:  open $OUT_DIR"
