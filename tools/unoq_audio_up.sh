#!/bin/bash
# Bring up PipeWire + WirePlumber + pipewire-pulse for the `arduino` user on the Uno Q
# without a systemd user session (no root / no linger needed). Idempotent.
#
# Needed so BlueZ has an A2DP endpoint: without it the RB Meta glasses connect and
# immediately drop ("Connected: no") because no audio profile can be attached.
#
# Usage: source tools/unoq_audio_up.sh   (or bash it; env is also written to ~/.audio_env)

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/home/arduino/.run}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
mkdir -p -m 700 "$XDG_RUNTIME_DIR"

cat > /home/arduino/.audio_env <<ENV
export XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR
export DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS
export PULSE_SERVER=$PULSE_SERVER
ENV

# session bus (WirePlumber's rtkit/portal lookups want one; harmless if unused)
if ! dbus-send --session --dest=org.freedesktop.DBus --type=method_call --print-reply \
      /org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    rm -f "$XDG_RUNTIME_DIR/bus"
    dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile
fi

start() {  # start <name> <cmd...> if not already running under this runtime dir
    local name=$1; shift
    if pgrep -u arduino -x "$name" >/dev/null; then echo "[audio] $name already running"; return; fi
    nohup "$@" >"$XDG_RUNTIME_DIR/$name.log" 2>&1 &
    echo "[audio] started $name (pid $!)"
}
start pipewire pipewire
sleep 1
# main-systemwide: disables logind seat-monitoring, which otherwise blocks the BlueZ
# monitor because the adb/arduino user has no logind session.
start wireplumber wireplumber -p main-systemwide
start pipewire-pulse pipewire-pulse
sleep 2

echo "[audio] pipewire: $(pgrep -x pipewire >/dev/null && echo up || echo DOWN)  wireplumber: $(pgrep -x wireplumber >/dev/null && echo up || echo DOWN)  pipewire-pulse: $(pgrep -x pipewire-pulse >/dev/null && echo up || echo DOWN)"
wpctl status 2>/dev/null | sed -n '/Audio/,/Video/p' | grep -vE '^\s*$' | head -30
