# Spotify Visualizer

A Spotify Connect receiver with a Winamp-style HDMI spectrum analyzer for Raspberry Pi Zero 2 W running Raspberry Pi OS Lite. [Raspotify](https://github.com/dtcooper/raspotify) sends decoded audio to a Python application, which plays it over HDMI and renders the spectrum directly through DRM/KMS. No desktop environment is required.

## Raspberry Pi Setup

Install Raspberry Pi OS Lite using Raspberry Pi Imager:

- Configure a username, Wi-Fi, and SSH.
- Connect an HDMI display or TV with audio support.
- Connect using `ssh <username>@pi.local`.

Install dependencies:

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip \
    alsa-utils libdrm-dev libopenblas0-pthread
```

Clone the project (substitute its repository URL):

```bash
cd ~
git clone <repository-url> spotify-visualizer
cd spotify-visualizer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Install [Raspotify](https://github.com/dtcooper/raspotify) following its official installation instructions. Set the following in `/etc/raspotify/conf`:

```ini
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
.venv/bin/python spotify_visualizer.py
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
WorkingDirectory=/home/<username>/spotify-visualizer
Environment=PYTHONUNBUFFERED=1
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

## Configuration

Edit the constants at the beginning of `spotify_visualizer.py`:

| Setting | Description |
| --- | --- |
| `FIFO` | Named pipe receiving Spotify PCM. |
| `DEVICE` | ALSA HDMI playback device; check with `aplay -l`. |
| `RATE` | PCM sample rate: 44,100 Hz for the configured librespot pipe. |
| `FPS`, `HOP` | Target display FPS and audio samples between FFT updates. |
| `FFT_SIZE` | Analysis window: larger gives better frequency resolution but less responsive transients. |
| `BANDS`, `SEGMENTS` | Frequency bars and colored blocks per bar. |
| `WIDTH`, `HEIGHT` | Requested HDMI DRM/KMS mode. |
| `DB_MIN`, `DB_MAX` | Input level range mapped to bar height. |
| `BAR_FALLOFF` | Speed of bar decay. |
| `PEAK_HOLD` | Duration before peak indicator starts falling. |
| `PEAK_FALLOFF` | Peak indicator falling speed. |
| `BASE_PALETTE` | BGRA colors interpolated to the chosen segment count. |
| `ALSA_BUFFER`, `ALSA_PERIOD` | Playback buffer and period in microseconds, when defined. |

The current configuration uses **38 bands, 21 segments, an 8192-point FFT, 24 FPS, and 720 × 400 resolution**.

## Troubleshooting

- **No HDMI audio:** Check ALSA devices with `aplay -l` and set `DEVICE` accordingly.
- **Low FPS:** Reduce `WIDTH` and `HEIGHT` to decrease framebuffer transfer cost.
- **Audio lags behind graphics:** Adjust ALSA buffering.
