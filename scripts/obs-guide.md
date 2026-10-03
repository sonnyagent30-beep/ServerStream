# OBS Setup Guide for ServerStream

## Step 1: Get Your Connection Details

From the ServerStream dashboard, copy:
- **Server URL**: `rtmp://your-server-ip/live`
- **Stream Key**: your custom stream key

## Step 2: Configure OBS

1. Open OBS Studio
2. Go to **Settings** → **Stream**
3. Set **Service** to `Custom...`
4. Set **Server** to your Server URL (e.g., `rtmp://192.168.1.100/live`)
5. Set **Stream Key** to your stream key
6. Click **OK**

## Step 3: Start Streaming

1. Click **Start Streaming** in OBS
2. Check the ServerStream dashboard — you should see an active stream
3. Your stream is now being pushed to all configured platforms

## Step 4: Verify on Platforms

- **YouTube**: Go to YouTube Studio → Live → Stream Health
- **Facebook**: Go to your Facebook Live dashboard
- **Twitch**: Go to Twitch Creator Dashboard → Stream Manager
- **TikTok**: Go to TikTok Live Studio

## Troubleshooting

| Problem | Solution |
|---------|----------|
| OBS can't connect | Check firewall: `ufw status` — port 1935 must be open |
| Stream not appearing on platform | Verify stream key in srs.conf push URL |
| High latency | Use FLV playback instead of HLS |
| Dashboard not loading | Check port 8081 is open: `ufw allow 8081/tcp` |
