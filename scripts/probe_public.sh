#!/usr/bin/env bash
# Probe the public-URL write path cleanly.
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
docker rm -f sscurl >/dev/null 2>&1
docker run -d --rm --network serversstream -v ssjar6:/j sscurl_img \
  curlimages/curl:latest sh -c 'apk add --no-cache curl >/dev/null 2>&1 || true; sleep 600' 2>/dev/null || \
docker run -d --rm --network serversstream -v ssjar6:/j \
  curlimages/curl:latest sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 600'
sleep 9

echo "=== login (internal), get csrf ==="
docker exec sscurl curl -s -c /j/c -o /dev/null \
  -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  http://manager:8081/api/auth/login
TOK=$(docker exec sscurl curl -s -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")
echo "  csrf: ${TOK:0:10}..."

echo
echo "=== write through INTERNAL manager address ==="
docker exec sscurl curl -s -o /dev/null -w "  PATCH internal  HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' http://manager:8081/api/platforms/1

echo
echo "=== write through PUBLIC URL (repro the user's failure) ==="
docker exec sscurl curl -s -o /dev/null -w "  PATCH public    HTTP %{http_code}  (the bug)\n" \
  -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
  -d '{"enabled":false}' https://sonnystream.duckdns.org/api/platforms/1

echo
echo "=== what nginx logs for the public attempt ==="
sleep 1
docker exec serversstream-edge tail -5 /var/log/nginx/access.log 2>/dev/null | grep -E "PATCH|POST|api/" | tail -3 || echo "(none yet)"
docker exec serversstream-edge tail -8 /var/log/nginx/error.log 2>/dev/null || echo "(no error log / no errors)"

docker rm -f sscurl >/dev/null 2>&1
echo "done"
