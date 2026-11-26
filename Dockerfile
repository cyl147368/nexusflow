FROM node:22-alpine AS web-builder
WORKDIR /src/webui/frontend
COPY webui/frontend/package*.json ./
RUN npm ci
COPY webui/frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEXUSFLOW_HOST=0.0.0.0 \
    NEXUSFLOW_PORT=8765 \
    NEXUSFLOW_DB_PATH=/app/runtime/webui.db \
    NEXUSFLOW_LOG_DIR=/app/runtime/logs \
    DISPLAY=:99 \
    BROWSER_FALLBACK_HEADLESS=0

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        nodejs \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libx11-xcb1 \
        libasound2 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libgbm1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        libxshmfence1 \
        libxt6 \
        libxtst6 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        fonts-liberation \
        fonts-noto-color-emoji \
        fontconfig \
        xvfb \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium \
    && python -m camoufox fetch
COPY . .
COPY --from=web-builder /src/webui/static ./webui/static
RUN mkdir -p /app/runtime/logs \
    && chmod +x /app/scripts/start_nexusflow.sh

EXPOSE 8765
CMD ["/app/scripts/start_nexusflow.sh"]
