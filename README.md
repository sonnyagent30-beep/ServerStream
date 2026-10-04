> **Status: work in progress.**
> Shipped and verified: RTMP/RTMPS ingest, multi-platform restreaming via ffmpeg
> (including RTMPS for Facebook), HLS preview, stream-key ingest auth, web UI.
>
> **Not yet implemented** — described below because they are the next commits,
> but do not expect them in a fresh clone yet:
> - **Dashboard login** — currently there is **no authentication**. Treat any
>   deployment on a public IP as open, and see *Security* below.
> - **Standby image + drop handling** — `STANDBY_SECONDS` is not read yet.
> - **Stream-key masking in API responses** — `/api/state` currently returns
>   platform stream keys in plain text.
>
> If you deploy this today, put it behind your own reverse-proxy auth or a VPN.

# ServerStream

**Stream once from OBS. Go live on YouTube, Facebook, Twitch and TikTok at the
same time — and keep the feed alive when OBS hiccups.**

Self-hosted, free, MIT. No per-platform fees, no vendor lock-in, nothing to
upload your video to.

```
                     ┌───────────────┐
  OBS ──RTMP(S)────► │ edge (nginx)  │  TLS termination for RTMPS
                     └───────┬───────┘
                             ▼
                     ┌───────────────┐        ┌──────────────┐
                     │      SRS      │◄──────►│   manager    │  registry + UI
                     │ RTMP + HLS    │        │  (FastAPI)   │  ingest auth
                     └───────┬───────┘        └──────┬───────┘
                             │ local copy            │ supervises ffmpeg
                             ▼                       ▼
                     HLS / FLV preview     YouTube · Facebook · Twitch · …

  standby image ──ffmpeg loop──► same vhost ──┘   (covers OBS drops)
```

---

## Why this exists

Two problems hit everyone who tries to stream one OBS feed to several places:

**1. Facebook needs RTMPS, and the obvious tool can't do it.**
The usual suggestion is to use SRS (or nginx-rtmp) and add a `push` directive
per platform. That works for YouTube, Twitch and TikTok — and **silently fails
for Facebook**, which only accepts `rtmps://`. SRS `push` cannot emit TLS. Most
people lose a day to this. ServerStream uses one `ffmpeg -c copy` process per
platform instead, which handles `rtmp://` and `rtmps://` equally well. No
transcoding, so CPU cost stays near zero.

**2. When OBS drops, every platform's feed dies.**
A restreamer pulling `rtmp://your-server/live/key` has nothing to read the
moment OBS disconnects, so it exits — and Facebook and YouTube show nothing
(or their own "technical difficulties" slate). ServerStream keeps a **standby
image** publishing at all times. When the live source disappears, viewers see
your standby image *instantly* instead of a frozen frame. After a configurable
grace period (default **120 s**), ServerStream closes the platform feeds
cleanly rather than leaving a dead broadcast running.

---

## Quick start

```bash
git clone https://github.com/sonnyagent30-beep/ServerStream.git
cd ServerStream
sudo ./scripts/deploy.sh SS_HOSTNAME=your.domain
```

The script is idempotent — re-run it any time to update. It will:

1. generate a random stream key and an admin password (prompted, not echoed)
2. open ports 80/443 (web) and 1935/1936 (RTMP/RTMPS ingest)
3. issue a TLS certificate via Let's Encrypt
4. install the nginx vhost
5. build and start the containers

Then open `https://your.domain`, add your platforms, and point OBS at the
ingest URL shown on the dashboard.

**Requirements:** Ubuntu 22.04/24.04, Docker + Compose v2, a domain pointing at
the host, and outbound bandwidth ≥ the sum of your target bitrates. A 1 vCPU /
1 GB box is enough — nothing is transcoded.

---

## Configure

Everything is done from the web UI; you should not need to edit config files.

| Step | What |
|------|------|
| 1 | Log in at `https://your.domain` |
| 2 | Upload a **standby image** (1920×1080 PNG/JPG recommended) |
| 3 | **Add a platform** — pick a preset, paste that platform's stream key |
| 4 | In OBS: Settings → Stream → Service `Custom…`, paste the ingest URL + key |

Repeat step 3 per platform.

### Platform presets

| Platform | Server URL | Protocol |
|----------|-----------|----------|
| YouTube | `rtmp://a.rtmp.youtube.com/live2` | RTMP |
| Facebook | `rtmps://live-api-s.facebook.com:443/rtmp` | **RTMPS** |
| Twitch | `rtmp://live.twitch.tv/app` | RTMP |
| TikTok | `rtmp://push.tiktoklive.com/live` | RTMP |
| AWS IVS | `rtmps://ingest.global-contribute.live-video.net:443/app` | RTMPS |
| Restream.io | `rtmp://live.restream.io/live` | RTMP |

Any RTMP/RTMPS endpoint works. Use the advanced **full push URL** field for
hosts that need extra query parameters.

**Facebook note:** Live Producer issues a new stream key per broadcast, unless
you enable a *persistent* stream key. You will need to update the key in the
dashboard each time you start a new broadcast.

---

## OBS settings that matter

| Setting | Value | Why |
|---------|-------|-----|
| Keyframe interval | **2 s** | Platforms reject or badly buffer anything less frequent |
| Bitrate | 4500–6000 Kbps (1080p60) | Keep total ≤ your upload |
| Encoder | x264 `veryfast`, or NVENC | `ultrafast` hurts quality badly |
| Rate control | CBR | |

Full guide: [`scripts/obs-guide.md`](scripts/obs-guide.md).

---

## Standby / drop handling

| Setting | Meaning |
|---------|---------|
| `STANDBY_SECONDS=120` | Grace period before closing platform feeds |
| `STANDBY_SECONDS=0` | Disable standby; platform feeds die the instant OBS does |

This is **not** a delay buffer. Nothing is buffered, so latency is unchanged —
the standby image simply covers the gap. That is the difference from a rolling
buffer, which would add its full length as latency and break anything
interactive (praise, altar call, "can you see me?").

If OBS returns within the grace period, streams resume on the real feed
automatically. If it does not, the broadcast is closed deliberately — start a
new one from the dashboard.

---

## Security

Implemented today:

- **Ingest is stream-key authenticated.** SRS asks the manager before accepting
  a publisher; anything without the right key is rejected. Verified working.
- **RTMPS ingest** on port 1936 for an encrypted OBS → server leg.
- `.env` is gitignored; only `.env.example` is committed.

**Known gaps, fixed in upcoming commits:**

- No dashboard authentication yet.
- `/api/state` returns platform stream keys unmasked.

Until those land, run behind your own auth (nginx basic auth, a VPN, or a
firewall rule) if the host is reachable from the internet.

If you need to share a view-only dashboard, put basic auth on the `location /`
block in `nginx/serversstream.conf` and leave `/live/` open so playback keeps
working.

---

## API

All endpoints require a session cookie unless noted.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | Liveness (no auth) |
| `POST` | `/api/auth/login` | Log in |
| `POST` | `/api/auth/logout` | Log out |
| `GET` | `/api/state` | Everything the dashboard needs |
| `GET` | `/api/platforms` | List targets |
| `POST` | `/api/platforms` | Add a target |
| `PATCH` | `/api/platforms/{id}` | Update / enable / disable |
| `DELETE` | `/api/platforms/{id}` | Remove |
| `GET` | `/api/standby` | Current standby image |
| `POST` | `/api/standby` | Upload a standby image |
| `GET` | `/api/srs/streams` | Raw SRS stream list |

---

## Operations

```bash
cd /opt/serversstream
docker compose ps                  # srs + manager + edge, all healthy
docker compose logs -f manager
docker compose restart
docker compose up -d --build       # after a git pull
```

Per-platform FFmpeg logs appear in the dashboard under each platform row.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| OBS cannot connect | Ports 1935/1936 blocked — check `ufw status` |
| Platform feed absent, dashboard idle | Wrong/expired stream key — open that platform's row → logs |
| Facebook rejects the stream | Needs RTMPS **and** a fresh key per broadcast |
| Platform drops every ~30 s | Bitrate above your upload, or keyframe interval ≠ 2 s |
| Standby image never appears | No standby image uploaded yet |
| Nothing after `deploy.sh` | `docker compose ps` and `docker compose logs manager` |
| HLS preview 404 | Stream name is your *stream key*, not `livestream` |

## License

MIT
