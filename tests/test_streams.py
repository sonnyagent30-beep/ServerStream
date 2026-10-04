
import os, sys, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "manager"))
tmp = tempfile.mkdtemp()
os.environ.update({
    "ADMIN_USER":"admin","ADMIN_PASSWORD":"pw","STANDBY_SECONDS":"6",
    "DB_PATH":os.path.join(tmp,"t.db"),"LOG_DIR":os.path.join(tmp,"logs"),
    "POLL_INTERVAL":"1","SRS_API":"http://127.0.0.1:1",
    "STANDBY_IMAGE":os.path.join(tmp,"standby.jpg"),
})
import app as A
import httpx
sup = A.supervisor
A.INGEST_KEY = "KEY"; A.STANDBY_STREAM = "standby"
fails=[]
def check(n,c,e=""):
    print(("  PASS  " if c else "  FAIL  ")+n+(("  -> "+str(e)) if e and not c else ""))
    if not c: fails.append(n)

# fake SRS: streams lists a stream that has NO attached publisher (stale entry)
class FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p

class FakeSRS:
    def __init__(self, streams, clients): self.s=streams; self.c=clients
    def get(self, url, timeout=None):
        if "/streams/" in url: return FakeResp({"streams":[{"name":n} for n in self.s]})
        if "/clients/" in url: return FakeResp({"clients":self.c})
        raise AssertionError(url)

print("\n== stale SRS stream entry must not count as live ==")
# 'KEY' listed by SRS but no publisher client -> stale
sup.__init__()
fake = FakeSRS(["KEY","standby"], [{"name":"standby","publish":True,"ip":"1.1.1.1"}])
orig = httpx.get
httpx.get = lambda url, timeout=None: fake.get(url)
try:
    got = sup.active_streams()
finally:
    httpx.get = orig
check("dead publisher stream excluded", "KEY" not in got, got)
check("live standby still included", "standby" in got, got)

print("\n== a real publisher is reported ==")
sup.__init__()
fake = FakeSRS(["KEY","standby"],
               [{"name":"standby","publish":True,"ip":"1.1.1.1"},
                {"name":"KEY","publish":True,"ip":"2.2.2.2"}])
httpx.get = lambda url, timeout=None: fake.get(url)
try:
    got = sup.active_streams()
finally:
    httpx.get = orig
check("both live streams reported", set(got)=={"KEY","standby"}, got)

print("\n== /clients unavailable -> trust the stream list (do not kill a good show) ==")
sup.__init__()
class OnlyStreams:
    def get(self, url, timeout=None):
        if "/streams/" in url: return FakeResp({"streams":[{"name":"KEY"},{"name":"standby"}]})
        raise RuntimeError("clients endpoint down")
httpx.get = lambda url, timeout=None: OnlyStreams().get(url)
try:
    got = sup.active_streams()
finally:
    httpx.get = orig
check("falls back to stream list", set(got)=={"KEY","standby"}, got)

print("\n"+("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
