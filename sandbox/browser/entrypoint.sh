#!/bin/bash
set -u
W=${SCREEN_WIDTH:-1280}
H=${SCREEN_HEIGHT:-800}

Xvfb :99 -screen 0 ${W}x${H}x24 -nolisten tcp &
sleep 1
fluxbox >/dev/null 2>&1 &
x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# Chrome only binds CDP to localhost, so socat exposes it on the container interface.
socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223 &

CHROME=$(ls -d /opt/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
[ -x "$CHROME" ] || { echo "chromium binary not found" >&2; exit 1; }
while true; do
  "$CHROME" --no-sandbox --test-type --no-first-run --no-default-browser-check \
    --remote-debugging-port=9223 --user-data-dir=/data/profile \
    --window-position=0,0 --window-size=${W},${H} --start-maximized \
    "${START_URL:-about:blank}"
  sleep 1
done
