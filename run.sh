#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────
#  Shorts Clipper — Termux Setup & Run Script
# ─────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.shorts_clipper_env"

BLUE='\033[1;34m'; GREEN='\033[1;32m'; RED='\033[1;31m'; YELLOW='\033[1;33m'; NC='\033[0m'

header() { echo -e "\n${BLUE}══════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}══════════════════════════════════════${NC}"; }
ok()     { echo -e "${GREEN}✓  $1${NC}"; }
warn()   { echo -e "${YELLOW}⚠  $1${NC}"; }
err()    { echo -e "${RED}✗  $1${NC}"; }

# ── Load saved keys ────────────────────────────
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

# ── 1. System packages ─────────────────────────
header "Checking system packages"
pkg install -y python ffmpeg yt-dlp tor 2>/dev/null | grep -E "newest|already" || true
ok "System packages ready"

# ── 2. Python packages ─────────────────────────
header "Checking Python packages"
pip install -q requests[socks] google-generativeai 2>/dev/null
ok "Python packages ready"

# ── 3. Start Tor ───────────────────────────────
header "Tor Proxy"
# Kill any stale Tor process first
pkill -x tor 2>/dev/null

tor --quiet &
TOR_PID=$!

echo -n "  Waiting for Tor to connect"
for i in $(seq 1 20); do
  sleep 2
  if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',9050)); s.close()" 2>/dev/null; then
    echo ""
    ok "Tor is running (pid $TOR_PID)"
    break
  fi
  echo -n "."
  if [ "$i" -eq 20 ]; then
    echo ""
    warn "Tor didn't start in time — continuing without proxy"
  fi
done

# ── 4. Environment variables ───────────────────
header "Environment Variables"

KEYS_CHANGED=0

if [ -n "$GEMINI_API_KEY" ]; then
  ok "GEMINI_API_KEY  already set (${GEMINI_API_KEY:0:8}...)"
else
  echo ""
  warn "Gemini API key not found."
  echo "    Get one free at: https://aistudio.google.com/app/apikey"
  echo -n "    Paste your Gemini API key: "
  read -r GEMINI_API_KEY
  KEYS_CHANGED=1
fi

if [ -n "$YOUTUBE_API_KEY" ]; then
  ok "YOUTUBE_API_KEY already set (${YOUTUBE_API_KEY:0:8}...)"
else
  echo ""
  warn "YouTube API key not found."
  echo "    Get one at: https://console.cloud.google.com"
  echo "    (APIs & Services → YouTube Data API v3 → Create key)"
  echo -n "    Paste your YouTube API key: "
  read -r YOUTUBE_API_KEY
  KEYS_CHANGED=1
fi

if [ "$KEYS_CHANGED" -eq 1 ]; then
  cat > "$ENV_FILE" <<EOF
export GEMINI_API_KEY="$GEMINI_API_KEY"
export YOUTUBE_API_KEY="$YOUTUBE_API_KEY"
EOF
  chmod 600 "$ENV_FILE"
  ok "Keys saved to $ENV_FILE"
fi

# ── 5. Cookies ─────────────────────────────────
header "Cookies"
if [ -f "$SCRIPT_DIR/cookies.txt" ] && [ -s "$SCRIPT_DIR/cookies.txt" ]; then
  ok "cookies.txt found ($(wc -l < "$SCRIPT_DIR/cookies.txt") lines)"
else
  warn "No cookies.txt — yt-dlp may hit rate limits"
fi

# ── 6. Run ─────────────────────────────────────
header "Run Shorts Clipper"
echo -n "    Enter YouTube channel (e.g. @freecodecamp): "
read -r CHANNEL

if [ -z "$CHANNEL" ]; then
  err "No channel entered. Exiting."
  kill $TOR_PID 2>/dev/null
  exit 1
fi

echo ""
ok "Starting clipper for $CHANNEL ..."
echo ""

cd "$SCRIPT_DIR" && \
  GEMINI_API_KEY="$GEMINI_API_KEY" \
  YOUTUBE_API_KEY="$YOUTUBE_API_KEY" \
  python shorts_clipper.py "$CHANNEL"

# Clean up Tor when done
kill $TOR_PID 2>/dev/null