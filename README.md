# ServerStream

Mini multi-platform streaming server. Stream once from OBS, go live everywhere.

## Architecture

```
OBS ──RTMP──→ SRS (Docker) ──┬──→ YouTube
                              ├──→ Facebook
                              ├──→ Twitch
                              ├──→ TikTok
                              └──→ Any RTMP platform
```

## Quick Start

### 1. Deploy to VPS

```bash
git clone https://github.com/sonnyagent30-beep/ServerStream.git
cd ServerStream
chmod +x scripts/setup.sh
sudo -S -p '' ./scripts/setup.sh
```

### 2. Configure Stream Key

```bash
nano /opt/serversstream/.env
# Set STREAM_KEY to a random string
```

### 3. Add Platform Stream Keys

Edit `srs.conf` and uncomment/add push URLs:

```nginx
vhost __defaultVhost__ {
    push rtmp://a.rtmp.youtube.com/live2/YOUR_YOUTUBE_KEY;
    push rtmp://rtmp-api.facebook.com:80/rtmp/YOUR_FACEBOOK_KEY;
    push rtmp://live.twitch.tv/app/YOUR_TWITCH_KEY;
}
```

### 4. Restart

```bash
cd /opt/serversstream
docker compose restart
```

### 5. Configure OBS

- **Service**: Custom
- **Server**: `rtmp://YOUR_SERVER_IP/live`
- **Stream Key**: your stream key from `.env`

## Ports

| Port | Purpose |
|------|---------|
| 1935 | RTMP ingest (OBS → SRS) |
| 1985 | SRS HTTP API |
| 8080 | HLS / HTTP-FLV playback |
| 8081 | Web dashboard |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/versions` | SRS version info |
| `GET /api/v1/streams` | Active streams |
| `GET /api/v1/clients` | Connected clients |

## Project Structure

```
ServerStream/
├── docker-compose.yml   # Service orchestration
├── srs.conf            # SRS server config
├── web/
│   └── index.html      # Dashboard UI
├── scripts/
│   ├── setup.sh        # VPS deployment script
│   └── obs-guide.md    # OBS configuration guide
├── hooks/              # HTTP callback hooks
└── README.md
```

## Requirements

- VPS with 2+ CPU cores, 4GB+ RAM
- Docker + Docker Compose
- Ports 1935, 1985, 8080, 8081 open in firewall

## License

MIT
