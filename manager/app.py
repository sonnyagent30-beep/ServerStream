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

import functools
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                            RedirectResponse, Response)
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


def mask_secret(value: Optional[str], keep_start: int = 4, keep_end: int = 2) -> str:
    """Render a credential for display without disclosing it.

    Shows only enough characters to let an operator tell two keys apart.
    """
    if not value:
        return ""
    v = value.strip()
    if len(v) <= keep_start + keep_end + 3:
        return "*" * len(v)
    return f"{v[:keep_start]}{'*' * 6}{v[-keep_end:]}"


def mask_url(url: Optional[str], secret: str = "") -> str:
    """Redact a secret wherever it appears inside a URL."""
    if not url:
        return ""
    out = url
    if secret:
        out = out.replace(secret.strip(), mask_secret(secret))
    # belt and braces: redact common RTMP query params too
    for param in ("stream_key", "streamkey", "key", "token", "auth", "sig"):
        out = re.sub(rf"([?&]{param}=)[^&]+", rf"\1***", out, flags=re.IGNORECASE)
    return out

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

    def __init__(self, platform: dict[str, Any], source_stream: str) -> None:
        self.platform_id = int(platform["id"])
        self.platform_name = platform["name"]
        self.source_stream = source_stream
        self.target = resolve_target(platform)
        self.started_at = time.time()
        self.error: Optional[str] = None
        self.restarts = 0
        self._log_path = os.path.join(
            LOG_DIR, f"platform-{self.platform_id}-{source_stream}.log")
        self._fh = open(self._log_path, "ab", buffering=0)
        self.proc = subprocess.Popen(self._cmd(), stdout=self._fh,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def _cmd(self) -> list[str]:
        # source is either the live OBS stream or the standby stream, chosen
        # by the supervisor. Both are published on the same vhost.
        src = f"{SRS_RTMP}/{INGEST_APP}/{self.source_stream}"
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
            "stream": self.source_stream,
            "source": "standby" if self.source_stream == STANDBY_STREAM else "live",
            "state": "running" if rc is None else "failed",
            "uptime": self.uptime,
            "exit_code": rc,
            "restarts": self.restarts,
            "target_host": self.target.split("/")[2] if "//" in self.target else self.target,
            "log": self.tail(),
        }


class Supervisor:
    """Decides what each platform should be fed, and keeps ffmpeg alive.

    State machine per broadcast cycle:

        live         OBS is publishing -> restreamers pull the OBS stream
        standby      OBS gone, grace period running -> restreamers pull the
                     standby image so viewers see a holding screen
        closed       grace period expired -> restreamers are stopped, the
                     platform broadcast ends deliberately

    Recovery is automatic: when OBS publishes again at any later point, the
    supervisor notices on its next poll and brings the restreamers back up on
    the live stream. Nobody has to be at the laptop.
    """

    PHASE_LIVE = "live"
    PHASE_STANDBY = "standby"
    PHASE_CLOSED = "closed"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[tuple[int, str], Restreamer] = {}
        self._retry_after: dict[tuple[int, str], float] = {}
        self._failures: dict[tuple[int, str], int] = {}
        self.srs_online = False
        self.last_poll: Optional[float] = None
        self.phase = self.PHASE_CLOSED
        self.live_stream: Optional[str] = None
        self.source_since = 0.0          # when the current phase began
        self.last_live_seen = 0.0        # last time OBS was actually publishing
        self.cycles = 0                  # completed live->closed transitions
        self._armed = False              # standby protection only after a real drop
        self._shutdown = threading.Event()

    # ---- SRS ----
    def active_streams(self) -> list[str]:
        """Streams that currently have a *live publisher* attached.

        SRS's /streams list can keep a stream entry alive for a while after its
        publisher dies without a clean disconnect (killed container, pulled
        network cable, black-holed socket). Acting on that stale entry would
        leave the supervisor believing OBS is still live, so cross-check
        /clients and only report streams with a publisher whose connection is
        still alive.
        """
        try:
            streams = httpx.get(f"{SRS_API}/api/v1/streams/", timeout=4)
            streams.raise_for_status()
            named = {s["name"] for s in streams.json().get("streams", []) if s.get("name")}
        except Exception as exc:
            self.srs_online = False
            self.last_poll = time.time()
            log.debug("SRS stream poll failed: %s", exc)
            return []

        try:
            clients = httpx.get(f"{SRS_API}/api/v1/clients/", timeout=4)
            clients.raise_for_status()
            pubs = [c for c in clients.json().get("clients", [])
                    if c.get("publish") and c.get("name") in named]
            live = {c["name"] for c in pubs}
            # A stream entry with no attached publisher is stale; drop it.
            stale = named - live
            if stale:
                log.warning("SRS lists stream(s) with no live publisher, "
                            "treating as gone: %s", sorted(stale))
        except Exception as exc:
            # If /clients is unavailable we cannot verify; fall back to the
            # stream list rather than wrongly dropping a healthy broadcast.
            log.debug("SRS client poll failed, trusting stream list: %s", exc)
            live = named

        self.srs_online = True
        self.last_poll = time.time()
        return [s for s in named if s in live]

    def clients(self) -> dict[str, int]:
        try:
            r = httpx.get(f"{SRS_API}/api/v1/clients/", timeout=4)
            r.raise_for_status()
            cl = r.json().get("clients", [])
            return {"publishers": sum(1 for c in cl if c.get("publish")),
                    "players": sum(1 for c in cl if c.get("publish") is None)}
        except Exception:
            return {"publishers": 0, "players": 0}

    # ---- what should be running right now ----
    def _resolve_phase(self, streams: list[str], now: float) -> tuple[str, Optional[str]]:
        """Return (phase, source_stream_to_feed) from observed SRS state.

        Idle vs standby is the important distinction here:

          * **closed** - the broadcast is over. Nothing is being sent anywhere.
            This is the state ServerStream sits in when it has never seen OBS,
            or after the grace period expires.
          * **standby** - OBS was publishing, then stopped. We deliberately keep
            the platform feeds alive on the standby frame for STANDBY_SECONDS so
            viewers do not see a frozen picture. This is a *reaction to a drop*,
            not a default state.
          * **live** - OBS is publishing.

        Without an explicit armed flag, standby would also mean "never had OBS",
        which is exactly the bug where the standby image gets pushed to every
        platform on its own.
        """
        live = [s for s in streams if s == INGEST_KEY]
        standby_up = STANDBY_STREAM in streams
        was_armed = self._armed

        if live:
            # OBS is up: arm standby protection and prefer the real feed.
            if self.phase != self.PHASE_LIVE:
                log.info("phase -> LIVE (OBS publishing)")
                if not was_armed:
                    self.cycles += 1
            self._armed = True
            self.phase = self.PHASE_LIVE
            self.live_stream = INGEST_KEY
            self.last_live_seen = now
            self.source_since = now
            return self.PHASE_LIVE, INGEST_KEY

        # No OBS. Only fall back to standby if we were actually live before,
        # i.e. this is a genuine drop rather than a quiet server.
        if was_armed:
            # Even after a real drop, standby is only usable if there is a frame
            # to send. Otherwise close rather than feed a dead source, which
            # would look live to viewers while carrying no video.
            if standby_image_path() is None and not standby.running:
                log.warning("live source gone and no standby image - closing feeds")
                self.phase = self.PHASE_CLOSED
                self._armed = False
                return self.PHASE_CLOSED, None
            if self.phase == self.PHASE_LIVE:
                log.warning("OBS disconnected - holding on standby for %ss "
                            "(viewers see the standby frame)", STANDBY_SECONDS)
                self.source_since = now
            self.phase = self.PHASE_STANDBY
            elapsed = now - self.source_since
            if STANDBY_SECONDS <= 0:
                log.info("standby disabled -> closing platform feeds")
                self.phase = self.PHASE_CLOSED
                self._armed = False
                return self.PHASE_CLOSED, None
            if elapsed >= STANDBY_SECONDS:
                log.info("standby window of %ss elapsed - closing platform feeds "
                         "(will auto-resume if OBS returns)", STANDBY_SECONDS)
                self.phase = self.PHASE_CLOSED
                self._armed = False
                return self.PHASE_CLOSED, None
            return self.PHASE_STANDBY, (STANDBY_STREAM if standby_up else None)

        # Never seen OBS (or already closed) and OBS is still absent.
        # Stay closed. Do not publish anything to the platforms.
        if self.phase != self.PHASE_CLOSED:
            log.info("phase -> CLOSED (no live source, standby not armed)")
            self.phase = self.PHASE_CLOSED
        return self.PHASE_CLOSED, None

    # ---- process lifecycle ----
    def _start(self, platform: dict[str, Any], source_stream: str) -> None:
        key = (int(platform["id"]), source_stream)
        target = resolve_target(platform)
        if not target:
            log.warning("platform %s has no push URL; skipping", platform["name"])
            return
        try:
            r = Restreamer(platform, source_stream)
            with self._lock:
                self._procs[key] = r
            log.info("restream started: %s <- %s -> %s",
                     platform["name"], source_stream, r.target)
        except Exception as exc:
            log.error("failed to start restreamer for %s: %s", platform["name"], exc)

    def _stop(self, key: tuple[int, str], reason: str) -> None:
        with self._lock:
            r = self._procs.pop(key, None)
        if r:
            log.info("restream stopped: %s (%s)", r.platform_name, reason)
            r.stop()

    def _stop_all(self, reason: str) -> None:
        with self._lock:
            keys = list(self._procs.keys())
        for k in keys:
            self._stop(k, reason)
        with self._lock:
            self._failures.clear()
            self._retry_after.clear()

    def poll_once(self) -> None:
        now = time.time()
        streams = self.active_streams()
        phase, source = self._resolve_phase(streams, now)

        with self._lock:
            existing = dict(self._procs)

        # 1. anything running against the wrong source, or stale, gets torn down
        for key, r in existing.items():
            stale_source = key[1] != source
            if phase == self.PHASE_CLOSED or stale_source:
                self._stop(key, "closed" if phase == self.PHASE_CLOSED
                           else f"source switched to {source}")
                self._failures.pop(key, None)
                self._retry_after.pop(key, None)
            elif not r.alive:
                rc = r.proc.poll()
                if r.uptime > 60:
                    self._failures[key] = 0
                fails = self._failures.get(key, 0) + 1
                self._failures[key] = fails
                backoff = min(60, 2 ** min(fails, 6))
                self._retry_after[key] = now + backoff
                self._stop(key, f"ffmpeg exited rc={rc} (retry in {backoff}s)")

        # 2. bring up whatever should be running
        if source is None:
            return
        if not standby.running and source == STANDBY_STREAM:
            standby.start()          # self-heal if the standby publisher died
        platforms = [p for p in list_platforms() if p["enabled"]]
        for p in platforms:
            key = (int(p["id"]), source)
            with self._lock:
                if self._procs.get(key) is not None:
                    continue
            if now < self._retry_after.get(key, 0):
                continue
            self._start(p, source)

    def run(self) -> None:
        log.info("supervisor started (poll=%ss standby=%ss)",
                 POLL_INTERVAL, STANDBY_SECONDS)
        while not self._shutdown.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("supervisor poll error")
            self._shutdown.wait(POLL_INTERVAL)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.snapshot() for r in self._procs.values()]

    def broadcast_snapshot(self) -> dict[str, Any]:
        now = time.time()
        if self.phase == self.PHASE_LIVE:
            detail = {"feed": "live", "seconds_left": None}
        elif self.phase == self.PHASE_STANDBY:
            left = max(0, int(STANDBY_SECONDS - (now - self.source_since)))
            detail = {"feed": "standby", "seconds_left": left}
        else:
            detail = {"feed": "closed", "seconds_left": 0}
        return {
            "phase": self.phase,
            "armed": self._armed,
            "standby_seconds": STANDBY_SECONDS,
            "since": self.source_since,
            "cycles": self.cycles,
            "standby_available": standby_image_path() is not None,
            "standby_running": standby.running,
            **detail,
        }

    def force_close(self) -> None:
        """Manual 'close the broadcast now' from the dashboard.

        Disarms standby as well, so the platforms stay closed until OBS
        connects again rather than quietly dropping back to standby.
        """
        self._stop_all("closed by operator")
        self.phase = self.PHASE_CLOSED
        self._armed = False

    def stop_all(self) -> None:
        self._shutdown.set()
        standby.stop()
        self._stop_all("shutdown")


init_db()
supervisor = Supervisor()
# NOTE: the supervisor thread is started at the *end* of this module, once every
# constant it reads (STANDBY_*, etc.) is defined. Starting it here raised
# NameError on the first poll.

# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="ServerStream", version="1.0.0")



# --------------------------------------------------------------------------
# Standby
# --------------------------------------------------------------------------
# A standby image is published as a permanent RTMP source on the same SRS
# vhost. When the live source is absent, restreamers are repointed at it, so
# platforms keep receiving *something* instead of a frozen frame or nothing.
#
# The standby encoder must match the live stream's codec parameters (H.264
# high, same resolution/fps, AAC stereo 44.1k) or ``-c copy`` cannot switch
# between them.

STANDBY_STREAM = os.getenv("STANDBY_STREAM", "standby")
STANDBY_SECONDS = int(os.getenv("STANDBY_SECONDS", "120"))
STANDBY_WIDTH = int(os.getenv("STANDBY_WIDTH", "1920"))
STANDBY_HEIGHT = int(os.getenv("STANDBY_HEIGHT", "1080"))
STANDBY_FPS = int(os.getenv("STANDBY_FPS", "30"))
STANDBY_BITRATE = os.getenv("STANDBY_BITRATE", "4500k")
STANDBY_AUDIO = os.getenv("STANDBY_AUDIO", "128k")
STANDBY_IMAGE = os.getenv("STANDBY_IMAGE", "/data/standby.jpg")
STATIC_STANDBY = os.path.join(STATIC_DIR, "standby-default.jpg")

# codecs the standby generator uses; live feed is expected to match
STANDBY_VIDEO_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
    "-preset", "veryfast", "-tune", "zerolatency", "-b:v", STANDBY_BITRATE,
    "-maxrate", STANDBY_BITRATE, "-bufsize", STANDBY_BITRATE,
    "-g", str(STANDBY_FPS * 2), "-pix_fmt", "yuv420p",
    "-r", str(STANDBY_FPS), "-s", f"{STANDBY_WIDTH}x{STANDBY_HEIGHT}",
]
STANDBY_AUDIO_ARGS = [
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", STANDBY_AUDIO,
]


def standby_image_path() -> Optional[str]:
    """User upload wins; otherwise fall back to the bundled brand image."""
    if os.path.exists(STANDBY_IMAGE) and os.path.getsize(STANDBY_IMAGE) > 0:
        return STANDBY_IMAGE
    if os.path.exists(STATIC_STANDBY):
        return STATIC_STANDBY
    return None


class StandbyPublisher:
    """Keeps the standby RTMP source alive for the lifetime of the process."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self.error: Optional[str] = None
        self.image = standby_image_path()
        self._fh = None
        self._log_path = os.path.join(LOG_DIR, "standby-publisher.log")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _cmd(self) -> list[str]:
        img = self.image or ""
        # -loop 1 repeats the image forever; silent stereo audio keeps the
        # stream shape identical to a real OBS feed.
        return [FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-re",
                "-loop", "1", "-framerate", str(STANDBY_FPS), "-i", img,
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                *STANDBY_VIDEO_ARGS, *STANDBY_AUDIO_ARGS,
                "-shortest",
                "-f", "flv", f"{SRS_RTMP}/{INGEST_APP}/{STANDBY_STREAM}"]

    def start(self) -> None:
        if self.running:
            return
        img = standby_image_path()
        if not img:
            self.error = "no standby image available"
            log.warning("standby disabled: no image found")
            return
        self.image = img
        try:
            self._fh = open(self._log_path, "ab", buffering=0)
            self.proc = subprocess.Popen(self._cmd(), stdout=self._fh,
                                         stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
            self.started_at = time.time()
            self.error = None
            log.info("standby publisher started from %s", img)
        except Exception as exc:
            self.error = str(exc)
            log.error("standby publisher failed to start: %s", exc)

    def reload_image(self) -> None:
        """After an upload: restart so ffmpeg picks up the new file."""
        self.stop()
        self.start()

    def stop(self) -> None:
        if self.running and self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def tail(self, lines: int = 10) -> list[str]:
        try:
            with open(self._log_path, "r", errors="replace") as fh:
                return [ln.rstrip("\n") for ln in deque(fh, maxlen=lines)]
        except OSError:
            return []

    def snapshot(self) -> dict[str, Any]:
        rc = self.proc.poll() if self.proc is not None else None
        return {
            "running": rc is None,
            "state": "running" if rc is None else ("no-image" if not self.image else "failed"),
            "uptime": int(time.time() - self.started_at) if rc is None else 0,
            "image": os.path.basename(self.image) if self.image else None,
            "error": self.error,
            "log": self.tail(),
        }


standby = StandbyPublisher()
standby.start()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
# Single-admin model: one username/password from the environment. Passwords are
# never stored or logged in plain text - they are compared with
# ``hmac.compare_digest`` and only ever exist in process memory.
#
# CSRF: the session cookie is SameSite=Strict and every mutating endpoint also
# requires an ``X-SS-Token`` header, which the dashboard reads from a
# non-HttpOnly cookie. A cross-site form post can send the cookie but cannot
# read the token, so it cannot forge a request.

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_COOKIE = os.getenv("SESSION_COOKIE", "ss_session")
TOKEN_COOKIE = "ss_token"
SESSION_TTL = int(os.getenv("SESSION_TTL", str(12 * 3600)))

_auth_warning = False


def _token() -> str:
    return secrets.token_urlsafe(32)


_sessions: dict[str, tuple[str, float]] = {}
_sessions_lock = threading.Lock()


def _prune_sessions(now: float) -> None:
    expired = [k for k, (_, exp) in _sessions.items() if exp <= now]
    for k in expired:
        _sessions.pop(k, None)


def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    now = time.time()
    with _sessions_lock:
        _prune_sessions(now)
        entry = _sessions.get(token)
        if not entry:
            return False
        _, exp = entry
        return exp > now


def create_session(username: str) -> str:
    tok = _token()
    now = time.time()
    with _sessions_lock:
        _prune_sessions(now)
        _sessions[tok] = (username, now + SESSION_TTL)
    return tok


def drop_session(token: Optional[str]) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def check_credentials(username: str, password: str) -> bool:
    ok_user = hmac.compare_digest(username.encode(), ADMIN_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return ok_user and ok_pass


def auth_guard(request: Request) -> Optional[str]:
    """Return an error body if the request is not authenticated, else None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not session_valid(token):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    # mutating verbs need the CSRF token too
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("X-SS-Token", "")
        if not sent or not hmac.compare_digest(sent, token):
            return JSONResponse({"error": "bad or missing X-SS-Token"}, status_code=403)
    return None


def require_auth(fn):
    """Guard for protected endpoints.

    Wraps both sync and async handlers - FastAPI happily calls either, and the
    endpoints here are a mix. Awaiting a plain dict (from a sync handler) raises
    TypeError, so the result is only awaited when it is awaitable.
    """
    import inspect

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        request = kwargs.get("request")
        if request is None:
            for a in args:
                if isinstance(a, Request):
                    request = a
                    break
        if request is None:
            return JSONResponse({"error": "no request"}, status_code=400)
        denied = auth_guard(request)
        if denied is not None:
            return denied
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
    return wrapper


@app.on_event("startup")
async def _startup_check() -> None:
    global _auth_warning
    if not ADMIN_PASSWORD:
        log.error("ADMIN_PASSWORD is empty - the dashboard API is UNAUTHENTICATED.")
        _auth_warning = True


def masked_platform(p: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Platform record safe to send to a browser."""
    if not p:
        return {}
    d = dict(p)
    key = d.get("stream_key") or ""
    target = resolve_target(d)
    d["stream_key_masked"] = mask_secret(key)
    d["stream_key"] = ""
    d["full_url"] = mask_url(d.get("full_url"), key)
    d["server_url"] = mask_url(d.get("server_url"))
    d["target"] = mask_url(target, key)
    return d


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/auth/login")
async def api_login(payload: LoginIn, request: Request, response: Response) -> JSONResponse:
    if not ADMIN_PASSWORD:
        return JSONResponse({"error": "ADMIN_PASSWORD is not configured"}, status_code=503)
    if not check_credentials(payload.username, payload.password):
        log.warning("failed login attempt for user=%r ip=%s",
                    payload.username[:40], request.client.host if request.client else "?")
        return JSONResponse({"error": "invalid username or password"}, status_code=401)
    tok = create_session(payload.username)
    out = JSONResponse({"ok": True, "user": payload.username})
    # Only mark the cookies Secure when the client actually reached us over
    # TLS. Hard-coding secure=True makes the login silently fail on a plain
    # HTTP deployment (the browser refuses to send the cookie back).
    secure = (request.headers.get("x-forwarded-proto") or
              request.url.scheme or "").lower() == "https"
    # HttpOnly on the session id so XSS cannot read it; the CSRF token is a
    # separate, deliberately JS-readable cookie.
    out.set_cookie(SESSION_COOKIE, tok, httponly=True, samesite="strict",
                   secure=secure, max_age=SESSION_TTL, path="/")
    out.set_cookie(TOKEN_COOKIE, tok, httponly=False, samesite="strict",
                   secure=secure, max_age=SESSION_TTL, path="/")
    return out


@app.post("/api/auth/logout")
def api_logout(request: Request) -> JSONResponse:
    drop_session(request.cookies.get(SESSION_COOKIE))
    out = JSONResponse({"ok": True})
    out.delete_cookie(SESSION_COOKIE, path="/")
    out.delete_cookie(TOKEN_COOKIE, path="/")
    return out


@app.get("/api/auth/me")
def api_me(request: Request) -> JSONResponse:
    tok = request.cookies.get(SESSION_COOKIE)
    if not session_valid(tok):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse({"ok": True, "user": ADMIN_USER,
                         "csrf_token": tok, "auth_configured": bool(ADMIN_PASSWORD)})


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
@require_auth
def api_state(request: Request) -> dict[str, Any]:
    platforms = list_platforms()
    restreams = supervisor.snapshot()
    by_platform: dict[int, list[dict[str, Any]]] = {}
    for r in restreams:
        by_platform.setdefault(r["platform_id"], []).append(r)

    out = []
    for p in platforms:
        d = dict(p)
        target = resolve_target(p)
        # Masked everywhere: the browser never needs the real push URL, and an
        # unauthenticated/misconfigured deploy must not leak platform keys.
        d["stream_key_masked"] = mask_secret(p.get("stream_key"))
        d["stream_key"] = ""                      # never leave the server
        d["full_url"] = mask_url(p.get("full_url"), p.get("stream_key") or "")
        d["server_url"] = mask_url(p.get("server_url"))
        d["target"] = mask_url(target, p.get("stream_key") or "")
        d["target_display"] = d["target"]
        d["configured"] = bool(target)
        d["restreams"] = by_platform.get(int(p["id"]), [])
        d["active"] = any(r["state"] == "running" for r in d["restreams"])
        out.append(d)

    streams = supervisor.active_streams()
    return {
        "host": PUBLIC_HOST,
        "broadcast": supervisor.broadcast_snapshot(),
        "ingest": {
            "rtmp":  f"rtmp://{PUBLIC_HOST}:{PUBLIC_RTMP_PORT}/{INGEST_APP}",
            "rtmps": f"rtmps://{PUBLIC_HOST}:{PUBLIC_RTMPS_PORT}/{INGEST_APP}",
            "key": INGEST_KEY,          # endpoint is authenticated
            "app": INGEST_APP,
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
@require_auth
def api_platforms(request: Request) -> list[dict[str, Any]]:
    return [masked_platform(p) for p in list_platforms()]


@app.post("/api/platforms", status_code=201)
@require_auth
def api_create(payload: PlatformIn, request: Request) -> dict[str, Any]:
    if not (payload.full_url or (payload.server_url and payload.stream_key)):
        raise HTTPException(400, "provide either full_url or server_url + stream_key")
    pid = create_platform(payload.name.strip(), payload.server_url.strip(),
                          payload.stream_key.strip(), payload.full_url.strip(),
                          payload.enabled)
    saved = get_platform(pid)
    return {"id": pid, **masked_platform(saved)}


@app.patch("/api/platforms/{pid}")
@require_auth
def api_update(pid: int, payload: PlatformPatch, request: Request) -> dict[str, Any]:
    if not get_platform(pid):
        raise HTTPException(404, "platform not found")
    update_platform(pid, **payload.model_dump(exclude_unset=True))
    return masked_platform(get_platform(pid))


@app.delete("/api/platforms/{pid}")
@require_auth
def api_delete(pid: int, request: Request) -> dict[str, bool]:
    if not delete_platform(pid):
        raise HTTPException(404, "platform not found")
    return {"deleted": True}


@app.post("/api/broadcast/close")
@require_auth
def api_close_broadcast(request: Request) -> JSONResponse:
    """Operator override: end every platform feed now (standby keeps running)."""
    supervisor.force_close()
    return JSONResponse({"ok": True, "phase": supervisor.phase})

@app.post("/api/hooks/on_publish")
async def on_publish(request: Request) -> PlainTextResponse:
    """SRS ingest authorisation. Body '0' allows, anything else rejects."""
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("1")
    app_name = body.get("app", "")
    stream = body.get("stream", "")
    # The standby publisher is our own internal process, not an outside
    # publisher, so it must be allowed through even though its stream name is
    # not the operator's key. Everything else still requires the exact key.
    internal = (stream == STANDBY_STREAM and app_name == INGEST_APP)
    if not internal and (app_name != INGEST_APP or stream != INGEST_KEY):
        log.warning("ingest REJECTED app=%r stream=%r ip=%s",
                    app_name, stream, body.get("ip"))
        return PlainTextResponse("1")
    log.info("ingest ALLOWED app=%r stream=%r ip=%s%s",
             app_name, stream, body.get("ip"), " (standby)" if internal else "")
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
@require_auth
def api_srs_streams(request: Request) -> dict[str, Any]:
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
# Standby image upload
# --------------------------------------------------------------------------
ALLOWED_IMG = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_IMG_BYTES = 12 * 1024 * 1024


def _validate_image(data: bytes) -> Optional[str]:
    """Cheap magic-byte sniff so we never shell out ffmpeg on junk input."""
    if len(data) < 12:
        return "file is too small to be an image"
    if data[:3] == b"\xff\xd8\xff":
        return None                                     # jpeg
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return None                                     # png
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return None                                     # webp
    return "unsupported image format (use JPEG, PNG or WebP)"


@app.get("/api/standby")
@require_auth
def api_standby(request: Request) -> JSONResponse:
    img = standby_image_path()
    if not img:
        return JSONResponse({"configured": False})
    st = os.stat(img)
    return JSONResponse({
        "configured": True,
        "using_default": img == STATIC_STANDBY,
        "filename": os.path.basename(img),
        "size": st.st_size,
        "url": "/api/standby/image",
    })


@app.get("/api/standby/image")
def api_standby_image() -> FileResponse:
    """The standby frame itself. Public so ffmpeg/the page can load it."""
    img = standby_image_path()
    if not img:
        raise HTTPException(404, "no standby image configured")
    media = {".jpg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp"}.get(os.path.splitext(img)[1].lower(),
                                        "application/octet-stream")
    return FileResponse(img, media_type=media)


@app.post("/api/standby")
@require_auth
async def api_standby_upload(request: Request) -> JSONResponse:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        data_url = body.get("data_url") or ""
        if not data_url.startswith("data:"):
            raise HTTPException(400, "data_url must be a data: URL")
        try:
            header, b64 = data_url.split(",", 1)
            import base64
            data = base64.b64decode(b64, validate=True)
            ctype = header[5:].split(";")[0].lower()
        except Exception:
            raise HTTPException(400, "could not decode data_url")
    else:
        data = await request.body()

    if len(data) > MAX_IMG_BYTES:
        raise HTTPException(413, "image larger than 12 MB")
    err = _validate_image(data)
    if err:
        raise HTTPException(400, err)

    ext = ALLOWED_IMG.get(ctype, os.path.splitext(STANDBY_IMAGE)[1] or ".jpg")
    tmp = STANDBY_IMAGE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, STANDBY_IMAGE)          # atomic, standby keeps serving
    log.info("standby image updated (%d bytes)", len(data))

    standby.reload_image()
    if not standby.running:
        return JSONResponse({"ok": True, "warning": standby.error or
                             "standby publisher did not start - check its log"},
                            status_code=201)
    return JSONResponse({"ok": True}, status_code=201)


@app.delete("/api/standby")
@require_auth
def api_standby_reset(request: Request) -> JSONResponse:
    """Fall back to the bundled brand image."""
    if os.path.exists(STANDBY_IMAGE):
        os.remove(STANDBY_IMAGE)
    standby.reload_image()
    return JSONResponse({"ok": True, "using_default": os.path.exists(STATIC_STANDBY)})


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/", include_in_schema=False)
def index(request: Request) -> Response:
    if not session_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


# Everything is defined: safe to start the supervisor now.
threading.Thread(target=supervisor.run, name="supervisor", daemon=True).start()
