# OBS Setup

## 1. Get the ingest details

Log in to the dashboard at `https://YOUR_DOMAIN` and copy the **RTMPS server**
and **stream key** from the *Ingest* panel.

## 2. Configure OBS

**Settings → Stream**

| Field | Value |
|-------|-------|
| Service | `Custom...` |
| Server | `rtmps://YOUR_DOMAIN:1936/live` |
| Stream Key | *your key from the dashboard* |

Prefer plain RTMP (lower CPU, no TLS)? Use `rtmp://YOUR_DOMAIN:1935/live`.

## 3. Encoder settings

**Settings → Output**

| Setting | Value |
|---------|-------|
| Output Mode | Advanced |
| Encoder | `x264` (or `NVENC` with an NVIDIA GPU) |
| Rate Control | CBR |
| Bitrate | 4500–6000 Kbps for 1080p60; 3000–4500 for 1080p30 |
| **Keyframe Interval** | **2** |
| Preset | `veryfast` |
| Profile | `high` |

**Settings → Video**

| Setting | Value |
|---------|-------|
| Base (Canvas) | 1920×1080 |
| Output (Scaled) | 1920×1080 |
| Downscale Filter | Lanczos |
| FPS | 30 or 60 |

> **A 2-second keyframe interval is not optional.** Every major platform
> rejects — or badly buffers — a stream that keyframes less often. This is the
> single most common cause of "it connects then drops after 30 seconds".

> Keep your total bitrate at or below your *upload* speed. ServerStream
> re-sends one copy per platform, so four 6 Mbps targets need ~24 Mbps upstream.

## 4. Start streaming

Click **Start Streaming**. Within a few seconds each enabled platform should
flip to **restreaming** on the dashboard.

If OBS or the server drops, viewers see your **standby image** rather than a
frozen frame, and streams resume on the real feed automatically once OBS is
back — as long as that happens within the grace period (`STANDBY_SECONDS`,
default 120 s).

## 5. Verify on each platform

- **YouTube** — YouTube Studio → Live → Stream Health
- **Facebook** — Live Producer → your broadcast
- **Twitch** — Creator Dashboard → Stream Manager
- **TikTok** — LIVE Studio

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| OBS "Failed to connect" | Port 1935/1936 closed — check `ufw status` |
| Dashboard shows SRS offline | `docker compose ps`, `docker compose logs manager` |
| Live locally, absent on a platform | Open that platform's row → **logs** in the dashboard. Usually a bad or expired key |
| Facebook rejects | Needs RTMPS **and** a fresh key per broadcast |
| Stream drops every ~30 s | Bitrate too high, or keyframe interval ≠ 2 s |
| Standby image not appearing | No standby image uploaded yet |
| Audio/video drift | Keep `-c copy`; do not transcode in the manager |

## Where to get stream keys

| Platform | Location | Notes |
|----------|----------|-------|
| YouTube | YouTube Studio → Go Live → Stream | Permanent key available |
| Facebook | Live Producer → Streaming software | New key per broadcast, or enable a persistent key |
| Twitch | Creator Dashboard → Settings → Stream | Permanent |
| TikTok | LIVE → Go Live → third-party software | Requires live access on your account |

Enter only the **server URL** and **stream key** in the dashboard — ServerStream
joins them into the full push URL.
