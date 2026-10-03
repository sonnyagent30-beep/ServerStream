# OBS Setup

## 1. Get the ingest details

From the dashboard at `https://sonnystream.duckdns.org` copy the **RTMPS server**
and **stream key**.

## 2. Configure OBS

**Settings -> Stream**

| Field | Value |
|-------|-------|
| Service | `Custom...` |
| Server | `rtmps://sonnystream.duckdns.org:1936/live` |
| Stream Key | *your key from the dashboard* |

Tick **Use authentication** only if you set `Use Auth` in OBS; otherwise leave it off.

Prefer plain RTMP (lower CPU, no TLS)? Use `rtmp://sonnystream.duckdns.org:1935/live`.

**Settings -> Output**

| Setting | Value |
|---------|-------|
| Output Mode | Advanced |
| Encoder | x264 (or NVENC if you have an NVIDIA GPU) |
| Rate Control | CBR |
| Bitrate | 4500-6000 Kbps for 1080p60, 3000-4500 for 1080p30 |
| Keyframe Interval | **2** (required by YouTube/Twitch/Facebook) |
| Preset | veryfast |
| Profile | high |

**Settings -> Video**

| Setting | Value |
|---------|-------|
| Base (Canvas) | 1920x1080 |
| Output (Scaled) | 1920x1080 |
| Downscale Filter | Lanczos |
| FPS | 30 or 60 |

> A **keyframe interval of 2 seconds** matters. Platforms reject or badly buffer
> streams that keyframe less often.

## 3. Start streaming

Click **Start Streaming**. The dashboard should flip to *restreaming* on each
enabled platform within ~5 seconds.

## 4. Verify on each platform

- **YouTube** - YouTube Studio -> Live -> Stream Health
- **Facebook** - Live Producer -> your broadcast
- **Twitch** - Creator Dashboard -> Stream Manager
- **TikTok** - LIVE Studio / your live dashboard

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| OBS "Failed to connect" | Port 1935/1936 closed. Check `ufw status`. |
| Dashboard shows SRS offline | `docker compose ps`; `docker logs serversstream-srs` |
| Stream live locally but not on a platform | Open the platform row -> **logs**. Usually a bad/expired key. |
| Facebook rejects | Facebook needs RTMPS **and** a fresh key per broadcast. Re-copy the key into the platform row. |
| Platform drops every ~30s | Bitrate too high for your upload, or keyframe interval is not 2. |
| Audio/video drift | Use `-c copy` (default). Do not transcode in the manager. |
| High latency on the local preview | Expected with HLS (~6-10s). Platforms get the feed directly. |

## Stream keys by platform

| Platform | Where to get the key | Notes |
|----------|---------------------|-------|
| YouTube | YouTube Studio -> Go Live -> Stream | Permanent key available |
| Facebook | Live Producer -> Streaming software | New key per broadcast, or enable a persistent key |
| Twitch | Creator Dashboard -> Settings -> Stream | Permanent |
| TikTok | LIVE -> Go Live -> third-party software | Requires live access on your account |

Enter only the **server URL** and **stream key** in the dashboard; ServerStream
joins them into the full push URL.
