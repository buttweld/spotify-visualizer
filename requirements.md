# Project requirements and maintenance notes

This file records the intended behavior and implementation details for future changes. Read it before changing audio, display transitions, KMS rendering, or Google Photos storage. [README.md](README.md) covers installation, normal use, and Google API setup; `requirements.txt` lists Python dependencies.

## Accepted baseline

- Raspberry Pi Zero 2 W, Raspberry Pi OS Lite, HDMI audio and display, no desktop.
- Hardware KMS scaling is the supported display path.
- `SPECTRUM_SIZE = (480, 270)` achieves 24 FPS at about 12.4 MB/s on the user's Pi/TV.
- Occasional ALSA underruns are acceptable to the user. Do not change audio buffering, scheduling, or FFT architecture to address them unless requested.

## Screens and input

The TV's IR remote reaches the Pi through **HDMI-CEC**. Enable CEC on the TV and select the Pi's HDMI input. No separate IR receiver is required. The app registers as `SpotifyVis` through `cec-ctl`.

| Event | Display behavior |
| --- | --- |
| Startup | Idle QR screen, with a full 30-second timeout. |
| Playback starts while Idle is visible | Switch to Spectrum. |
| Spectrum has no non-silent PCM for `IDLE_TIMEOUT` | Switch to Google Photos. |
| Idle stays visible for `IDLE_TIMEOUT` | Switch to Google Photos, including when Idle was selected manually during playback. |
| Remote OK / Select | Cycle **Idle / QR → Spectrum → Slideshow → Idle / QR**. |
| Playback starts while Google Photos is visible | Keep Google Photos visible; audio plays normally. |

Playback detection uses PCM activity rather than a Spotify account API. A silence gap of `PLAYBACK_ACTIVE_WINDOW` (default 0.5 seconds) permits a new playback-start event. Selecting Idle during an ongoing stream leaves it visible until its timeout or a new playback start. Selecting Spectrum while already silent gives it a fresh timeout.

| Button | Idle / Spectrum | Google Photos |
| --- | --- | --- |
| OK / Select | Next screen | Next screen |
| Left | No action | Previous photo, wrapping to the end |
| Right | No action | Next photo, wrapping to the start |
| Up | Increase spectrum delay by 20 ms | Increase photo interval by 5 seconds |
| Down | Decrease spectrum delay by 20 ms | Decrease photo interval by 5 seconds, minimum 5 seconds |

Spectrum delay ranges from 0–2000 ms and affects only the animation. `Delay: +300 ms` appears for two seconds after adjustment. The slideshow defaults to **30 seconds**. Changing the interval shows `Interval: 30 s` at the top left for **two seconds**; each adjustment restarts the label timer. The label is hidden during ordinary playback and startup. Photo navigation and interval adjustments restart the photo timer. Held-button repeats are ignored. Delay and interval adjustments reset to configured defaults on application restart.

Audio ingestion, ALSA output, and FFT work stay in their own thread. Google requests, cache validation, and photo decoding run in background workers. Switching screens keeps the same DRM display open and does not restart the audio player.

## Configuration reference

Edit **`visualizer/config.py`**, then restart the service.

| Setting | Default / meaning |
| --- | --- |
| `IDLE_SIZE` | `(1920, 1080)` logical Idle QR-menu resolution. |
| `SPECTRUM_SIZE` | `(480, 270)` logical Spectrum resolution. |
| `PHOTOS_SIZE` | `(1920, 1080)` logical slideshow resolution; also requested from Google for new downloads. |
| `HDMI_SIZE` | `(1920, 1080)` fixed physical output resolution. |
| `PHOTO_INTERVAL`, `PHOTO_INTERVAL_STEP` | 30 seconds initially; 5-second remote adjustments. |
| `PHOTO_TOKEN`, `PHOTO_CACHE` | `.google-photos-token.json` and `.cache/`, relative to the working directory. |
| `FIFO` | `/run/spotify-vis/audio.pcm`, Raspotify PCM pipe. |
| `AUDIO_DEVICE`, `DRM_DEVICE`, `CEC_DEVICE` | HDMI ALSA, DRM/KMS, and HDMI-CEC devices. |
| `RATE`, `CHANNELS`, `FPS`, `HOP` | PCM format, spectrum frame rate, and samples per FFT update. |
| `FFT_SIZE`, `BANDS`, `SEGMENTS` | 8192-point FFT, 38 bars, 21 blocks per bar. |
| `DB_MIN`, `DB_MAX`, `BASE_PALETTE` | Spectrum range and BGRA colors. |
| `BAR_FALLOFF`, `PEAK_HOLD`, `PEAK_FALLOFF` | Spectrum animation timing. |
| `ALSA_BUFFER`, `ALSA_PERIOD` | 60 ms playback buffer and 15 ms period. |
| `IDLE_TIMEOUT` | 30 seconds before automatic Photos transition. |
| `IDLE_RMS_THRESHOLD` | Minimum PCM RMS considered active playback. |
| `PLAYBACK_ACTIVE_WINDOW` | 0.5-second activity window for playback-start detection. |
| `IDLE_REFRESH` | 30 seconds between QR-menu network-information refreshes. |
| `DEVICE_NAME` | `LIBRESPOT_NAME`, otherwise `raspotify (<hostname>)`. |
| `VISUAL_DELAY`, `VISUAL_DELAY_STEP`, `VISUAL_DELAY_MAX` | Initial 300 ms spectrum delay, 20 ms adjustment, 2000 ms maximum. |
| `VISUAL_DELAY_INFO_TIMEOUT` | Two seconds for the delay label. |

Screen resolutions are source framebuffer resolutions. The hardware KMS overlay scales and centers each source within the fixed HDMI output; Python does not enlarge spectrum frames into 1080p images. The default 480 × 270 spectrum uploads 12.4 MB/s of source pixels at 24 FPS. The user confirmed this frame rate on the Pi/TV. The display hardware still scans out the physical HDMI mode.

Physical HDMI mode changes can reset the HDMI audio clock, so screen transitions update only the overlay plane's framebuffer and source/destination rectangles. They do not touch the connector, CRTC mode, primary plane or ALSA player. Set `HDMI_SIZE` to a mode supported by your TV. The KMS driver chooses scaling quality; nearest-neighbour is requested when its scaling-filter property offers it.

## KMS implementation

`visualizer/kms.py` loads `.cache/native/libkms_plane.so`, built from `native/kms_plane.c` using `scripts/build_kms.py`. Build on the target Pi; compiled binaries stay local and are excluded from sync. Rebuild after native source changes or architecture changes. No rebuild is needed for Python-only configuration changes.

The helper borrows the existing `drm-display` file descriptor and CRTC. It chooses an unused XRGB8888 overlay plane compatible with that CRTC and validates every unique configured source resolution using atomic test-only commits. The connector and CRTC mode stay fixed; no presentation commit permits a modeset.

Two persistently mapped buffers are allocated per unique source size. Uploads copy only source pixels, row by row using the actual DRM pitch. Synchronous atomic commits alternate buffers, so uploads do not modify the frame currently being scanned out. The primary framebuffer is cleared once at startup for black borders. Cleanup restores the overlay before releasing its buffers and closing the display owner.

Request nearest-neighbour scaling when the driver exposes it. Preserve aspect ratio and center the source. Missing/stale native binaries and unsupported plane configurations produce actionable errors instead of silently switching to software scaling.

Startup logs identify the selected overlay and tested source sizes. Spectrum logs report achieved FPS, source upload volume, and mean presentation time, including display-flip waits. These are diagnostics, not additional performance targets beyond the accepted baseline.

## Google Photos selection and cache

- Idle/startup is the dark QR menu, including Spotify Connect name, SSH target, hostname, and local-retention disclosure. There is no extra title screen or QR sub-screen inside the slideshow.
- Start selection in the background. Reuse an active session on returning to Idle; ignore events from older session generations. Deliberate cancellation must not produce an error flash.
- Sessions may finish while another screen is visible. Successful selection switches Idle to the slideshow; otherwise preserve the manually selected screen.
- Poll at Google's supplied interval and stop at its timeout. Retrieve all pages and filter out videos. Download photo bytes with bearer authentication at `PHOTOS_SIZE`.
- Validate all downloads before atomically publishing the latest-selection marker. Empty/video-only selections, cancellation, timeout, and selections with no usable downloads preserve the previous selection. If only some downloads fail, the valid selected photos become the new selection.
- Keep network requests, cache validation and image decoding outside the audio/display threads. Keep only the current and next decoded frames in memory.
- Resume `.cache/google-photos-latest.txt` automatically. It identifies a `.cache/google-photos-*/` directory containing numerically ordered `.photo` files. Earlier selections remain until deleted.
- Shuffle each complete slideshow cycle so every photo appears once before reshuffling. Avoid repeating the last photo of one cycle as the first of the next when more than one photo exists. Left/Right follows the shuffled order; moving forward past its end starts a new shuffled cycle. New selections start a fresh order. Cached file order on disk remains unchanged.
- Center photos with black borders, preserving orientation and aspect ratio. A photo navigation or interval adjustment resets the advance timer. The interval label appears for two seconds only after Up/Down, then clears even if the photo has not changed.

## Data retention and deletion

Downloaded Picker image URLs expire after approximately 60 minutes; the app saves photo bytes rather than URLs. Google's documented 60-minute media-caching rule explicitly applies to the **Library API only**. The Picker documentation reviewed does not state an equivalent fixed retention limit for downloaded photo bytes. This supports retaining explicitly selected photos for this personal slideshow, but is not an unconditional guarantee that indefinite retention is permitted for every application or distribution.

The app retains your selected photos locally until you delete them. It does not upload them to another service or use them for advertising or model training. Use of information received from Photos APIs follows Google's Limited Use requirements. Google's general Photos policies still require clear consent and disclosure, deletion controls, and secure storage, including encryption at rest for user data and tokens. This demo uses filesystem permissions, **not application-level encryption**. An encrypted filesystem/storage solution is needed to meet that requirement; these code changes alone do not establish full policy compliance for a distributed product.

References: [caching guidance](https://developers.google.com/photos/overview/best-practices#caching), [Picker media URLs](https://developers.google.com/photos/picker/guides/media-items), [Photos API data policy](https://developers.google.com/photos/support/api-policy).

To delete all downloaded selections and their marker, stop the app and run from the project directory:

```bash
sudo systemctl stop spotify-visualizer
rm -rf -- .cache/google-photos-*
```

Delete `.cache/saved-photos` separately if you made the older manual backup. To disconnect Google access as well, revoke the app in your Google Account's third-party connections and remove `.google-photos-token.json`. Revoking Google access alone does not erase offline copies; delete the cache too. Earlier selections remain on disk until you remove them; allow enough disk space and remove unused directories as needed.

## Operations and troubleshooting

The service working directory must be the project root. Token and photo-cache ownership must match the service account. After a previous `sudo` demo run, fix existing paths before starting the regular-user service:

```bash
sudo chown -R "$(id -un):$(id -gn)" .cache
sudo chown "$(id -un):$(id -gn)" .google-photos-token.json
chmod 600 .google-photos-token.json
```

Use the service account instead of the current username if different. Testing-mode refresh credentials may expire after seven days; reauthorize if required. Offline cached photos remain usable.

A successful `Authorization saved` message followed by an SSH channel `Connection refused` may be an extra browser request after the temporary callback server closes. The SSH tunnel is needed only during authorization. If port 8080 is busy, use matching alternative ports in `ssh -L` and `--auth-port`.

If audio is missing, check `aplay -l` and `AUDIO_DEVICE`. For CEC, keep `CAP_NET_ADMIN`, enable CEC on the TV, and inspect `cec-ctl` output. For Photos HTTP 403, check the enabled Photos Picker API, scope, and test-user email. For KMS errors, rebuild the helper and check the requested sizes and plane availability. Stop other DRM renderers before running standalone demos.

To import an old manually saved `.cache/saved-photos` directory, while the app is stopped:

```bash
cp -a .cache/saved-photos .cache/google-photos-imported
printf '%s' 'google-photos-imported' > .cache/google-photos-latest.txt
```

## Source layout and demos

```text
spotify_visualizer.py          Audio worker and display orchestration
hdmi_cec.py                    TV remote transport and press detection
visualizer/
  config.py                   User-editable configuration
  state.py                    Screen transitions and slideshow controls
  render.py                   Spectrum and stable HDMI output
  kms.py                      Python interface to the scaling overlay
  photos/
    api.py                    OAuth and Google Photos Picker API
    cache.py                  Downloads, validation, saved selection
    controller.py             Background sessions and image decoding
    render.py                 Photos and QR menu theme
    demo.py                   Standalone demo command implementation
demos/
  google_photos_demo.py        Authorization and standalone slideshow entry point
  hdmi_cec_demo.py             Standalone remote-event viewer
native/
  kms_plane.c                 Atomic KMS plane scaling and mapped buffers
scripts/
  build_kms.py                Target-native helper build and native tests
  sync_to_rpi.sh              Code sync, excluding private/runtime files
tests/                        Hardware-independent regression tests
requirements.txt              All Python dependencies
requirements.md               Behavior, implementation and maintenance notes
```

The separate Photos demo still uses 1920 × 1080 and a one-second interval. Stop the visualizer first to release DRM:

```bash
sudo systemctl stop spotify-visualizer
sudo .venv/bin/python demos/google_photos_demo.py --resume
```

Without `--resume`, the demo offers a new QR selection. `--resume DIRECTORY` plays a specific cache directory. Press Ctrl+C to exit, then restart the visualizer service. For CEC diagnostics use `sudo .venv/bin/python demos/hdmi_cec_demo.py` while the visualizer is stopped.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_kms.py --test
```

Python tests cover state transitions, remote controls, overlay expiry, cache retention on failed selection, background decoding, API pagination/polling, and a single audio/display lifecycle. Native tests exercise the real upload/commit code using a fake atomic transaction sink, including pitch padding, double buffering, failed commits, and cleanup. They require a compiler and libdrm headers but no DRM device.

For display changes, validate on the Pi/TV: confirm hardware-scaling logs, check the configured source size, play music and cycle screens. Preserve the accepted playback behavior rather than treating an occasional underrun as a new defect.
