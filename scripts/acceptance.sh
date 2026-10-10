#!/usr/bin/env bash
# ServerStream acceptance test — fully isolated.
# Spins up its OWN manager + SRS + sink on dedicated ports / containers
# / volumes, so it NEVER touches a production ServerStream deployment.
set -euo pipefail

PROJ="ss-acc-$$"
DIR="/tmp/$PROJ"
mkdir -p "$DIR"

ADMIN_USER="admin"
ADMIN_PASSWORD="acceptance-test-pw"
STREAM_KEY="test-key-$$"

# Randomize host-mapped ports to avoid collisions with a live install.
SRS_API_PORT=$((19000 + RANDOM % 500))
SRS_RTMP_PORT=$((19010 + RANDOM % 500))
MGR_PORT=$((19020 + RANDOM % 500))
SINK_API_PORT=$((19040 + RANDOM % 500))
SINK_RTMP_PORT=$((19050 + RANDOM % 500))

MANAGER_BUILD_DIR="${SERVERSTREAM_MANAGER_DIR:-/opt/serversstream/manager}"
export PROJ DIR ADMIN_USER ADMIN_PASSWORD STREAM_KEY        SRS_API_PORT SRS_RTMP_PORT MGR_PORT        SINK_API_PORT SINK_RTMP_PORT MANAGER_BUILD_DIR

# SRS config (origin ingest + hooks). The manager pushes TO SRS (internal port
# 1935) via the Docker network; OBS publishes FROM the host (external port).
cat > "$DIR/srs.conf" <<'SRS'
listen 1935;
max_connections 1000;
daemon off;
srs_log_tank console;
http_api { enabled on; listen 1985; crossdomain on; }
http_server { enabled on; listen 8080; dir ./objs/nginx/html; crossdomain on; }
vhost __defaultVhost__ {
    tcp_nodelay on;
    play { gop_cache off; queue_length 10; mw_latency 100; }
    publish { mr off; }
    http_hooks {
        enabled on;
        on_publish http://manager:8081/api/hooks/on_publish;
        on_unpublish http://manager:8081/api/hooks/on_unpublish;
    }
}
SRS

# --- isolated compose: manager + SRS + sink + cli ---
# Key: INTERNAL traffic uses container name + port 1935 (not host-mapped port).
# Only the manager API and OBS publish use host-mapped ports.
cat > "$DIR/compose.yml" <<COMPOSE
version: "3.8"
services:
  srs:
    image: ossrs/srs:5
    container_name: \${PROJ}-srs
    ports:
      - "127.0.0.1:\${SRS_API_PORT}:1985"
      - "127.0.0.1:\${SRS_RTMP_PORT}:1935"
    networks: [ss]
    volumes:
      - ./srs.conf:/usr/local/srs/conf/srs.conf:ro
      - srs-logs:/usr/local/srs/objs/logs
    command: ["./objs/srs", "-c", "conf/srs.conf"]

  manager:
    build: \${MANAGER_BUILD_DIR}
    container_name: \${PROJ}-manager
    depends_on: [srs]
    environment:
      - SRS_API=http://srs:1985
      - SRS_RTMP=rtmp://srs:1935
      - INGEST_APP=live
      - INGEST_KEY=\${STREAM_KEY}
      - PUBLIC_HOST=localhost
      - PUBLIC_RTMP_PORT=\${SRS_RTMP_PORT}
      - PUBLIC_RTMPS_PORT=0
      - ADMIN_USER=\${ADMIN_USER}
      - ADMIN_PASSWORD=\${ADMIN_PASSWORD}
      - STANDBY_SECONDS=5
      - POLL_INTERVAL=1
    volumes:
      - mgr-data:/data
    ports:
      - "127.0.0.1:\${MGR_PORT}:8081"
    networks: [ss]

  sink:
    image: ossrs/srs:5
    container_name: \${PROJ}-sink
    ports:
      - "127.0.0.1:\${SINK_API_PORT}:1985"
      - "127.0.0.1:\${SINK_RTMP_PORT}:1935"
    networks: [ss]

  cli:
    image: curlimages/curl:8.9.1
    container_name: \${PROJ}-cli
    volumes:
      - cli-jar:/j
    command: ["sleep", "1200"]
    networks: [ss]

volumes:
  srs-logs:
  mgr-data:
  cli-jar:

networks:
  ss:
    name: \${PROJ}-net
COMPOSE

# ---- cleanup on exit ---
cleanup(){
  echo
  echo "### cleaning up $PROJ"
  docker rm -f "$PROJ-sink" "$PROJ-cli" "$PROJ-srs" "$PROJ-manager" obsF$$ obsG$$ >/dev/null 2>&1 || true
  docker network rm "$PROJ-net" >/dev/null 2>&1 || true
  rm -rf "$DIR"
}
trap cleanup EXIT

# ---- launch ---
echo "### launching isolated stack: $PROJ"
echo "### manager API: $MGR_PORT | SRS ingest: $SRS_RTMP_PORT | sink: $SINK_RTMP_PORT"
docker compose -p "$PROJ" -f "$DIR/compose.yml" up -d --build 2>&1 | tail -5
echo "### waiting for manager to start..."
sleep 16

MGR="http://127.0.0.1:$MGR_PORT"
pass=0; fail=0

xc(){ docker exec "$PROJ-cli" curl -s "$@"; }
t(){ if [ "$2" = "$3" ]; then echo "  ✓ PASS  $1"; pass=$((pass+1)); else echo "  ✗ FAIL  $1 (got '"'"'$2'"'"' want '"'"'$3'"'"')"; fail=$((fail+1)); fi; }

show(){ xc -b /j/c "$MGR/api/state" > /tmp/s.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/s.json")); b=d["broadcast"]; rs=d["restreams"]
live=len([r for r in rs if r.get("source")=="live" and r["state"]=="running"])
stby=len([r for r in rs if r.get("source")=="standby" and r["state"]=="running"])
print(f"  [{sys.argv[1]:28s}] phase={b['phase']:8s} armed={str(b['armed']):5s} left={str(b['seconds_left']):>4}s live={live} standby={stby}")
PY
rm -f /tmp/s.json; }

sinkb(){ xc "http://sink:$SINK_API_PORT/api/v1/streams/" > /tmp/k.json 2>/dev/null
python3 -c "
import json
try:
  ss=json.load(open('/tmp/k.json')).get('streams',[])
  print('    sink:',[(s['name'],s.get('recv_bytes',0)) for s in ss] or 'NONE  <-- platform receiving NOTHING')
except Exception: print('    sink: (no data yet)')
" 2>/dev/null || echo "    sink: (error)"
rm -f /tmp/k.json; }

q(){ xc -b /j/c "$MGR/api/state" > /tmp/q.json
python3 - "$1" <<'PY'
import json,sys
d=json.load(open("/tmp/q.json"))
print(len([r for r in d["restreams"] if r.get("source")==sys.argv[1] and r["state"]=="running"]))
PY
rm -f /tmp/q.json; }

field(){ xc -b /j/c "$MGR/api/state" > /tmp/f.json
python3 - "$1" <<'PY'
import json,sys
print(json.load(open("/tmp/f.json"))["broadcast"][sys.argv[1]])
PY
rm -f /tmp/f.json; }

purge_test_platforms(){
  local state ids
  state=$(xc -b /j/c "$MGR/api/state" 2>/dev/null || echo "")
  ids=$(echo "$state" | grep -o '"id":[0-9]*.*"name":"ZZ[^"]*"' | grep -o '"id":[0-9]*' | sed 's/"id"://' || true)
  for id in $ids; do
    xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" "$MGR/api/platforms/$id" >/dev/null 2>&1 || true
  done
}

# --- auth ---
rm -f /j/c
xc -c /j/c -X POST -H "Content-Type: application/json"    -d '{"username":"'$ADMIN_USER'","password":"'$ADMIN_PASSWORD'"}' "$MGR/api/auth/login" >/dev/null
TOK=$(xc -b /j/c "$MGR/api/auth/me" | grep -o '"csrf_token":"[^"]*"' | sed 's/"csrf_token":"//;s/"$//')
purge_test_platforms

# Platform URL uses INTERNAL container name + port 1935 (restreamer runs
# inside the manager container on the same Docker network).
PID=$(xc -b /j/c -X POST -H "Content-Type: application/json" -H "X-SS-Token: $TOK"   -d '{"name":"ZZ Sink","full_url":"rtmp://sink:1935/live/out","enabled":true}'   "$MGR/api/platforms" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "ERR")
if [ "$PID" = "ERR" ] || [ -z "$PID" ]; then
  echo "FATAL: could not create test platform"
  exit 1
fi
echo "### test platform created (id=$PID)"
echo

echo "TEST 1  no OBS ever -> nothing sent to platforms"
show "t+0"; sleep 8; show "t+8"; sleep 12; show "t+20"; sinkb
t "phase is closed"      "$(field phase)"      "closed"
t "armed is false"       "$(field armed)"      "False"
t "0 standby feeds"      "$(q standby)"        "0"

echo
echo "TEST 2  OBS connects -> live feed reaches the platform"
# OBS publishes to the origin SRS on the host-mapped RTMP port.
docker run -d --rm --network host --name obsF$$ linuxserver/ffmpeg:latest   -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30   -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60   -c:a aac -ar 44100 -ac 2 -b:a 128k -t 180 -f flv "rtmp://127.0.0.1:$SRS_RTMP_PORT/live/$STREAM_KEY" >/dev/null 2>&1
sleep 15; show "OBS live"; sinkb
t "phase is live"        "$(field phase)"      "live"
if xc "http://sink:$SINK_API_PORT/api/v1/streams/" 2>/dev/null | grep -q "recv_bytes"; then
  t "platform is receiving live data" "yes" "yes"
else
  t "platform is receiving live data" "no" "yes"
fi

echo
echo "TEST 3  OBS hard-killed -> standby takes over"
docker kill --signal=KILL obsF$$ >/dev/null 2>&1; docker rm -f obsF$$ >/dev/null 2>&1
sleep 12; show "OBS killed"; sinkb
t "phase is standby"     "$(field phase)"      "standby"

echo
echo "TEST 4  operator close -> stays closed, never drifts back"
xc -b /j/c -X POST -H "X-SS-Token: $TOK" "$MGR/api/broadcast/close" >/dev/null
sleep 8; show "closed"; sleep 15; show "closed +15s"; sinkb
t "phase is closed"      "$(field phase)"      "closed"
t "0 live feeds"         "$(q live)"           "0"

echo
echo "TEST 5  OBS returns unattended -> auto-resume"
docker run -d --rm --network host --name obsG$$ linuxserver/ffmpeg:latest   -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30   -f lavfi -i sine=frequency=440 -c:v libx264 -preset veryfast -b:v 1500k -g 60   -c:a aac -ar 44100 -ac 2 -b:a 128k -t 180 -f flv "rtmp://127.0.0.1:$SRS_RTMP_PORT/live/$STREAM_KEY" >/dev/null 2>&1
sleep 15; show "OBS back"; sinkb
t "phase is live"        "$(field phase)"      "live"

echo
echo "TEST 6  dashboard writes sent as text/plain are still parsed (regression)"
NEW=$(xc -b /j/c -X POST    -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK"    -d '{"name":"ZZ content-type probe","full_url":"rtmp://sink:1935/live/probe","enabled":false}'    "$MGR/api/platforms" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")' 2>/dev/null || echo "")
if [ -n "$NEW" ]; then
  t "create with text/plain body" "created" "created"
  R=$(xc -b /j/c -X PATCH -H "Content-Type: text/plain;charset=UTF-8" -H "X-SS-Token: $TOK"       -d '{"enabled":true}' "$MGR/api/platforms/$NEW")
  EN=$(echo "$R" | grep -o '"enabled":[a-z]*' | head -1)
  t "the exact call from the bug report works" "$EN" '"enabled":true'
else
  t "create with text/plain body" "missing" "created"
fi

# cleanup
xc -b /j/c -X DELETE -H "X-SS-Token: $TOK" "$MGR/api/platforms/$PID" >/dev/null 2>&1 || true
purge_test_platforms
docker rm -f obsG$$ >/dev/null 2>&1 || true

echo
echo "================ $pass passed, $fail failed ================"
[ "$fail" -eq 0 ]
