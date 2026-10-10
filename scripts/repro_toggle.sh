#!/usr/bin/env bash
# Reproduce the EXACT browser click flow against the LIVE public URL.
set -u
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
curl -s -c /tmp/cj_pub -X POST -H "Content-Type: application/json" \
     -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
     https://sonnystream.duckdns.org/api/auth/login >/dev/null
ME=$(curl -s -b /tmp/cj_pub https://sonnystream.duckdns.org/api/auth/me)
TOK=$(echo "$ME" | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")
PID=$(curl -s -b /tmp/cj_pub https://sonnystream.duckdns.org/api/state | \
      python3 -c "import json,sys;ps=json.load(sys.stdin)['platforms'];print(ps[0]['id'] if ps else '')")
echo "csrf=$TOK  target_platform_id=$PID"
echo
echo "=== the EXACT request the browser makes on toggle (correct header) ==="
curl -sv -b /tmp/cj_pub \
  -X PATCH \
  -H "Content-Type: application/json" \
  -H "X-SS-Token: $TOK" \
  -d "{\"enabled\":false}" \
  "https://sonnystream.duckdns.org/api/platforms/$PID" 2>&1 | grep -iE "<<< HTTP|Strict|Secure|set-cookie|{\"id" | head
echo
echo "=== same call but body sent as text/plain (what shipped BEFORE my fix) ==="
curl -s -b /tmp/cj_pub -w "\nHTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
  -d "{\"enabled\":false}" "https://sonnystream.duckdns.org/api/platforms/$PID"
echo
echo "=== reset it back to enabled ==="
curl -s -b /tmp/cj_pub -o /dev/null -w "reset HTTP %{http_code}\n" \
  -X PATCH -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d "{\"enabled\":true}" "https://sonnystream.duckdns.org/api/platforms/$PID"
rm -f /tmp/cj_pub
