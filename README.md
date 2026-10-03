# ServerStream

Stream once from OBS. Go live on YouTube, Facebook, Twitch, TikTok and anything
else that speaks RTMP or RTMPS - simultaneously, from one small server.

```
                    ┌──────────────┐
   OBS ──RTMP(S)──► │  edge (nginx)│  TLS termination for RTMPS
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐        ┌─────────────┐
                    │     SRS      │◄──────►│   manager   │  registry + UI
                    │  RTMP + HLS  │        │  (FastAPI)  │  ingest auth
                    └──────┬───────┘        └──────┬──────┘
                           │  local copy           │ supervises
                           │                       ▼
                           │              ┌──────────────────┐
                           │              │ ffmpeg x N       │
                           │              │ one per platform │
                           │              └────────┬─────────┘
                           ▼                       ▼
                    HLS / FLV preview      YouTube · Facebook · Twitch · ...
```

## Why FFmpeg and not SRS `push`?

SRS can forward a stream with a `push` directive, but it only emits **plain
RTMP**. Facebook Live refuses plain RTMP and requires **RTMPS**. So the manager
runs one `ffmpeg -c copy` process per platform, which handles `rtmp://` and
`rtmps://` alike. Nothing is re-encoded, so CPU cost stays near zero.

## Quick start

```bash
git clone https://github.com/sonnyagent30-beep/ServerStream.git
cd ServerStream
sudo DOMAIN=yourname.duckdns.org ./scripts/deploy.sh
```

`deploy.sh` is idempotent - re-run it any time to update.

It will: install nothing you already have, generate a stream key, open ports
80/443/1935/1936, issue a Let's Encrypt certificate, install the nginx vhost,
and bring up the containers.

## Configure

1. Open `https://yourname.duckdns.org`
2. **Add a platform** - pick a preset, paste the stream key from that platform's
   live dashboard. Repeat per platform.
3. Point OBS at the server shown on the dashboard:

   | Field | Value |
   |-------|-------|
   | Service | `Custom...` |
   | Server | `rtmps://yourname.duckdns.org:1936/live` |
   | Stream Key | *the key shown on the dashboard* |

4. Start streaming. Each enabled platform flips to **restreaming** within a few
   seconds.

See [`scripts/obs-guide.md`](scripts/obs-guide.md) for encoder settings.

## Dashboard

Everything is driven from the web UI - no config files to hand-edit:

- add / edit / enable / disable / delete restream targets
- live status per platform, with per-platform FFmpeg logs
- ingest credentials with one-click copy
- SRS health, active streams, viewer count

## Platform presets

| Platform | Server URL | Protocol |
|----------|-----------|----------|
| YouTube | `rtmp://a.rtmp.youtube.com/live2` | RTMP |
| Facebook | `rtmps://live-api-s.facebook.com:443/rtmp` | **RTMPS** |
| Twitch | `rtmp://live.twitch.tv/app` | RTMP |
| TikTok | `rtmp://push.tiktoklive.com/live` | RTMP |
| AWS IVS | `rtmps://ingest.global-contribute.live-video.net:443/app` | RTMPS |
| Restream.io | `rtmp://live.restream.io/live` | RTMP |

Any other RTMP/RTMPS endpoint works too - use the advanced **full push URL**
field for hosts that need extra query parameters.

## Security

- `on_publish` HTTP hook: SRS asks the manager before accepting a publisher.
  Anything that does not present the configured stream key is rejected.
- Stream key is generated at deploy time and stored in `.env` (gitignored).
- RTMPS ingest on 1936 for an encrypted OBS-to-server leg.
- The dashboard is public. Put it behind basic auth or a VPN if you do not want
  the ingest key visible - see *Locking down the dashboard* below.

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/state` | Everything the dashboard needs |
| `GET` | `/api/platforms` | List targets |
| `POST` | `/api/platforms` | Add a target |
| `PATCH` | `/api/platforms/{id}` | Update / enable / disable |
| `DELETE` | `/api/platforms/{id}` | Remove |
| `GET` | `/api/srs/streams` | Raw SRS stream list |
| `GET` | `/api/health` | Liveness |

## Ports

| Port | Service |
|------|---------|
| 80 / 443 | Dashboard + HLS (nginx) |
| 1935 | RTMP ingest |
| 1936 | RTMPS ingest |

## Operations

```bash
cd /opt/serversstream
docker compose ps
docker compose logs -f manager
docker compose restart
docker compose up -d --build      # after a git pull
```

Per-platform FFmpeg logs are also visible in the dashboard, and on disk under
the `manager-data` volume (`/data/logs`).

## Locking down the dashboard

Add basic auth to the `location /` block in `nginx/serversstream.conf`:

```bash
htpasswd -c /etc/nginx/.htpasswd youruser
```

```nginx
location / {
    auth_basic "ServerStream";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8081;
    ...
}
```

Leave `/live/` unauthenticated so HLS playback keeps working.

## Requirements

- Ubuntu 22.04/24.04 VPS, 1 vCPU / 1 GB RAM is enough (no transcoding)
- Docker + Compose v2
- A domain pointing at the server (DuckDNS works)
- Upload bandwidth at least as high as the sum of your outbound bitrates

## License

MIT
