
import os, sys, time, tempfile
sys.path.insert(0, r"C:\Users\Dannion/ServerStream/manager")
tmp = tempfile.mkdtemp()
os.environ.update({
    "ADMIN_USER":"admin","ADMIN_PASSWORD":"pw","STANDBY_SECONDS":"6",
    "DB_PATH":os.path.join(tmp,"t.db"),"LOG_DIR":os.path.join(tmp,"logs"),
    "POLL_INTERVAL":"1","SRS_API":"http://127.0.0.1:1",
    "STANDBY_IMAGE":os.path.join(tmp,"standby.jpg"),
})
import app as A

sup = A.supervisor
fails=[]
def check(n,c,e=""):
    print(("  PASS  " if c else "  FAIL  ")+n+(("  -> "+str(e)) if e and not c else ""))
    if not c: fails.append(n)

# ---- the phase machine, driven directly (no SRS needed) ----
print("\n== phase transitions (STANDBY_SECONDS=6) ==")
now = time.time()
A.INGEST_KEY = "KEY"
A.STANDBY_STREAM = "standby"

sup.phase = sup.PHASE_LIVE; sup.source_since = now; sup.live_stream = "KEY"
ph, src = sup._resolve_phase(["KEY","standby"], now)
check("OBS publishing -> live, feed=KEY", (ph,src)==("live","KEY"), (ph,src))

# OBS vanishes -> standby
ph, src = sup._resolve_phase(["standby"], time.time())
check("OBS gone -> standby, feed=standby", (ph,src)==("standby","standby"), (ph,src))

# before expiry -> still standby
ph, src = sup._resolve_phase(["standby"], time.time()+3)
check("t+3s still standby", (ph,src)==("standby","standby"), (ph,src))

# after expiry -> closed
ph, src = sup._resolve_phase(["standby"], time.time()+7)
check("t+7s (>6s) -> closed, no feed", (ph,src)==("closed",None), (ph,src))

# AUTO-RESUME: OBS comes back later, unattended
sup.phase = sup.PHASE_CLOSED
ph, src = sup._resolve_phase(["KEY","standby"], time.time()+600)
check("OBS returns later -> auto-resumes to live", (ph,src)==("live","KEY"), (ph,src))

# standby disabled (0) -> close immediately
A.STANDBY_SECONDS = 0
sup.phase = sup.PHASE_LIVE; sup.source_since = time.time()
ph, src = sup._resolve_phase(["standby"], time.time())
check("STANDBY_SECONDS=0 -> closed immediately", (ph,src)==("closed",None), (ph,src))
A.STANDBY_SECONDS = 6

# standby image missing -> cannot fall back, must close rather than feed nothing
print("\n== standby image missing ==")
A.STANDBY_IMAGE = os.path.join(tmp,"nope.jpg")
A.STATIC_STANDBY = os.path.join(tmp,"also-nope.jpg")
check("standby_image_path() returns None", A.standby_image_path() is None, A.standby_image_path())
A.standby.stop()          # publisher genuinely down
sup.phase = sup.PHASE_LIVE; sup.source_since = time.time()
ph, src = sup._resolve_phase([], time.time())
check("no standby available -> closed (never feed a dead source)", ph=="closed", (ph,src))

# And when a standby image IS present, standby phase is used instead.
A.standby.image = A.STATIC_STANDBY = os.path.join(os.path.dirname(A.__file__),"static","standby-default.jpg")
sup.phase = sup.PHASE_LIVE; sup.source_since = time.time()
ph, src = sup._resolve_phase(["standby"], time.time())
check("standby image present -> standby phase used", (ph,src)==("standby","standby"), (ph,src))

print("\n== snapshot ==")
snap = sup.broadcast_snapshot()
check("snapshot has phase", "phase" in snap, snap)
check("snapshot reports standby availability flag", "standby_available" in snap, snap)

print("\n"+("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
