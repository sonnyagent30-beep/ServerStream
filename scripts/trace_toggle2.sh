#!/usr/bin/env bash
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
CN=trace2
docker rm -f "$CN" >/dev/null 2>&1
docker run -d --rm --name "$CN" --network serversstream curlimages/curl:latest sh -c "sleep 120" >/dev/null
sleep 8

echo "=== login + get csrf ==="
curl -s -c /j/c -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  http://manager:8081/api/auth/login
echo
TOK=$(docker exec "$CN" curl -s -b /j/c http://manager:8081/api/auth/me \
  | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')
echo "  csrf: ${TOK:0:8}..."

echo "=== list platforms ==="
docker exec "$CN" curl -s -b /j/c http://manager:8081/api/state | python3 -c "
import json,sys
for p in json.load(sys.stdin)['platforms']: print(f\"    {p['name']} id={p['id']} enabled={p['enabled']}\")" 2>/dev/null || echo "  (python in container failed, raw:)"
docker exec "$CN" curl -s -b /j/c http://manager:8081/api/state | grep -o '"name":"[^"]*"\|"id":[0-9]*\|"enabled":[01]' | paste - - -
PID=$(docker exec "$CN" curl -s -b /j/c http://manager:8081/api/state \
  | grep -o '"id":[0-9]*' | head -1 | grep -o '[0-9]*')
echo "  toggling platform id=$PID"

echo
echo "=== PATCH disable (browser-style request) ==="
docker exec "$CN" curl -s -b /j/c -w "\n  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' "http://manager:8081/api/platforms/$PID"

echo
echo "=== PATCH enable ==="
docker exec "$CN" curl -s -b /j/c -w "\n  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"enabled":true}' "http://manager:8081/api/platforms/$PID"

docker rm -f "$CN" >/dev/null 2>&1
