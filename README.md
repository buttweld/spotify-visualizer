# Spotify Visualizer

A Spotify Connect receiver, spectrum analyzer, and Google Photos slideshow for Raspberry Pi Zero 2 W running Raspberry Pi OS Lite. HDMI audio and hardware-scaled graphics work without a desktop.

Development requirements and implementation notes are in [requirements.md](requirements.md).

## Raspberry Pi Setup

Install Raspberry Pi OS Lite using Raspberry Pi Imager:

- Configure a username, Wi-Fi, and SSH.
- Connect an HDMI display with audio support.
- Connect using `ssh <username>@pi.local`.

Install dependencies:

```bash
sudo apt update
sudo apt install -y git curl build-essential python3 python3-venv python3-pip \
    alsa-utils libdrm-dev libopenblas0-pthread fonts-dejavu-core v4l-utils
```

Clone the project:

```bash
cd ~
git clone https://github.com/buttweld/spotify-visualizer.git
cd spotify-visualizer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_kms.py
```

Install [Raspotify](https://github.com/dtcooper/raspotify) following its official installation instructions. Set the following in `/etc/raspotify/conf` (choose a Spotify Connect device name):

```ini
LIBRESPOT_NAME="RPiZero"
LIBRESPOT_BITRATE=320
LIBRESPOT_BACKEND="pipe"
LIBRESPOT_DEVICE="/run/spotify-vis/audio.pcm"
LIBRESPOT_FORMAT=S16
```

Create the audio pipe (replace `<username>` with the Raspberry Pi user):

```bash
sudo tee /etc/tmpfiles.d/spotify-vis.conf >/dev/null <<'EOF'
d /run/spotify-vis 0770 root <username> -
p /run/spotify-vis/audio.pcm 0660 root <username> -
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/spotify-vis.conf
```

Allow Raspotify to access the pipe:

```bash
sudo systemctl edit raspotify
```

Add:

```ini
[Service]
ReadWritePaths=/run/spotify-vis
PrivateUsers=yes
```

Test the visualizer in one terminal:

```bash
cd ~/spotify-visualizer
sudo .venv/bin/python spotify_visualizer.py
```

In another terminal, restart Raspotify and play music using Spotify Connect:

```bash
sudo systemctl restart raspotify
```

## Automatic Startup on Boot

Create `/etc/systemd/system/spotify-visualizer.service` (replace `<username>`):

```ini
[Unit]
Description=Spotify HDMI Spectrum Visualizer
Wants=sound.target
After=systemd-tmpfiles-setup.service sound.target

[Service]
Type=simple
User=<username>
Group=<username>
SupplementaryGroups=audio video render
AmbientCapabilities=CAP_NET_ADMIN
WorkingDirectory=/home/<username>/spotify-visualizer
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/raspotify/conf
ExecStartPre=/usr/bin/test -p /run/spotify-vis/audio.pcm
ExecStart=/home/<username>/spotify-visualizer/.venv/bin/python /home/<username>/spotify-visualizer/spotify_visualizer.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Add startup ordering to the same Raspotify override (keep its `[Service]` settings):

```ini
[Unit]
Requires=spotify-visualizer.service
After=spotify-visualizer.service
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-visualizer.service
sudo systemctl enable --now raspotify.service
```

Check status and logs:

```bash
systemctl status spotify-visualizer raspotify
journalctl -u spotify-visualizer -b -n 50 --no-pager
journalctl -u raspotify -b -n 50 --no-pager
```

## Screens and controls

The three screens are **Idle / QR → Spectrum → Slideshow**. Startup shows Idle. Music starting in Idle opens Spectrum; after 30 seconds of inactivity, the slideshow starts. Idle also moves to the slideshow after 30 seconds. Audio continues in every screen.

Enable HDMI-CEC on your TV to use its remote:

| Button | Idle / Spectrum | Slideshow |
| --- | --- | --- |
| OK | Next screen | Next screen |
| Left / Right | — | Previous / next photo |
| Up / Down | Spectrum delay ±20 ms | Photo interval ±5 seconds |

Photos play once each in a shuffled order, then reshuffle for the next cycle. Left/Right follows that order. The slideshow defaults to 30 seconds, with a minimum of 5 seconds. Delay and interval labels appear for two seconds after adjustment.

## Configuration

Edit `visualizer/config.py`, then restart the service. Defaults:

- Idle and photos: **1920 × 1080**.
- Spectrum: **480 × 270**, hardware-scaled to the fixed **1920 × 1080** HDMI output.
- Spectrum: **24 FPS**, 38 bands, 21 segments, 300 ms visual delay.
- Idle timeout and slideshow interval: **30 seconds**.

Build the native helper on the Pi after changes to `native/kms_plane.c`:

```bash
.venv/bin/python scripts/build_kms.py
```

## Google Photos setup

Google Photos is optional. Existing photos resume automatically; selecting new photos requires authorization and internet access.

### Create credentials

1. Create or select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Google Photos Picker API** (`photospicker.googleapis.com`).
3. Choose **User data** in the credentials wizard. This allows a person to grant photo access through OAuth. **Application data** uses a service account, which Google Photos does not support.
4. Configure **Google Auth Platform → Branding** with the app name and contact emails.
5. Under **Audience**, choose **External**, keep status **Testing**, then **Test users → Add users**. Add the Google email address you will use for Photos and save.
6. Under **Data Access**, add `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.
7. Under **Clients**, create a **Desktop app** client and **Download JSON**.
8. Copy the complete downloaded file to the Pi as `~/spotify-visualizer/credentials-google-photos.json`. A client ID ending in `.apps.googleusercontent.com` alone is not the JSON file.

For example, from Windows PowerShell, using your actual download filename and Pi username:

```powershell
scp "$HOME\Downloads\YOUR_DOWNLOADED_FILE.json" buttweld@pi.local:~/spotify-visualizer/credentials-google-photos.json
```

### Authorize once

From your computer, connect with SSH port forwarding:

```bash
ssh -L 8080:127.0.0.1:8080 buttweld@pi.local
```

`-L` forwards your computer's port 8080 to the Pi's callback listener. Keep this SSH session open. At the Pi prompt, run:

```bash
cd ~/spotify-visualizer
.venv/bin/python demos/google_photos_demo.py --authorize
```

Open the printed link in the browser **on that same computer**, not your phone. Sign in with the test-user account. For your own unverified app, choose **Advanced → Go to [app name] (unsafe)** if shown, then approve access. If authorization times out, rerun the command.

Success saves `.google-photos-token.json`. The tunnel is no longer needed. Run authorization as the visualizer service user; see [ownership troubleshooting](requirements.md#operations-and-troubleshooting) if an earlier demo ran under `sudo`.

### Select photos

On the Idle screen, scan the QR code with your phone, use the same Google account, select photos, and tap **Done**. Photos download and the slideshow starts. Videos are skipped. An empty, cancelled, or expired selection keeps the previous photos.

Photos remain locally until deleted. To clear all downloaded selections, stop the app and run:

```bash
sudo systemctl stop spotify-visualizer
rm -rf -- .cache/google-photos-*
sudo systemctl start spotify-visualizer
```

Credentials and tokens are private. The sync script excludes them and `.cache/`. See [retention and deletion notes](requirements.md#data-retention-and-deletion) for storage details and Google policy references.

## Updating and troubleshooting

Sync code with `scripts/sync_to_rpi.sh` (`--dry-run` previews changes). Then on the Pi:

```bash
cd ~/spotify-visualizer
sudo systemctl stop spotify-visualizer
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_kms.py
sudo systemctl start spotify-visualizer
```

The native helper must be built on the Pi; compiled binaries are not synced. Configuration-only changes do not need rebuilding.

Check `journalctl -u spotify-visualizer -f` for errors and spectrum FPS. If audio is missing, check `aplay -l` and `AUDIO_DEVICE`. Enable TV HDMI-CEC for remote control. Additional configuration, demos, and tests are documented in [requirements.md](requirements.md).
