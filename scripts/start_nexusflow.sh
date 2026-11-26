#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"

if [ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp -ac &
    xvfb_pid=$!
    attempts=0
    while [ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ] && [ "$attempts" -lt 50 ]; do
        if ! kill -0 "$xvfb_pid" 2>/dev/null; then
            echo "Xvfb exited before its display became ready" >&2
            exit 1
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    if [ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
        echo "Xvfb did not become ready on $DISPLAY" >&2
        exit 1
    fi
fi

exec python start_webui.py --host 0.0.0.0 --port 8765 --no-browser
