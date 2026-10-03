"""
ServerStream manager.

Responsibilities
----------------
* Platform registry (SQLite) - add / edit / enable / disable restream targets.
* Restream supervisor - watches SRS for live publishers and runs one FFmpeg
  process per enabled platform, pushing the stream onward.
* SRS ingest auth hook - rejects publishers that do not present the stream key.
* Serves the dashboard UI and proxies the SRS stats API.

FFmpeg is used (instead of SRS `push`) because Facebook Live only accepts
RTMPS, which the SRS `push` directive cannot emit.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SRS_API       = os.getenv("SRS_API", "http://srs:1985")
SRS_RTMP      = os.getenv("SRS_RTMP", "rtmp://srs:1935")
INGEST_APP    = os.getenv("INGEST_APP", "live")
INGEST_KEY    = os.getenv("INGEST_KEY", "livestream")
PUBLIC_HOST   = os.getenv("PUBLIC_HOST", "localhost")
PUBLIC_RTMP_PORT   = os.getenv("PUBLIC_RTMP_PORT", "1935")
PUBLIC_RTMPS_PORT  = os.getenv("PUBLIC_RTMPS_PORT", "1936")
DB_PATH       = os.getenv("DB_PATH", "/data/serversstream.db")
LOG_DIR       = os.getenv("LOG_DIR", "/data/logs")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
FFMPEG_BIN    = os.getenv("FFMPEG_BIN", "ffmpeg")
STATIC_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("serversstream")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS platforms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    server_url  TEXT    NOT NULL DEFAULT '',
    stream_key  TEXT    NOT NULL DEFAULT '',
    full_url    TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
"""

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_platforms() -> list[dict[str, Any]]:
    with _db_lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM platforms ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_platform(pid: int) -> Optional[dict[str, Any]]:
    with _db_lock, _connect() as conn:
        row = conn.execute("SELECT * FROM platforms WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def create_platform(name: str, server_url: str, stream_key: str, full_url: str,
                    enabled: bool = True) -> int:
    ts = now_iso()
    with _db_lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO platforms (name, server_url, stream_key, full_url, enabled,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (name, server_url, stream_key, full_url, int(enabled), ts, ts))
        return int(cur.lastrowid)


def update_platform(pid: int, **fields: Any) -> bool:
    allowed = {"name", "server_url", "stream_key", "full_url", "enabled"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            vals.append(int(v) if k == "enabled" else v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(now_iso())
    vals.append(pid)
    with _db_lock, _connect() as conn:
        cur = conn.execute(f"UPDATE platforms SET {', '.join(sets)} WHERE id = ?", vals)
        return cur.rowcount > 0


def delete_platform(pid: int) -> bool:
    with _db_lock, _connect() as conn:
        cur = conn.execute("DELETE FROM platforms WHERE id = ?", (pid,))
        return cur.rowcount > 0


def resolve_target(p: dict[str, Any]) -> str:
    """Full push URL for a platform."""
    if p.get("full_url"):
        return p["full_url"].strip()
    server = (p.get("server_url") or "").strip().rstrip("/")
    key = (p.get("stream_key") or "").strip()
    return f"{server}/{key}" if server and key else ""


# --------------------------------------------------------------------------
# Restream supervisor
# --------------------------------------------------------------------------
class Restreamer:
    """One FFmpeg process pushing one SRS stream to one platform."""

    def __init__(self, platform: dict[str, Any], stream: str) -> None:
        self.platform_id = int(platform["id"])
        self.platform_name = platform["name"]
        self.stream = stream
        self.target = resolve_target(platform)
        self.started_at = time.time()
        self.error: Optional[str] = None
        self.restarts = 0
        self._log_path = os.path.join(
            LOG_DIR, f"platform-{self.platform_id}-{stream}.log")
        self._fh = open(self._log_path, "ab", buffering=0)
        self.proc = subprocess.Popen(self._cmd(), stdout=self._fh,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def _cmd(self) -> list[str]:
        src = f"{SRS_RTMP}/{INGEST_APP}/{self.stream}"
        return [FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-i", src, "-c", "copy", "-f", "flv", self.target]

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def uptime(self) -> int:
        return int(time.time() - self.started_at)

    def tail(self, lines: int = 12) -> list[str]:
        try:
            with open(self._log_path, "r", errors="replace") as fh:
                return [ln.rstrip("\n") for ln in deque(fh, maxlen=lines)]
        except OSError:
            return []

    def stop(self) -> None:
        if self.alive:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._fh.close()
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        rc = self.proc.poll()
        return {
            "platform_id": self.platform_id,
            "platform": self.platform_name,
            "stream": self.stream,
            "state": "running" if rc is None else "failed",
            "uptime": self.uptime,
            "exit_code": rc,
            "restarts": self.restarts,
            "target_host": self.target.split("/")[2] if "//" in self.target else self.target,
            "log": self.tail(),
        }


class Supervisor:
    """Polls SRS and keeps the right set of FFmpeg processes alive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[tuple[int, str], Restreamer] = {}
        self._retry_after: dict[tuple[int, str], float] = {}
        self._failures: dict[tuple[int, str], int] = {}
        self.srs_online = False
        self.last_poll: Optional[float] = None
        self._shutdown = threading.Event()

    # ---- SRS ----
    def active_streams(self) -> list[str]:
        try:
            r = httpx.get(f"{SRS_API}/api/v1/streams/", timeout=4)
            r.raise_for_status()
            data = r.json()
            self.srs_online = True
            self.last_poll = time.time()
            return [s["name"] for s in data.get("streams", []) if s.get("name")]
        except Exception as exc:
            self.srs_online = False
            self.last_poll = time.time()
            log.debug("SRS poll failed: %s", exc)
            return []

    def clients(self) -> dict[str, int]:
        try:
            r = httpx.get(f"{SRS_API}/api/v1/clients/", timeout=4)
            r.raise_for_status()
            cl = r.json().get("clients", [])
            return {"publishers": sum(1 for c in cl if c.get("publish")),
                    "players": sum(1 for c in cl if c.get("publish") is None)}
        except Exception:
            return {"publishers": 0, "players": 0}

    # ---- lifecycle ----
    def _start(self, platform: dict[str, Any], stream: str) -> None:
        key = (int(platform["id"]), stream)
        target = resolve_target(platform)
        if not target:
            log.warning("platform %s has no push URL; skipping", platform["name"])
            return
        try:
            r = Restreamer(platform, stream)
            with self._lock:
                self._procs[key] = r
            log.info("restream started: %s -> %s (stream=%s)",
                     platform["name"], r.target, stream)
        except Exception as exc:
            log.error("failed to start restreamer for %s: %s", platform["name"], exc)

    def _stop(self, key: tuple[int, str], reason: str) -> None:
        with self._lock:
            r = self._procs.pop(key, None)
        if r:
            log.info("restream stopped: %s (%s)", r.platform_name, reason)
            r.stop()

    def poll_once(self) -> None:
        streams = self.active_streams()
        platforms = [p for p in list_platforms() if p["enabled"]]
        wanted = {(int(p["id"]), s) for p in platforms for s in streams}

        with self._lock:
            existing = dict(self._procs)

        now = time.time()

        # ---- tear down: stream ended, platform disabled/deleted, or ffmpeg died
        for key, r in existing.items():
            if key not in wanted:
                self._stop(key, "stream ended or platform disabled")
                self._failures.pop(key, None)
                self._retry_after.pop(key, None)
                continue
            if not r.alive:
                rc = r.proc.poll()
                # a long healthy run resets the failure counter
                if r.uptime > 60:
                    self._failures[key] = 0
                fails = self._failures.get(key, 0) + 1
                self._failures[key] = fails
                backoff = min(60, 2 ** min(fails, 6))
                self._retry_after[key] = now + backoff
                self._stop(key, f"ffmpeg exited rc={rc} (retry in {backoff}s)")

        # ---- bring up anything that should be running
        for p in platforms:
            for s in streams:
                key = (int(p["id"]), s)
                with self._lock:
                    if self._procs.get(key) is not None:
                        continue
                if now < self._retry_after.get(key, 0):
                    continue
                self._start(p, s)

    def run(self) -> None:
        log.info("supervisor started (poll=%ss)", POLL_INTERVAL)
        while not self._shutdown.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("supervisor poll error")
            self._shutdown.wait(POLL_INTERVAL)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.snapshot() for r in self._procs.values()]

    def stop_all(self) -> None:
        self._shutdown.set()
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for r in procs:
            r.stop()


init_db()
supervisor = Supervisor()
threading.Thread(target=supervisor.run, name="supervisor", daemon=True).start()

# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="ServerStream", version="1.0.0")


class PlatformIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    server_url: str = ""
    stream_key: str = ""
    full_url: str = ""
    enabled: bool = True


class PlatformPatch(BaseModel):
    name: Optional[str] = None
    server_url: Optional[str] = None
    stream_key: Optional[str] = None
    full_url: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    platforms = list_platforms()
    restreams = supervisor.snapshot()
    by_platform: dict[int, list[dict[str, Any]]] = {}
    for r in restreams:
        by_platform.setdefault(r["platform_id"], []).append(r)

    out = []
    for p in platforms:
        d = dict(p)
        d["target"] = resolve_target(p)
        d["target_display"] = (d["target"][:38] + "...") if len(d["target"]) > 41 else d["target"]
        d["configured"] = bool(d["target"])
        d["restreams"] = by_platform.get(int(p["id"]), [])
        d["active"] = any(r["state"] == "running" for r in d["restreams"])
        out.append(d)

    streams = supervisor.active_streams()
    return {
        "host": PUBLIC_HOST,
        "ingest": {
            "rtmp":  f"rtmp://{PUBLIC_HOST}:{PUBLIC_RTMP_PORT}/{INGEST_APP}",
            "rtmps": f"rtmps://{PUBLIC_HOST}:{PUBLIC_RTMPS_PORT}/{INGEST_APP}",
            "key": INGEST_KEY,
            "app": INGEST_APP,
            "hls": f"https://{PUBLIC_HOST}/live/__STREAM__.m3u8",
            "flv": f"https://{PUBLIC_HOST}/live/__STREAM__.flv",
        },
        "srs": {
            "online": supervisor.srs_online,
            "last_poll": supervisor.last_poll,
            "streams": streams,
            "clients": supervisor.clients(),
        },
        "platforms": out,
        "restreams": restreams,
    }


@app.get("/api/platforms")
def api_platforms() -> list[dict[str, Any]]:
    return list_platforms()


@app.post("/api/platforms", status_code=201)
def api_create(payload: PlatformIn) -> dict[str, Any]:
    if not (payload.full_url or (payload.server_url and payload.stream_key)):
        raise HTTPException(400, "provide either full_url or server_url + stream_key")
    pid = create_platform(payload.name.strip(), payload.server_url.strip(),
                          payload.stream_key.strip(), payload.full_url.strip(),
                          payload.enabled)
    return {"id": pid, **get_platform(pid)}


@app.patch("/api/platforms/{pid}")
def api_update(pid: int, payload: PlatformPatch) -> dict[str, Any]:
    if not get_platform(pid):
        raise HTTPException(404, "platform not found")
    update_platform(pid, **payload.model_dump(exclude_unset=True))
    return get_platform(pid)


@app.delete("/api/platforms/{pid}")
def api_delete(pid: int) -> dict[str, bool]:
    if not delete_platform(pid):
        raise HTTPException(404, "platform not found")
    return {"deleted": True}


@app.post("/api/hooks/on_publish")
async def on_publish(request: Request) -> PlainTextResponse:
    """SRS ingest authorisation. Body '0' allows, anything else rejects."""
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("1")
    app_name = body.get("app", "")
    stream = body.get("stream", "")
    if app_name != INGEST_APP or stream != INGEST_KEY:
        log.warning("ingest REJECTED app=%r stream=%r ip=%s",
                    app_name, stream, body.get("ip"))
        return PlainTextResponse("1")
    log.info("ingest ALLOWED app=%r stream=%r ip=%s", app_name, stream, body.get("ip"))
    return PlainTextResponse("0")


@app.post("/api/hooks/on_unpublish")
async def on_unpublish(request: Request) -> PlainTextResponse:
    """SRS notifies us when a publisher disconnects."""
    try:
        body = await request.json()
        log.info("ingest ended app=%r stream=%r ip=%s",
                 body.get("app"), body.get("stream"), body.get("ip"))
    except Exception:
        pass
    return PlainTextResponse("0")


@app.post("/api/hooks/on_unpublish")
async def on_unpublish(request: Request) -> PlainTextResponse:
    """SRS notifies us when a publisher disconnects."""
    try:
        body = await request.json()
        log.info("ingest ended app=%r stream=%r ip=%s",
                 body.get("app"), body.get("stream"), body.get("ip"))
    except Exception:
        pass
    return PlainTextResponse("0")


@app.get("/api/srs/streams")
def api_srs_streams() -> dict[str, Any]:
    try:
        r = httpx.get(f"{SRS_API}/api/v1/streams/", timeout=4)
        return r.json()
    except Exception as exc:
        raise HTTPException(503, f"SRS unavailable: {exc}")


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "srs_online": supervisor.srs_online,
            "restreams": len(supervisor.snapshot())}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
