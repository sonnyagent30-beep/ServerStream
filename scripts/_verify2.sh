#!/bin/bash
C="curlimages/curl:latest"
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
KEY=$(grep -oP '(?<=^STREAM_KEY=).*' /opt/serversstream/.env)

echo "=== 1. restart manager = genuinely fresh state, then watch 40s with no OBS ==="
docker restart serversstream-manager >/dev/null
sleep 10
docker logs serversstream-manager 2>&1 | grep -E "phase ->|armed" | tail -2

docker rm -f sscli >/dev/null 2>&1
docker run -d --rm --name sscli --network serversstream -v ssjar:/j \
  alpine:3.20 sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 1800' >/dev/null
sleep 8
xc(){ docker exec sscli curl -s "$@"; }
xc -c /j/c -X POST -H "Content-Type: application/json" \
   -d "{\"username\":\"admin\",\"password\":\"$PW\"}" http://manager:8081/api/auth/login >/dev/null
TOK=$(xc -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")

show(){ xc -b /j/c http://manager:8081/api/state > /tmp/ss.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/ss.json")); b=d["broadcast"]
rs=d["restreams"]
live=[r for r in rs if r.get("source")=="live" and r["state"]=="running"]
stby=[r for r in rs if r.get("source")=="standby" and r["state"]=="running"]
print(f"  [{sys.argv[1]:26s}] phase={b['phase']:8s} armed={str(b['armed']):5s} "
      f"left={str(b['seconds_left']):>4}s | live_feeds={len(live)} standby_feeds={len(stby)} | srs={sorted(s[:8] for s in d['srs']['streams'])}")
PY
rm -f /tmp/ss.json; }

show "fresh boot +0s"
sleep 15; show "fresh boot +15s"
sleep 20; show "fresh boot +35s  (BUG CHECK)"

echo
echo "=== 2. enable one test platform -> a sink SRS (stands in for YouTube) ==="
docker rm -f sink >/dev/null 2>&1
docker run -d --rm --name sink --network serversstream ossrs/srs:5 >/dev/null
sleep 4
PID=$(xc -b /j/c -X POST -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"name":"ZZ Test Sink","full_url":"rtmp://sink:1935/live/testout","enabled":true}' \
  http://manager:8081/api/platforms | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
echo "  created test platform id=$PID"
show "test platform enabled, no OBS"

pub(){ docker run -d --rm --network host --name $1 linuxserver/ffmpeg:latest \
   -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 \
   -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60 \
   -c:a aac -ar 44100 -ac 2 -b:a 128k -f flv \
   "rtmps://sonnystream.duckdns.org:1936/live/$KEY" > /tmp/$1.log 2>&1; }

echo
echo "=== 3. OBS connects -> platform should get the LIVE feed ==="
pub obsC; sleep 14
show "OBS live"
echo -n "  sink received: "
xc http://sink:1985/api/v1/streams/ | python3 -c "import json,sys;print([(s['name'],s['recv_bytes']) for s in json.load(sys.stdin).get('streams',[])])"

echo
echo "=== 4. OBS drops -> platform should switch to the STANDBY frame ==="
docker rm -f obsC >/dev/null 2>&1; sleep 10
show "OBS dropped"
echo -n "  sink received: "
xc http://sink:1985/api/v1/streams/ | python3 -c "import json,sys;print([(s['name'],s['recv_bytes']) for s in json.load(sys.stdin).get('streams',[])])"

echo
echo "=== cleanup ==="
xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" http://manager:8081/api/platforms/$PID >/dev/null
docker rm -f sink sscli >/dev/null 2>&1
echo "  test platform removed, sink removed"
