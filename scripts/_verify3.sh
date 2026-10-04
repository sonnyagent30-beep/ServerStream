#!/bin/bash
C="curlimages/curl:latest"
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
KEY=$(grep -oP '(?<=^STREAM_KEY=).*' /opt/serversstream/.env)

docker rm -f sscli sink obsC obsD >/dev/null 2>&1
docker run -d --rm --name sink --network serversstream ossrs/srs:5 >/dev/null
docker run -d --rm --name sscli --network serversstream -v ssjar:/j \
  alpine:3.20 sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 1800' >/dev/null
sleep 8
xc(){ docker exec sscli curl -s "$@"; }
xc -c /j/c -X POST -H "Content-Type: application/json" \
   -d "{\"username\":\"admin\",\"password\":\"$PW\"}" http://manager:8081/api/auth/login >/dev/null
TOK=$(xc -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")
PID=$(xc -b /j/c -X POST -H "Content-Type: application/json" -H "X-SS-Token: $TOK" \
  -d '{"name":"ZZ Test Sink","full_url":"rtmp://sink:1935/live/testout","enabled":true}' \
  http://manager:8081/api/platforms | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

show(){ xc -b /j/c http://manager:8081/api/state > /tmp/ss.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/ss.json")); b=d["broadcast"]; rs=d["restreams"]
live=[r for r in rs if r.get("source")=="live" and r["state"]=="running"]
stby=[r for r in rs if r.get("source")=="standby" and r["state"]=="running"]
print(f"  [{sys.argv[1]:24s}] phase={b['phase']:8s} armed={str(b['armed']):5s} left={str(b['seconds_left']):>4}s live_feeds={len(live)} standby_feeds={len(stby)}")
PY
rm -f /tmp/ss.json; }
sinkbytes(){ xc http://sink:1985/api/v1/streams/ > /tmp/k.json
python3 -c "
import json
ss=json.load(open('/tmp/k.json')).get('streams',[])
print('   sink:',[(s['name'],s['recv_bytes']) for s in ss] or 'NONE')"
rm -f /tmp/k.json; }

echo "### 1. OBS live -> platform gets live feed"
docker run -d --rm --network host --name obsD linuxserver/ffmpeg:latest \
  -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 \
  -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60 \
  -c:a aac -ar 44100 -ac 2 -b:a 128k -f flv "rtmps://sonnystream.duckdns.org:1936/live/$KEY" >/tmp/obsD.log 2>&1
sleep 16; show "OBS live"; sinkbytes

echo
echo "### 2. HARD KILL OBS (SIGKILL, no graceful close)"
docker exec obsD sh -c 'kill -9 1' >/dev/null 2>&1 || docker kill --signal=KILL obsD >/dev/null 2>&1
sleep 10; show "OBS killed"; sinkbytes

echo
echo "### 3. keep watching - standby must switch the feed, then expire at 120s"
sleep 20; show "+30s (standby)"; sinkbytes

echo
echo "### cleanup"
xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" http://manager:8081/api/platforms/$PID >/dev/null
docker rm -f sink sscli obsD >/dev/null 2>&1
echo "  done"
