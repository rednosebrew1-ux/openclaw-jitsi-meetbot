#!/usr/bin/env sh
# Voice calls with your AI on Jitsi Meet, through a self-hosted openclaw-jitsi-meetbot.
#   call.sh call "reason"      the AI joins a room and prints the link to send the user
#   call.sh join [room]        the AI joins a room (a link or room name; the always-on room if none)
#   call.sh wait ID            wait for the call to end; prints status and transcript
#   call.sh hangup ID          end the call
#   call.sh status             is the bridge connected, is a call going, the always-on link
set -eu

# Settings come from the environment, or from ~/.config/openclaw-jitsi-meetbot/skill.env
# (OpenClaw blocks secret-looking env vars from reaching skills).
CONFIG="${MEETBOT_CONFIG:-$HOME/.config/openclaw-jitsi-meetbot/skill.env}"
if [ -z "${MEETBOT_TOKEN:-}" ] && [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
fi
MEETBOT_URL="${MEETBOT_URL:-http://localhost:8090}"
: "${MEETBOT_TOKEN:?no token: put MEETBOT_TOKEN=... (the server API_TOKEN) in $CONFIG}"

api() {  # api METHOD PATH [JSON] [curl options...]
  method=$1 path=$2 body=${3:-}
  shift 2; [ $# -gt 0 ] && shift
  curl -sS --fail-with-body -X "$method" "$MEETBOT_URL$path" \
    -H "Authorization: Bearer $MEETBOT_TOKEN" -H "Content-Type: application/json" \
    ${body:+-d "$body"} "$@"
  echo
}

json() {  # json key value [key value...] -> {"key": "value", ...}
  python3 -c 'import json,sys; a=sys.argv[1:]; print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  call)   api POST /api/call "$(json reason "${1:?usage: call.sh call REASON}")" ;;
  join)   api POST /api/join "$(json link "${1:-}" reason "${2:-}")" ;;
  wait)   api GET "/api/calls/${1:?usage: call.sh wait ID}?wait=1&timeout=1800" "" --max-time 1860 ;;
  hangup) api POST "/api/calls/${1:?usage: call.sh hangup ID}/hangup" ;;
  status) api GET /api/status ;;
  *) sed -n '2,7p' "$0" >&2; exit 2 ;;
esac
