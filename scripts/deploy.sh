#!/usr/bin/env bash
# ServerStream deploy / update script. Safe to re-run.
set -euo pipefail

# SS_HOSTNAME: your domain or bare IP that OBS will connect to.
#   Required, and deliberately prefixed: HOSTNAME is a standard environment
#   variable on many systems (Windows, systemd), so reading it would silently
#   pick up the machine's own name instead of the domain you meant. There is no
#   default either - a wrong default would point certificates and nginx
#   server_name at somebody else's server.
SS_HOSTNAME="${SS_HOSTNAME:-${DOMAIN:-}}"
PROJECT_DIR="${PROJECT_DIR:-/opt/serversstream}"
REPO="${REPO:-https://github.com/sonnyagent30-beep/ServerStream.git}"
EMAIL="${EMAIL:-}"

say(){ printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }

if [ -z "${SS_HOSTNAME}" ]; then
  cat >&2 <<'USAGE'
Usage: sudo SS_HOSTNAME=your.domain ./scripts/deploy.sh [ADMIN_PASSWORD=...]

  SS_HOSTNAME     (required) domain or IP that OBS connects to (env var)
  ADMIN_PASSWORD  admin password for the dashboard (prompted if omitted)
  EMAIL           optional email for the ACME account

Example:
  sudo SS_HOSTNAME=stream.example.com ./scripts/deploy.sh
USAGE
  exit 2
fi
DOMAIN="${SS_HOSTNAME}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (use sudo)"; exit 1; }


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
NEW_ENV=0
if [ ! -f .env ]; then
  cp .env.example .env
  NEW_ENV=1
  KEY="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
  sed -i "s|^STREAM_KEY=.*|STREAM_KEY=${KEY}|" .env
  sed -i "s|^PUBLIC_HOST=.*|PUBLIC_HOST=${DOMAIN}|" .env
  echo "  created .env with a generated stream key"
else
  echo "  .env already present, leaving it alone"
fi

# Dashboard credentials. Prompted when not supplied so the password never
# lands in shell history or a CI log.
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
if [ -z "${ADMIN_PASSWORD}" ]; then
  if [ "${NEW_ENV}" -eq 1 ]; then
    read -r -s -p "Dashboard admin password: " ADMIN_PASSWORD; echo
  elif [ "${FORCE_PASSWORD:-0}" = "1" ]; then
    read -r -s -p "Dashboard admin password: " ADMIN_PASSWORD; echo
  elif grep -q '^ADMIN_PASSWORD=change-me$\|^ADMIN_PASSWORD=$' .env 2>/dev/null; then
    read -r -s -p "Dashboard admin password (currently 'change-me'): " ADMIN_PASSWORD; echo
  fi
fi
if [ -n "${ADMIN_PASSWORD}" ]; then
  ESCAPED="$(printf '%s' "${ADMIN_PASSWORD}" | sed 's/[\\&|]/\\&/g')"
  if grep -q '^ADMIN_PASSWORD=' .env; then
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ESCAPED}|" .env
  else
    printf '\nADMIN_USER=%s\nADMIN_PASSWORD=%s\n' \
      "${ADMIN_USER:-admin}" "${ESCAPED}" >> .env
  fi
  echo "  dashboard admin password set"
elif grep -q '^ADMIN_PASSWORD=change-me$\|^ADMIN_PASSWORD=$' .env 2>/dev/null; then
  echo "  WARNING: dashboard password is still 'change-me' (insecure!)."
  echo "  Set a real password with: sudo SS_HOSTNAME=$DOMAIN ADMIN_PASSWORD=... ./scripts/deploy.sh"
fi
chmod 600 .env

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
sed "s|__DOMAIN__|${DOMAIN}|g" nginx/serversstream.conf > /etc/nginx/sites-available/serversstream
ln -sf /etc/nginx/sites-available/serversstream /etc/nginx/sites-enabled/serversstream
nginx -t && systemctl reload nginx
echo "  nginx reloaded"

say "Edge (RTMPS) config"
sed "s|__DOMAIN__|${DOMAIN}|g" edge/nginx.conf > edge/nginx.conf.rendered

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
