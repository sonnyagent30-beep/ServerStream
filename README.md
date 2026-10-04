# ServerStream

**Stream once from OBS. Go live on YouTube, Facebook, Twitch and TikTok at the
same time — and keep the feed alive when OBS hiccups.**

Self-hosted, free, MIT. No per-platform fees, no vendor lock-in, nothing to
upload your video to.

```
  OBS ──RTMP(S)────► edge (nginx :1935 / :1936)   TLS termination for RTMPS
                            │
                            ▼
                     ┌───────────────┐      ┌──────────────┐
                     │      SRS      │◄────►│   manager    │  auth + registry
                     │  RTMP hub     │      │  (FastAPI)   │  live/standby/closed
                     └───────┬───────┘      └──────┬───────┘
                             │                     │ supervises one ffmpeg per platform
                             ▼                     ▼
                       standby image ──►  YouTube · Facebook · Twitch · …
                       (ffmpeg loop)      (each gets the live feed, or the
                                            standby frame when OBS drops)
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

---

## Security

- **Single-admin login is mandatory.** Stream keys are credentials. A dashboard
  on a public IP without auth hands every platform to whoever asks.
- **Stream keys never leave the server.** Every API response is masked
  (`mask_secret` / `mask_url`), and query-parameter secrets are redacted even
  if a platform URL is unusual. This holds regardless of auth state.
- **CSRF protection** — session cookie is `SameSite=Strict` and mutations
  require an `X-SS-Token` header.
- **Ingest is stream-key authenticated.** SRS calls the manager's `on_publish`
  hook and rejects anything that is not your key. (The internal standby
  publisher is the one exception, explicitly allow-listed.)
- **RTMPS ingest** on 1936 for an encrypted OBS → server leg.

Credentials live in `.env` (gitignored, `chmod 600`). To rotate the admin
password, edit `ADMIN_PASSWORD` and `docker compose up -d manager`.

## Standby / drop handling

The failure mode this exists for: a restreamer pulling `rtmp://your-server/live/key`
has nothing to read the instant OBS disconnects, so it exits — and every
platform goes dark.

ServerStream keeps a **standby image** publishing as a second RTMP source on
the same vhost. The supervisor then moves the platform restreamers between
sources:

| Phase | Trigger | What platforms receive |
|-------|---------|------------------------|
| `live` | OBS publishing | the real OBS feed |
| `standby` | OBS gone | the standby image, with a countdown |
| `closed` | `STANDBY_SECONDS` elapsed, or operator | nothing — the broadcast ends cleanly |

**Recovery is automatic.** When OBS publishes again — at any later point, with
nobody at the laptop — the supervisor notices on its next poll and puts the
restreamers back on the live feed. That is deliberate: unattended church streams
must recover by themselves.

Because nothing is buffered, latency is unchanged. The standby frame covers the
gap; it does not delay anything. That is the difference from a rolling buffer,
which would add its full length as latency.

| Setting | Default | Meaning |
|---------|---------|---------|
| `STANDBY_SECONDS` | `120` | Grace period before closing platform feeds |
| `STANDBY_SECONDS=0` | — | Disable standby; feeds die the instant OBS does |

If no standby image is available, ServerStream closes rather than feeding a
dead source, so viewers never see a "live" label with no video behind it.

## API

All endpoints require a session cookie unless noted.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | Liveness (no auth) |
| `POST` | `/api/auth/login` | Log in |
| `POST` | `/api/auth/logout` | Log out |
| `POST` | `/api/auth/login` | Log in (sets `ss_session` + `ss_token`) |
| `POST` | `/api/auth/logout` | Log out |
| `GET` | `/api/auth/me` | Current session + CSRF token |
| `GET` | `/api/state` | Everything the dashboard needs |
| `GET` | `/api/platforms` | List targets (keys masked) |
| `POST` | `/api/platforms` | Add a target |
| `PATCH` | `/api/platforms/{id}` | Update / enable / disable |
| `DELETE` | `/api/platforms/{id}` | Remove |
| `GET` | `/api/standby` | Standby image status |
| `GET` | `/api/standby/image` | The standby frame (public) |
| `POST` | `/api/standby` | Upload a standby image (data-URL) |
| `DELETE` | `/api/standby` | Restore the bundled default |
| `POST` | `/api/broadcast/close` | Operator override — end all feeds now |
| `GET` | `/api/srs/streams` | Raw SRS stream list |

Unauthenticated: `GET /api/health`, `GET /login`, `GET /static/*`,
`GET /api/standby/image`, and the SRS `on_publish`/`on_unpublish` hooks
(internal, called by SRS itself).

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

## License

MIT

## Tests

```bash
python -m pip install "fastapi" "uvicorn[standard]" "httpx"
python tests/test_app.py      # auth gate, CSRF, key masking, standby API, ingest hook
python tests/test_phase.py    # live → standby → closed → auto-resume
```

No Docker or network needed; all exit non-zero on failure.

For an end-to-end check against a real endpoint (spawns a throwaway SRS as a
fake platform plus a test-pattern RTMPS publisher, then cleans up):

```bash
scp scripts/acceptance.sh root@your.host:/tmp/acc.sh
ssh root@your.host 'bash /tmp/acc.sh'
```

It asserts the full cycle: idle sends nothing → OBS live → standby covers a
hard kill → operator close sticks → unattended auto-resume.

`tests/test_content_type.py` and `tests/test_ui_headers.mjs` guard a real
production bug: `fetch()` sends a string body as `text/plain` unless told
otherwise, so the server received every dashboard write as a raw string and
rejected it with *"Input should be a valid dictionary or object"*. The client
now declares its JSON body and the server tolerates mislabelled JSON.

## Contributing

Issues and PRs welcome. Two things the project cares about:

1. **Never commit a secret.** `.env` is gitignored and only `.env.example` ships.
2. **Keep it portable.** No hostnames, IPs, or provider assumptions baked into
   shipped files — `deploy.sh` takes `SS_HOSTNAME` and templates `__DOMAIN__`.
