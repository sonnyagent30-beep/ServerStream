
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
sup = A.supervisor
A.INGEST_KEY = "KEY"; A.STANDBY_STREAM = "standby"
fails=[]
def check(n,c,e=""):
    print(("  PASS  " if c else "  FAIL  ")+n+(("  -> "+str(e)) if e and not c else ""))
    if not c: fails.append(n)

print("\n== THE BUG: standby must NOT start when OBS was never connected ==")
sup.__init__()
ph, src = sup._resolve_phase(["standby"], time.time())
check("fresh boot, standby publishing, no OBS -> CLOSED",
      (ph,src)==("closed",None), (ph,src))
ph, src = sup._resolve_phase([], time.time())
check("fresh boot, nothing at all -> CLOSED", (ph,src)==("closed",None), (ph,src))
sup._armed = False
ph, src = sup._resolve_phase(["standby"], time.time()+500)
check("still CLOSED 500s later (never picks standby on its own)",
      (ph,src)==("closed",None), (ph,src))

print("\n== standby DOES arm, but only after a real live stream ==")
sup.__init__()
now = time.time()
ph, src = sup._resolve_phase(["KEY","standby"], now)
check("OBS live -> live/KEY", (ph,src)==("live","KEY"), (ph,src))
check("armed after live", sup._armed is True)
ph, src = sup._resolve_phase(["standby"], time.time())
check("OBS drops -> standby", (ph,src)==("standby","standby"), (ph,src))

print("\n== grace period, then close, then stay closed ==")
ph, src = sup._resolve_phase(["standby"], time.time()+7)
check("after 7s > 6s -> CLOSED", (ph,src)==("closed",None), (ph,src))
check("disarmed after close", sup._armed is False)
ph, src = sup._resolve_phase(["standby"], time.time()+20)
check("still CLOSED 20s later (does not re-arm by itself)",
      (ph,src)==("closed",None), (ph,src))
ph, src = sup._resolve_phase(["standby"], time.time()+300)
check("still CLOSED 5min later", (ph,src)==("closed",None), (ph,src))

print("\n== auto-resume still works ==")
ph, src = sup._resolve_phase(["KEY","standby"], time.time()+600)
check("OBS returns unattended -> live again", (ph,src)==("live","KEY"), (ph,src))

print("\n== operator close disarms too ==")
sup.__init__()
sup._resolve_phase(["KEY","standby"], time.time())
sup.force_close()
check("force_close -> CLOSED", sup.phase=="closed", sup.phase)
check("force_close disarms", sup._armed is False)
ph, src = sup._resolve_phase(["standby"], time.time())
check("after operator close, standby does not resume",
      (ph,src)==("closed",None), (ph,src))

print("\n== STANDBY_SECONDS=0 ==")
A.STANDBY_SECONDS = 0
sup.__init__()
sup._resolve_phase(["KEY","standby"], time.time())
ph, src = sup._resolve_phase(["standby"], time.time())
check("0s grace -> immediate CLOSED on drop", (ph,src)==("closed",None), (ph,src))
A.STANDBY_SECONDS = 6

print("\n== no standby image, after a real drop ==")
sup.__init__()
A.STANDBY_IMAGE = os.path.join(tmp,"nope.jpg"); A.STATIC_STANDBY = os.path.join(tmp,"no.jpg")
A.standby.stop()
sup._resolve_phase(["KEY"], time.time())
ph, src = sup._resolve_phase([], time.time())
check("no standby image -> CLOSED (never feed a dead source)",
      (ph,src)==("closed",None), (ph,src))

print("\n== snapshot exposes armed ==")
sup.__init__()
sup._resolve_phase(["KEY","standby"], time.time())
snap = sup.broadcast_snapshot()
check("snapshot has 'armed'", "armed" in snap, snap)
check("armed is True while live", snap.get("armed") is True, snap)

print("\n"+("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
