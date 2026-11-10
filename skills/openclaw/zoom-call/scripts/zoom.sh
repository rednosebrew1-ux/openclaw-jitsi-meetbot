#!/usr/bin/env sh
# Zoom calls with your AI, through a self-hosted openclaw-zoom-bot.
#   zoom.sh call "reason"      the AI opens a meeting and joins; prints the join link
#   zoom.sh join [link]        the AI joins your meeting (your personal meeting if no link)
#   zoom.sh wait ID            wait for the call to end; prints status and transcript
#   zoom.sh hangup ID          make the AI leave
#   zoom.sh status             is the bridge connected, is a call going
set -eu

# Settings come from the environment, or from ~/.config/openclaw-zoom-bot/skill.env
# (OpenClaw blocks secret-looking env vars from reaching skills).
CONFIG="${ZOOM_BOT_CONFIG:-$HOME/.config/openclaw-zoom-bot/skill.env}"
if [ -z "${ZOOM_BOT_TOKEN:-}" ] && [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
fi
ZOOM_BOT_URL="${ZOOM_BOT_URL:-http://localhost:8090}"
: "${ZOOM_BOT_TOKEN:?no token: put ZOOM_BOT_TOKEN=... (the server's API_TOKEN) in $CONFIG}"

api() {  # api METHOD PATH [JSON] [curl options...]
  method=$1 path=$2 body=${3:-}
  shift 2; [ $# -gt 0 ] && shift
  curl -sS --fail-with-body -X "$method" "$ZOOM_BOT_URL$path" \
    -H "Authorization: Bearer $ZOOM_BOT_TOKEN" -H "Content-Type: application/json" \
    ${body:+-d "$body"} "$@"
  echo
}

json() {  # json key value [key value...] -> {"key": "value", ...}
  python3 -c 'import json,sys; a=sys.argv[1:]; print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  call)   api POST /api/call "$(json reason "${1:?usage: zoom.sh call \"reason\"}")" ;;
  join)   api POST /api/join "$(json link "${1:-}" reason "${2:-}" obf_token "${ZOOM_OBF_TOKEN:-}")" ;;
  wait)   api GET "/api/calls/${1:?usage: zoom.sh wait ID}?wait=1&timeout=1800" "" --max-time 1860 ;;
  hangup) api POST "/api/calls/${1:?usage: zoom.sh hangup ID}/hangup" ;;
  status) api GET /api/status ;;
  *) sed -n '2,7p' "$0" >&2; exit 2 ;;
esac
