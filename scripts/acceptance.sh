#!/usr/bin/env bash
# ServerStream acceptance test - proves the live/standby/closed state machine
# against a real platform endpoint.
#
#   scp scripts/acceptance.sh root@YOUR_HOST:/tmp/acc.sh
#   ssh root@YOUR_HOST 'bash /tmp/acc.sh'
#
# Creates a throwaway SRS ("sink") as a stand-in for YouTube/Facebook, a
# throwaway platform target in the registry, and a test-pattern RTMPS
# publisher as a stand-in for OBS. Nothing real is broadcast and the test
# cleans up after itself.
#
# Asserts:
#   1. with no OBS ever connected, NOTHING is sent to the platforms
#   2. when OBS connects, platforms receive the live feed
#   3. when OBS is hard-killed, standby takes over within seconds
#   4. after the operator closes, it stays closed and never drifts back
#   5. when OBS returns unattended, streaming auto-resumes
#   6. dashboard writes sent as text/plain are parsed (the fetch() footgun)
#
# Exits non-zero if any assertion fails.
C="curlimages/curl:latest"
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
KEY=$(grep -oP '(?<=^STREAM_KEY=).*' /opt/serversstream/.env)
# Kill any leftover OBS publishers / sinks from a previous run.
docker rm -f sscli sink obsA obsB obsC obsD obsE obsF obsG obs1 obs2 obsZ obs_sim obsfg obsz >/dev/null 2>&1
# Restart the manager: sessions live in memory, so a restart after logging in
# would immediately invalidate the cookie.
docker restart serversstream-manager >/dev/null
sleep 14
echo "### clean baseline established (manager restarted, no OBS publishers)"
echo

# Start the fresh test harness containers.
docker run -d --rm --name sink --network serversstream ossrs/srs:5 >/dev/null
docker run -d --rm --name sscli --network serversstream -v ssjar:/j \
  alpine:3.20 sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 1800' >/dev/null
sleep 9

xc(){ docker exec sscli curl -s "$@"; }

# Test platforms are named "ZZ ...". Purge leftovers by NAME rather than a
# captured id: a run that dies midway must not leave junk in the registry.
purge_test_platforms(){
  local state
  state=$(xc -b /j/c http://manager:8081/api/state)
  # Find IDs of platforms whose name starts with ZZ.
  local ids
  ids=$(echo "$state" | grep -o '"id":[0-9]*.*"name":"ZZ[^"]*"' | grep -o '"id":[0-9]*' | sed 's/"id"://')
  for id in $ids; do
    xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" \
       "http://manager:8081/api/platforms/$id" >/dev/null 2>&1
  done
}

# Cookie jar: always start fresh, it lives on a shared persistent volume
# (ssjar) so a previous run's session cookie would otherwise be reused.
rm -f /j/c
xc -c /j/c -X POST -H "Content-Type: application/json" \
   -d "{\"username\":\"admin\",\"password\":\"$PW\"}" http://manager:8081/api/auth/login >/dev/null
# Extract the csrf token with grep+sed.
TOK=$(xc -b /j/c http://manager:8081/api/auth/me | grep -o '"csrf_token":"[^"]*"' | sed 's/"csrf_token":"//;s/"$//')

# Purge ANY leftover test platforms from a previous run before starting.
purge_test_platforms
PID=$(xc -b /j/c -X POST -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"name":"ZZ Sink","full_url":"rtmp://sink:1935/live/out","enabled":true}' \
  http://manager:8081/api/platforms | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
pass=0; fail=0

show(){ xc -b /j/c http://manager:8081/api/state > /tmp/s.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/s.json")); b=d["broadcast"]; rs=d["restreams"]
live=len([r for r in rs if r.get("source")=="live" and r["state"]=="running"])
stby=len([r for r in rs if r.get("source")=="standby" and r["state"]=="running"])
print(f"  [{sys.argv[1]:28s}] phase={b['phase']:8s} armed={str(b['armed']):5s} left={str(b['seconds_left']):>4}s live={live} standby={stby}")
PY
rm -f /tmp/s.json; }
st(){ xc http://srs:1985/api/v1/streams/ > /tmp/k.json
python3 -c "
import json
ss=json.load(open('/tmp/k.json')).get('streams',[])
print('    srs:',sorted(s['name'][:8] for s in ss))"
rm -f /tmp/k.json; }
sinkb(){ xc http://sink:1985/api/v1/streams/ > /tmp/k.json
python3 -c "
import json
ss=json.load(open('/tmp/k.json')).get('streams',[])
print('    sink:',[(s['name'],s['recv_bytes']) for s in ss] or 'NONE  <-- platform receiving NOTHING')"
rm -f /tmp/k.json; }
t(){ if [ "$2" = "$3" ]; then echo "  ✓ PASS  $1"; pass=$((pass+1)); else echo "  ✗ FAIL  $1 (got '$2' want '$3')"; fail=$((fail+1)); fi; }
q(){ xc -b /j/c http://manager:8081/api/state > /tmp/q.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/q.json"))
print(len([r for r in d["restreams"]
          if r.get("source")==sys.argv[1] and r["state"]=="running"]))
PY
rm -f /tmp/q.json; }
field(){ xc -b /j/c http://manager:8081/api/state > /tmp/f.json
python3 - "$1" <<'PY'
import json,sys
print(json.load(open("/tmp/f.json"))["broadcast"][sys.argv[1]])
PY
rm -f /tmp/f.json; }

echo "TEST 1  no OBS ever -> nothing sent to platforms"
show "t+0"; sleep 12; show "t+12"; sleep 20; show "t+32"; st; sinkb
t "phase is closed"      "$(field phase)" "closed"
t "armed is false"       "$(field armed)" "False"
t "0 standby feeds"      "$(q standby)" "0"

echo
echo "TEST 2  OBS connects -> live feed reaches the platform"
docker run -d --rm --network host --name obsF linuxserver/ffmpeg:latest \
  -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 \
  -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60 \
  -c:a aac -ar 44100 -ac 2 -b:a 128k -f flv "rtmps://sonnystream.duckdns.org:1936/live/$KEY" >/tmp/obsF.log 2>&1
sleep 18; show "OBS live"; sinkb
t "phase is live"        "$(field phase)" "live"
t "1 live feed"          "$(q live)" "1"

echo
echo "TEST 3  OBS hard-killed -> standby takes over"
docker kill --signal=KILL obsF >/dev/null 2>&1; docker rm -f obsF >/dev/null 2>&1
sleep 12; show "OBS killed"; sinkb
t "phase is standby"     "$(field phase)" "standby"
t "1 standby feed"       "$(q standby)" "1"

echo
echo "TEST 4  operator close -> stays closed, never drifts back"
xc -b /j/c -X POST -H "X-SS-Token: $TOK" http://manager:8081/api/broadcast/close >/dev/null
sleep 8; show "closed"; sleep 25; show "closed +25s"; sinkb
t "phase is closed"      "$(field phase)" "closed"
t "0 standby feeds"      "$(q standby)" "0"
t "0 live feeds"         "$(q live)" "0"

echo
echo "TEST 5  OBS returns unattended -> auto-resume"
docker run -d --rm --network host --name obsG linuxserver/ffmpeg:latest \
  -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 \
  -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60 \
  -c:a aac -ar 44100 -ac 2 -b:a 128k -f flv "rtmps://sonnystream.duckdns.org:1936/live/$KEY" >/tmp/obsG.log 2>&1
sleep 18; show "OBS back"; sinkb
t "phase is live"        "$(field phase)" "live"

echo
echo "TEST 6  dashboard writes sent as text/plain are still parsed (regression)"
# fetch() labels a string body text/plain unless told otherwise. The dashboard
# shipped without that header, so every write failed with "Input should be a
# valid dictionary or object to extract fields from".
NEW=$(xc -b /j/c -X POST \
   -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
   -d '{"name":"ZZ content-type probe","full_url":"rtmp://sink:1935/live/probe","enabled":true}' \
   http://manager:8081/api/platforms | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('id',''))
except Exception: print('')")
t "create with text/plain body" "$([ -n "$NEW" ] && echo created || echo missing)" "created"
t "the exact call from the bug report works" \
  "$(xc -b /j/c -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK" \
      -d '{"enabled":false}' http://manager:8081/api/platforms/$NEW \
     | python3 -c 'import json,sys;print(int(bool(json.load(sys.stdin).get("enabled"))))')" "0"

xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" http://manager:8081/api/platforms/$PID >/dev/null
purge_test_platforms
docker rm -f sink sscli obsG >/dev/null 2>&1
echo
echo "================ $pass passed, $fail failed ================"
[ "$fail" -eq 0 ]
