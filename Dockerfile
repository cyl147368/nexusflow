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
    NEXUSFLOW_LOG_DIR=/app/runtime/logs

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=web-builder /src/webui/static ./webui/static
RUN mkdir -p /app/runtime/logs

EXPOSE 8765
CMD ["python", "start_webui.py", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
