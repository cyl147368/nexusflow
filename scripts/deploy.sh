#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "[NexusFlow] Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

PUBLIC_IP="${NEXUSFLOW_PUBLIC_IP:-$(curl -4fsS --max-time 5 https://api.ipify.org || true)}"
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"

if [[ ! -f .env ]]; then
  umask 077
  cat > .env <<EOF
NEXUSFLOW_BASIC_USER=admin
NEXUSFLOW_BASIC_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-28)
NEXUSFLOW_PORT=8770
MAILHUB_PORT=8090
MAILHUB_SMTP_PUBLIC_PORT=2525
MAILHUB_PUBLIC_URL=http://${PUBLIC_IP}:8090
MAILHUB_ADMIN_TOKEN=$(openssl rand -hex 32)
ICLOUD_PRIME_API_TOKEN=$(openssl rand -hex 32)
TZ=Asia/Shanghai
EOF
  echo "[NexusFlow] Created .env with generated secrets."
fi

ensure_env() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" .env; then
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

ensure_env PAYMENT_DETECTOR_TOKEN "$(openssl rand -hex 32)"

docker compose pull --ignore-buildable || true
docker compose build
docker compose up -d

echo
echo "[NexusFlow] Deployment complete"
echo "  Main UI:  http://${PUBLIC_IP}:8770/"
echo "  Mail Hub: http://${PUBLIC_IP}:8090/api/health (Bearer token required)"
echo "  Login user: $(grep '^NEXUSFLOW_BASIC_USER=' .env | cut -d= -f2-)"
echo
echo "The generated password is stored in $ROOT/.env (mode 600) and is never printed to CI logs."
