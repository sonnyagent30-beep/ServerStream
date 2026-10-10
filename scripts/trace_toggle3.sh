#!/usr/bin/env bash
# Use HOST curl + local cookie jar. No container-in-container volume nonsense.
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
CJ=/tmp/trace_cj.txt
rm -f "$CJ"

echo "=== login via PUBLIC https URL ==="
curl -s -c "$CJ" -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  https://sonnystream.duckdns.org/api/auth/login
echo

echo "=== cookie jar ==="
cat "$CJ"

echo
echo "=== /api/auth/me ==="
curl -s -b "$CJ" https://sonnystream.duckdns.org/api/auth/me
echo

TOK=$(curl -s -b "$CJ" https://sonnystream.duckdns.org/api/auth/me \
  | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')
echo "csrf token: ${TOK:0:8}..."

echo
echo "=== GET /api/state (list platforms) ==="
curl -s -b "$CJ" https://sonnystream.duckdns.org/api/state | \
  sed -n 's/.*"platforms":\[/&/; s/.*"name":"\([^"]*\)".*"id":\([0-9]*\).*"enabled":\([01]\)/  \1 id=\2 enabled=\3/p'

PID=$(curl -s -b "$CJ" https://sonnystream.duckdns.org/api/state \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['platforms'][0]['id'])" 2>/dev/null)
echo "toggling platform id=$PID"

echo
echo "=== PATCH disable ==="
curl -s -b "$CJ" -w "\n  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' "https://sonnystream.duckdns.org/api/platforms/$PID"

echo
echo "=== PATCH enable ==="
curl -s -b "$CJ" -w "\n  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"enabled":true}' "https://sonnystream.duckdns.org/api/platforms/$PID"

rm -f "$CJ"
