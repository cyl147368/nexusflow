#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"

display_number="${DISPLAY#:}"
socket_path="/tmp/.X11-unix/X${display_number}"
lock_path="/tmp/.X${display_number}-lock"

# A container restart preserves /tmp. If the old Xvfb process is gone, its
# socket and lock files can survive and make a socket-only check skip startup.
if ! pgrep -x Xvfb >/dev/null 2>&1; then
    rm -f "$socket_path" "$lock_path"
fi

if ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp -ac &
    xvfb_pid=$!
    attempts=0
    while [ ! -S "$socket_path" ] && [ "$attempts" -lt 50 ]; do
        if ! kill -0 "$xvfb_pid" 2>/dev/null; then
            echo "Xvfb exited before its display became ready" >&2
            exit 1
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    if [ ! -S "$socket_path" ]; then
        echo "Xvfb did not become ready on $DISPLAY" >&2
        exit 1
    fi
fi

exec python start_webui.py --host 0.0.0.0 --port 8765 --no-browser
