#!/usr/bin/env bash
# ServerStream deploy / update script. Safe to re-run.
set -euo pipefail

DOMAIN="${DOMAIN:-sonnystream.duckdns.org}"
PROJECT_DIR="${PROJECT_DIR:-/opt/serversstream}"
REPO="${REPO:-https://github.com/sonnyagent30-beep/ServerStream.git}"
EMAIL="${EMAIL:-}"

say(){ printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

say "Fetching ServerStream into $PROJECT_DIR"
if [ -d "$PROJECT_DIR/.git" ]; then
  git -C "$PROJECT_DIR" fetch --all --quiet
  git -C "$PROJECT_DIR" reset --hard origin/main --quiet
else
  rm -rf "$PROJECT_DIR"
  git clone --depth 1 "$REPO" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"

say "Environment"
if [ ! -f .env ]; then
  cp .env.example .env
  KEY="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  sed -i "s|^STREAM_KEY=.*|STREAM_KEY=${KEY}|" .env
  sed -i "s|^PUBLIC_HOST=.*|PUBLIC_HOST=${DOMAIN}|" .env
  echo "  created .env with a generated stream key"
else
  echo "  .env already present, leaving it alone"
fi
set -a; . ./.env; set +a

say "Firewall"
for port in 1935 1936; do
  ufw allow "${port}/tcp" comment 'ServerStream ingest' >/dev/null 2>&1 || true
done
ufw allow 80/tcp  >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
echo "  opened 80, 443, 1935, 1936"

say "TLS certificate for $DOMAIN"
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  echo "  certificate already exists"
else
  systemctl stop nginx 2>/dev/null || true
  certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email ${EMAIL:+-m "$EMAIL"} \
    || certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
  systemctl start nginx 2>/dev/null || true
fi

say "Host nginx vhost"
cp nginx/serversstream.conf /etc/nginx/sites-available/serversstream
ln -sf /etc/nginx/sites-available/serversstream /etc/nginx/sites-enabled/serversstream
nginx -t && systemctl reload nginx
echo "  nginx reloaded"

say "Containers"
docker compose up -d --build --remove-orphans
sleep 6
docker compose ps

say "Verifying"
curl -fsS "http://127.0.0.1:8081/api/health" && echo
echo
echo "Dashboard : https://$DOMAIN"
echo "OBS (RTMPS): rtmps://$DOMAIN:1936/live"
echo "OBS (RTMP) : rtmp://$DOMAIN:1935/live"
echo "Stream key : ${STREAM_KEY:-see .env}"
