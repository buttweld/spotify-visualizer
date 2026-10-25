# Spotify Visualizer

A Spotify Connect receiver with a Winamp-style HDMI spectrum analyzer for Raspberry Pi Zero 2 W running Raspberry Pi OS Lite. [Raspotify](https://github.com/dtcooper/raspotify) sends decoded audio to a Python application, which plays it over HDMI and renders the spectrum directly through DRM/KMS. No desktop environment is required. When playback is idle, the screen displays the Spotify Connect name and network/SSH connection details.

## Raspberry Pi Setup

Install Raspberry Pi OS Lite using Raspberry Pi Imager:

- Configure a username, Wi-Fi, and SSH.
- Connect an HDMI display with audio support.
- Connect using `ssh <username>@pi.local`.

Install dependencies:

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip \
    alsa-utils libdrm-dev libopenblas0-pthread fonts-dejavu-core v4l-utils
```

Clone the project:

```bash
cd ~
git clone https://github.com/buttweld/spotify-visualizer.git
cd spotify-visualizer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

## Configuration

Edit the constants near the top of `spotify_visualizer.py`:

| Setting | Description |
| --- | --- |
| `FIFO` | Raspotify PCM named pipe. |
| `AUDIO_DEVICE` | HDMI ALSA device; list devices using `aplay -l`. |
| `DRM_DEVICE` | DRM/KMS display device. |
| `CEC_DEVICE` | HDMI-CEC adapter; defaults to `/dev/cec0`. |
| `RATE`, `CHANNELS` | PCM input format (44,100 Hz, stereo). |
| `FPS`, `HOP` | Display frame rate and PCM samples processed per update. |
| `VISUAL_DELAY` | Initial spectrum delay in seconds; defaults to `0.300`. |
| `VISUAL_DELAY_STEP`, `VISUAL_DELAY_MAX` | Remote adjustment step and maximum delay in seconds. |
| `VISUAL_DELAY_INFO_TIMEOUT` | Seconds to show the delay after a remote adjustment; defaults to `2.0`. |
| `FFT_SIZE` | FFT window length; larger windows improve frequency resolution but smooth fast changes. |
| `BANDS`, `SEGMENTS` | Spectrum bars and blocks per bar. |
| `WIDTH`, `HEIGHT` | Requested HDMI output resolution. |
| `DB_MIN`, `DB_MAX` | Decibel range mapped to bar height. |
| `BAR_FALLOFF`, `PEAK_HOLD`, `PEAK_FALLOFF` | Spectrum bar and peak animation timing. |
| `BASE_PALETTE` | BGRA color gradient for spectrum segments. |
| `ALSA_BUFFER`, `ALSA_PERIOD` | Requested playback buffer and period in microseconds. |
| `IDLE_TIMEOUT` | Seconds without non-silent PCM before showing the idle screen. |
| `IDLE_RMS_THRESHOLD` | Minimum audio RMS level considered active playback. |
| `IDLE_REFRESH` | Seconds between idle-screen network-information refreshes. |
| `DEVICE_NAME` | Spotify Connect name read from the `LIBRESPOT_NAME` environment variable; defaults to `raspotify (<hostname>)`. |

Defaults include **720 × 400**, **24 FPS**, **38 bands**, **21 segments**, **8192-point FFT**, **60 ms ALSA buffer**, **15 ms ALSA period**, and a **30-second idle timeout**.

## HDMI-CEC Remote Control

Enable HDMI-CEC on the TV and select the Pi's HDMI input. The visualizer registers as `SpotifyVis` using `cec-ctl` through `hdmi_cec.py`. The service's `AmbientCapabilities=CAP_NET_ADMIN` setting allows CEC monitoring.

| Button | Action |
| --- | --- |
| Up | Increase visual delay by 20 ms. |
| Down | Decrease visual delay by 20 ms. |

Delay ranges from **0–2000 ms** and affects only the spectrum. Each press logs the value and shows `Delay: +300 ms` for **2 seconds**, below the hostname on the idle screen or at the top left of the spectrum. Further presses restart the timer.

Held-button repeats are ignored. Adjustments work during playback and while idle, and reset to `VISUAL_DELAY` on restart.

## Idle Screen

The idle screen appears **immediately after startup**. After audio playback stops or becomes silent, it reappears following `IDLE_TIMEOUT`. Playback automatically returns the display to the spectrum. The idle screen shows:

```text
spotify-visualizer
stream to RPiZero
ssh <username>@<ip-address>
IP Address: <ip-address>
Hostname: <hostname>
```

The SSH username, IP address, and hostname are detected at runtime. The text uses a 12 px DejaVu Sans Mono font with a built-in Pillow font as fallback. Install Python dependencies through `requirements.txt`, which should include **numpy**, **drm-display**, and **Pillow**.

To display the configured Raspotify device name, the visualizer service loads `EnvironmentFile=-/etc/raspotify/conf` as shown above. Review this file before sharing it; it may contain private configuration. An alternative is to set only `LIBRESPOT_NAME` in a separate service environment file.

## Troubleshooting

- **Spotify connects but does not play:** Make sure the visualizer is running and the FIFO exists.
- **No HDMI audio:** Check ALSA devices with `aplay -l` and set `AUDIO_DEVICE` accordingly.
- **Low FPS:** Reduce `WIDTH` and `HEIGHT` to decrease framebuffer transfer cost.
- **Audio lags behind graphics:** Adjust ALSA buffering.
- **CEC permission error:** Add `AmbientCapabilities=CAP_NET_ADMIN` under the visualizer service's `[Service]` section, then reload systemd and restart the service.
- **Remote buttons do nothing:** Enable TV HDMI-CEC and select the Pi's input. Test with `sudo cec-ctl -d /dev/cec0 --playback --osd-name SpotifyVis --monitor`.
