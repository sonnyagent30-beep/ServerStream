
import os, sys, json, tempfile
sys.path.insert(0, r"C:\Users\Dannion/ServerStream/manager")

tmp = tempfile.mkdtemp()
os.environ.update({
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "s3cret-test-pw",
    "STANDBY_SECONDS": "5",
    "DB_PATH": os.path.join(tmp, "t.db"),
    "LOG_DIR": os.path.join(tmp, "logs"),
    "POLL_INTERVAL": "1",
    "SRS_API": "http://127.0.0.1:1",      # unreachable: SRS offline is a valid state
    "STANDBY_IMAGE": os.path.join(tmp, "standby.jpg"),
    "SESSION_COOKIE": "ss_session",
})

from fastapi.testclient import TestClient
import app as A

fails = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond: fails.append(name)

c = TestClient(A.app)

print("\n== auth gate ==")
r = c.get("/api/state")
check("GET /api/state is 401 when logged out", r.status_code == 401, r.status_code)

r = c.get("/", follow_redirects=False)
check("GET / redirects to /login when logged out", r.status_code == 302 and "/login" in r.headers.get("location",""), (r.status_code, r.headers.get("location")))

r = c.post("/api/auth/login", json={"username":"admin","password":"wrong"})
check("bad password rejected 401", r.status_code == 401, r.status_code)

r = c.post("/api/auth/login", json={"username":"admin","password":"s3cret-test-pw"})
check("good password logs in 200", r.status_code == 200, r.text[:200])
check("session cookie set", "ss_session" in r.cookies or any(k=="ss_session" for k in r.cookies.keys()), list(r.cookies.keys()))

print("\n== csrf ==")
# TestClient keeps cookies; grab the token value
tok = c.cookies.get("ss_token")
check("csrf token cookie present", bool(tok), list(c.cookies.keys()))

r = c.post("/api/platforms", json={"name":"X","server_url":"rtmp://a/b","stream_key":"k"})
check("POST without X-SS-Token is 403", r.status_code == 403, r.status_code)

r = c.post("/api/platforms", json={"name":"X","server_url":"rtmp://host/live","stream_key":"abcd1234efgh"},
           headers={"X-SS-Token": tok})
check("POST with token succeeds 201", r.status_code == 201, r.text[:300])

print("\n== key masking ==")
r = c.get("/api/state")
check("state reachable when logged in", r.status_code == 200, r.status_code)
body = r.json()
plats = body.get("platforms", [])
check("platform returned", len(plats) == 1, plats)
if plats:
    p = plats[0]
    check("stream_key is empty in response", p.get("stream_key") == "", repr(p.get("stream_key")))
    check("raw secret absent from whole payload", "abcd1234efgh" not in json.dumps(body), "LEAK")
    check("masked hint present", bool(p.get("stream_key_masked")), p.get("stream_key_masked"))
    check("target is masked", "abcd1234efgh" not in (p.get("target") or ""), p.get("target"))

print("\n== standby api ==")
r = c.get("/api/standby")
check("GET /api/standby 200", r.status_code == 200, r.status_code)
r = c.post("/api/standby", json={"data_url":"data:garbage"}, headers={"X-SS-Token": tok})
check("rejects non-image payload", r.status_code == 400, (r.status_code, r.text[:120]))

import base64
png = base64.b64decode(
 "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
r = c.post("/api/standby", json={"data_url":"data:image/png;base64,"+base64.b64encode(png).decode()},
           headers={"X-SS-Token": tok})
check("accepts a real PNG", r.status_code == 201, (r.status_code, r.text[:200]))

r = c.get("/api/standby")
check("standby reports configured", r.json().get("configured") is True, r.text[:200])

print("\n== broadcast close ==")
r = c.post("/api/broadcast/close", headers={"X-SS-Token": tok})
check("close endpoint 200", r.status_code == 200, r.text[:200])

print("\n== health stays open ==")
r = c.get("/api/health")
check("health needs no auth", r.status_code == 200, r.status_code)


print("\n== ingest hook: standby must be allowed, others rejected ==")
import asyncio
# (a) the standby stream is ours -> allowed
r = c.post("/api/hooks/on_publish",
           json={"app":"live","stream":"standby","ip":"172.18.0.3"})
check("standby publish allowed", r.status_code==200 and r.text.strip()=="0", (r.status_code, r.text))
# (b) the real key -> allowed
r = c.post("/api/hooks/on_publish",
           json={"app":"live","stream":A.INGEST_KEY,"ip":"1.2.3.4"})
check("correct ingest key allowed", r.text.strip()=="0", r.text)
# (c) a wrong key -> rejected
r = c.post("/api/hooks/on_publish",
           json={"app":"live","stream":"wrongkey","ip":"1.2.3.4"})
check("wrong ingest key rejected", r.text.strip()!="0", r.text)
# (d) wrong app -> rejected
r = c.post("/api/hooks/on_publish",
           json={"app":"other","stream":A.INGEST_KEY,"ip":"1.2.3.4"})
check("wrong app rejected", r.text.strip()!="0", r.text)

print("\n== ingest hook needs no session ==")
c2 = TestClient(A.app)
r = c2.post("/api/hooks/on_publish", json={"app":"live","stream":"nope","ip":"1.1.1.1"})
check("hook reachable without login (SRS is internal)", r.status_code==200, r.status_code)
check("hook rejects unknown key without login", r.text.strip()!="0", r.text)

print("\n== logout ==")
r = c.post("/api/auth/logout", headers={"X-SS-Token": tok})
check("logout 200", r.status_code == 200, r.status_code)
c.cookies.clear()
r = c.get("/api/state")
check("state blocked again after logout", r.status_code == 401, r.status_code)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
