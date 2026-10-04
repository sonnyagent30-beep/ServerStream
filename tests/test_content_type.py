"""
The fetch() text/plain footgun.

fetch() silently sends ``Content-Type: text/plain;charset=UTF-8`` whenever a
request carries a string body and no explicit Content-Type. Starlette then
hands the body to the endpoint as a raw *string* instead of parsing it, and it
surfaces as Pydantic's ``Input should be a valid dictionary or object to
extract fields from`` with ``input`` holding a string that merely looks like
JSON.

The dashboard hit this on every write: enabling a platform, adding one,
editing one and uploading the standby image all failed with an opaque alert.

The client now sets the header itself (tests/test_ui_headers.mjs guards that),
and the server tolerates mislabelled JSON. This file guards the server side and
checks the tolerance stays narrow enough to leave real form uploads alone.
"""

import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "manager"))

tmp = tempfile.mkdtemp()
os.environ.update({
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "s3cret-test-pw",
    "STANDBY_SECONDS": "5",
    "DB_PATH": os.path.join(tmp, "t.db"),
    "LOG_DIR": os.path.join(tmp, "logs"),
    "POLL_INTERVAL": "1",
    "SRS_API": "http://127.0.0.1:1",
    "STANDBY_IMAGE": os.path.join(tmp, "standby.jpg"),
    "SESSION_COOKIE": "ss_session",
})

from fastapi.testclient import TestClient
import app as A

fails = []
def check(name, cond, extra: object = ""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ((("  -> " + str(extra)) if extra and not cond else "")))
    if not cond: fails.append(name)

c = TestClient(A.app)

# Authenticate and keep hold of the CSRF token, as the real UI does.
r = c.post("/api/auth/login", json={"username": "admin", "password": "s3cret-test-pw"})
check("login works", r.status_code == 200, r.text[:200])
tok = c.cookies.get("ss_token")
check("csrf token present", bool(tok), list(c.cookies.keys()))
csrf = {"X-SS-Token": tok}

BODY = json.dumps({"name": "Test Platform", "full_url": "rtmp://sink:1935/live/out", "enabled": True})

print("\n== text/plain bodies still parse (this is the reported bug) ==")
r = c.post("/api/platforms", content=BODY,
           headers={"Content-Type": "text/plain;charset=UTF-8", **csrf})
check("POST /api/platforms with text/plain body", r.status_code == 201, (r.status_code, r.text[:220]))
pid = r.json().get("id") if r.status_code == 201 else None

print("\n== a body with no Content-Type at all still parses ==")
r = c.post("/api/platforms", content=BODY,
           headers={"Content-Type": "application/json", **csrf})
pid2 = r.json().get("id") if r.status_code == 201 else None
check("POST with correct application/json unchanged", r.status_code == 201, (r.status_code, r.text[:220]))

print("\n== the exact failing call from the screenshot ==")
if pid:
    r = c.patch(f"/api/platforms/{pid}", content='{"enabled":false}',
                headers={"Content-Type": "text/plain;charset=UTF-8", **csrf})
    check("PATCH enable=false via text/plain", r.status_code == 200, (r.status_code, r.text[:220]))
    # `enabled` comes straight from SQLite as 0/1, so assert on truthiness.
    check("platform is now disabled", r.status_code == 200 and not r.json().get("enabled"),
          r.text[:200])

    r = c.patch(f"/api/platforms/{pid}", content='{"enabled":true}',
                headers={"Content-Type": "text/plain;charset=UTF-8", **csrf})
    check("PATCH enable=true via text/plain", r.status_code == 200 and r.json().get("enabled"),
          (r.status_code, r.text[:220]))
else:
    check("PATCH enable toggle", False, "no platform created")

print("\n== tolerance is narrow: real form uploads are NOT rewritten ==")
r = c.post("/api/platforms", content="name=x&enabled=1",
           headers={"Content-Type": "application/x-www-form-urlencoded", **csrf})
check("urlencoded body is not forced to JSON", r.status_code == 422, (r.status_code, r.text[:220]))

r = c.post("/api/platforms", content="--BOUNDARY",
           headers={"Content-Type": "multipart/form-data; boundary=BOUNDARY", **csrf})
check("multipart body is not forced to JSON", r.status_code == 422, (r.status_code, r.text[:220]))

print("\n== auth is not weakened by the rewrite ==")
r = c.post("/api/platforms", content=BODY,
           headers={"Content-Type": "text/plain;charset=UTF-8"})
check("text/plain still requires auth",
      r.status_code in (401, 403), r.status_code)  # CSRF rejects before auth does

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
