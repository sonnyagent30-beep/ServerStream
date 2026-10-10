#!/usr/bin/env bash
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
CN=trace1
docker rm -f "$CN" >/dev/null 2>&1
docker run -d --rm --name "$CN" --network serversstream curlimages/curl:latest \
  sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 120' >/dev/null
sleep 8

echo "=== login, get csrf, list platforms ==="
curl -s -c /j/c -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  http://manager:8081/api/auth/login >/dev/null
curl -s -b /j/c http://manager:8081/api/state -o /j/state.json
echo "  platforms:"
docker exec "$CN" python3 -c "
import json
d=json.load(open('/j/state.json'))
for p in d['platforms']: print(f\"    {p['name']:26s} id={p['id']} enabled={p['enabled']}\")"

PID=$(docker exec "$CN" python3 -c "import json;print(json.load(open('/j/state.json'))['platforms'][0]['id'])")
TOK=$(curl -s -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin)['csrf_token'])")

echo
echo "=== toggle OFF (PATCH, body as the browser sends it) ==="
R1=$(curl -s -b /j/c -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d "{\"enabled\":false}" "http://manager:8081/api/platforms/$PID")
echo "  response: $R1"

echo "=== toggle ON ==="
R2=$(curl -s -b /j/c -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d "{\"enabled\":true}" "http://manager:8081/api/platforms/$PID")
echo "  response: $R2"

docker rm -f "$CN" >/dev/null 2>&1
