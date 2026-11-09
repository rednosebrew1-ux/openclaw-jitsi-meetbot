#!/bin/sh
# The Zoom SDK wants a working sound system even though the bridge only uses raw audio:
# start a private PulseAudio with a silent output and point ALSA at it.
set -eu

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
rm -rf "$XDG_RUNTIME_DIR/pulse"

printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > "$HOME/.asoundrc"
mkdir -p "$HOME/.config"
printf '[General]\nsystem.audio.type=default\n' > "$HOME/.config/zoomus.conf"

pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --log-target=stderr
i=0
until pactl info >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 50 ] && { echo "PulseAudio didn't start" >&2; exit 1; }
  sleep 0.1
done
pactl load-module module-null-sink sink_name=zoom >/dev/null
pactl set-default-sink zoom
pactl set-default-source zoom.monitor

exec "$@"
