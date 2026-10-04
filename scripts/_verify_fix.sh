#!/bin/bash
C="curlimages/curl:latest"
PW=$(grep -oP '(?<=^ADMIN_PASSWORD=).*' /opt/serversstream/.env)
KEY=$(grep -oP '(?<=^STREAM_KEY=).*' /opt/serversstream/.env)

docker rm -f sscli >/dev/null 2>&1
docker run -d --rm --name sscli --network serversstream -v ssjar:/j \
  alpine:3.20 sh -c 'apk add --no-cache curl >/dev/null 2>&1; sleep 1200' >/dev/null
sleep 10
xc(){ docker exec sscli curl -s "$@"; }
xc -c /j/c -X POST -H "Content-Type: application/json" \
   -d "{\"username\":\"admin\",\"password\":\"$PW\"}" http://manager:8081/api/auth/login >/dev/null
TOK=$(xc -b /j/c http://manager:8081/api/auth/me | python3 -c "import json,sys;print(json.load(sys.stdin).get('csrf_token',''))")
[ -z "$TOK" ] && { echo "AUTH FAILED"; exit 1; }
echo "auth ok"

show(){ xc -b /j/c http://manager:8081/api/state > /tmp/ss_st.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/ss_st.json")); b=d["broadcast"]
n=len([r for r in d["restreams"] if r["state"]=="running"])
print(f"  [{sys.argv[1]:22s}] phase={b['phase']:8s} armed={str(b['armed']):5s} feed={b['feed']:8s} left={str(b['seconds_left']):>4}s running_restreams={n} srs={d['srs']['streams']}")
PY
rm -f /tmp/ss_st.json; }

pub(){ docker run -d --rm --network host --name $1 linuxserver/ffmpeg:latest \
   -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 \
   -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60 \
   -c:a aac -ar 44100 -ac 2 -b:a 128k -f flv \
   "rtmps://sonnystream.duckdns.org:1936/live/$KEY" > /tmp/$1.log 2>&1; }

echo; echo "### A  fresh boot, OBS never connected  (THE REPORTED BUG)"
show "no OBS"

echo; echo "### B  wait 20s - does standby creep in by itself?"
sleep 20; show "after 20s"

echo; echo "### C  OBS connects"
pub obsA; sleep 12; show "OBS live"

echo; echo "### D  OBS drops -> standby (armed)"
docker rm -f obsA >/dev/null 2>&1; sleep 8; show "OBS dropped"

echo; echo "### E  operator close -> must stay closed"
xc -b /j/c -X POST -H "X-SS-Token: $TOK" http://manager:8081/api/broadcast/close >/dev/null
sleep 6; show "operator closed"

echo; echo "### F  30s later - still closed? (not standby again)"
sleep 30; show "30s after close"

echo; echo "### G  OBS returns unattended -> auto-resume"
pub obsB; sleep 14; show "OBS back"

docker rm -f obsB sscli >/dev/null 2>&1
echo; echo "### done"
