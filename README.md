# youtube_count_event
Counts YouTube video views and sends OSC broadcast events when the count goes up.

## Script
Run `youtube_view_watcher.py` with a YouTube Data API v3 key:

```bash
python youtube_view_watcher.py "https://www.youtube.com/watch?v=VIDEO_ID" --api-key YOUR_API_KEY
```

Or set `YOUTUBE_API_KEY` first and omit `--api-key`.

Optional flags:

- `--port 5005` sets the OSC-over-UDP destination port.
- `--interval 3` sets the polling interval in seconds.
- `--broadcast-ip 192.168.1.255` overrides the auto-detected `.255` broadcast address.

Behavior:

- On the first successful API call, it prints the initial view count.
- On every successful check, it sends `/viewcheck` with `0` for no new views and `1` when new views were detected.
- If the next count is higher, it also sends the delta as an OSC message to `/newviews`.
- If the count is unchanged, it prints `same views`.
- If the count goes down, it skips the broadcast and logs that case.

## Note
Polling every 3 seconds consumes YouTube Data API quota quickly.

## Linux Service
To run the watcher continuously on Linux, use a `systemd` service.

### 1. Copy the project to the target machine
Example location:

```bash
sudo mkdir -p /opt/youtube_count_event
sudo cp -r /path/to/your/repo/* /opt/youtube_count_event/
```

Make sure Python 3 is installed:

```bash
python3 --version
```

### 2. Create an environment file
Create `/etc/youtube_view_watcher.env`:

```ini
YOUTUBE_API_KEY=YOUR_API_KEY_HERE
```

This keeps the API key out of the service file.

### 3. Create the systemd service file
Create `/etc/systemd/system/youtube_view_watcher.service` with this content:

```ini
[Unit]
Description=YouTube View Watcher OSC Broadcaster
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/youtube_count_event
EnvironmentFile=/etc/youtube_view_watcher.env
ExecStart=/usr/bin/python3 /opt/youtube_count_event/youtube_view_watcher.py "https://www.youtube.com/watch?v=VIDEO_ID" --interval 3 --port 5005 --broadcast-ip 192.168.1.255
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Replace these values before starting it:

- `VIDEO_ID` with the YouTube video you want to monitor.
- `192.168.1.255` with your actual broadcast address, or remove the full `--broadcast-ip ...` part if you want the script to auto-detect it.
- `--interval 3` and `--port 5005` if you want different values.

Because the script reads `YOUTUBE_API_KEY` from the environment, you do not need to put `--api-key` in `ExecStart`.

### 4. Reload systemd and enable the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable youtube_view_watcher.service
sudo systemctl start youtube_view_watcher.service
```

### 5. Check status and logs

```bash
sudo systemctl status youtube_view_watcher.service
sudo journalctl -u youtube_view_watcher.service -f
```

### 6. Update the service later
If you change the service file:

```bash
sudo systemctl daemon-reload
sudo systemctl restart youtube_view_watcher.service
```

If you only change the Python script:

```bash
sudo systemctl restart youtube_view_watcher.service
```

### Optional: run it as a dedicated user
If you do not want to run the service as root, create a dedicated user and set ownership:

```bash
sudo useradd --system --home /opt/youtube_count_event --shell /usr/sbin/nologin youtubeview
sudo chown -R youtubeview:youtubeview /opt/youtube_count_event
```

Then add this line under `[Service]`:

```ini
User=youtubeview
```
