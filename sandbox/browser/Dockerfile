# One disposable browser sandbox: headful Chromium on a virtual display,
# viewable by a human over noVNC and drivable by the agent over CDP.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify fluxbox socat fonts-liberation fonts-noto-color-emoji \
    && pip install --no-cache-dir playwright==1.63.0 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 sk && mkdir -p /data && chown sk /data
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER sk
# 6080 = noVNC (human live view), 9222 = CDP (agent control)
EXPOSE 6080 9222
ENTRYPOINT ["/entrypoint.sh"]
