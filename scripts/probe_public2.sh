#!/usr/bin/env bash
# Probe the public-URL write path cleanly.
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
CN="sscurl-probe-$$"
docker rm -f "$CN" >/dev/null 2>&1
docker run -d --rm --name "$CN" --network serversstream -v ssjar7:/j \
  curlimages/curl:latest sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 300'
sleep 9

echo "=== login (internal) ==="
docker exec "$CN" curl -s -c /j/c -o /dev/null \
  -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  http://manager:8081/api/auth/login
TOK=$(docker exec "$CN" curl -s -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")
echo "  csrf: ${TOK:0:10}...  (len=${#TOK})"

echo
echo "=== PATCH internal (should be 200) ==="
docker exec "$CN" curl -s -w "  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' http://manager:8081/api/platforms/1

echo
echo "=== PATCH public (the user's failure path) ==="
docker exec "$CN" curl -s -w "  HTTP %{http_code}  <- the bug\n" \
  -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' https://sonnystream.duckdns.org/api/platforms/1

echo
echo "=== nginx ==="
sleep 1
docker exec serversstream-edge tail -8 /var/log/nginx/access.log 2>/dev/null | grep "PATCH" | tail -2 || echo "(access log empty)"
docker exec serversstream-edge tail -5 /var/log/nginx/error.log 2>/dev/null || echo "(no errors)"

docker rm -f "$CN" >/dev/null 2>&1
