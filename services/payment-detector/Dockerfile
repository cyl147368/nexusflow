FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/vendor \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    GPT_REGISTER_CONFIG_DIR=/app/config \
    TZ=Asia/Shanghai

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium
COPY service.py .
COPY vendor ./vendor

RUN mkdir -p /app/config /app/results /app/data
EXPOSE 8091
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
