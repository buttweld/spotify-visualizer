"""User-editable audio, display, remote and Google Photos settings."""
import os
from pathlib import Path
import socket
import numpy as np

# ---------------- CONFIGURATION ----------------
FIFO = "/run/spotify-vis/audio.pcm"
AUDIO_DEVICE = "plughw:CARD=vc4hdmi,DEV=0"
DRM_DEVICE = "/dev/dri/card0"
CEC_DEVICE = "/dev/cec0"

# Librespot's raw pipe output is normally 44.1 kHz, S16_LE, stereo.
RATE = 44100
CHANNELS = 2
FPS = 24
VISUAL_DELAY = 0.300  # seconds; delay spectrum only, not audio
VISUAL_DELAY_STEP = 0.020  # seconds per Up/Down press
VISUAL_DELAY_MAX = 2.000  # seconds; also determines spectrum history capacity
VISUAL_DELAY_INFO_TIMEOUT = 2.0  # seconds to show delay after an Up/Down press
HOP = round(RATE / FPS)
FFT_SIZE = 8192

ALSA_BUFFER = 60_000  # microseconds
ALSA_PERIOD = 15_000  # microseconds

IDLE_TIMEOUT = 120.0  # seconds without non-silent PCM
IDLE_RMS_THRESHOLD = 0.0002
IDLE_REFRESH = 30.0  # seconds between QR-menu network info refreshes
DEVICE_NAME = os.environ.get("LIBRESPOT_NAME", f"raspotify ({socket.gethostname()})")

BANDS = 38
SEGMENTS = 21

# Logical screen resolutions; a stable HDMI mode prevents audio resets.
IDLE_SIZE = (1920, 1080)
SPECTRUM_SIZE = (480, 270)
PHOTOS_SIZE = (1920, 1080)
HDMI_SIZE = (1920, 1080)
PHOTO_INTERVAL = 30  # seconds; remote Up/Down changes this by 5 seconds
PHOTO_INTERVAL_STEP = 5
PHOTO_TOKEN = Path(".google-photos-token.json")
PHOTO_CACHE = Path(".cache")
PLAYBACK_ACTIVE_WINDOW = 0.5  # silence gap that allows a new playback-start event

BAR_FALLOFF = 1.8
PEAK_HOLD = 0.20
PEAK_FALLOFF = 0.65

DB_MIN = -65.0
DB_MAX = 5.0

# BGRA reference palette, bottom to top
BASE_PALETTE = np.array([
    (8, 132, 24, 255),
    (0, 148, 41, 255),
    (8, 156, 49, 255),
    (16, 181, 57, 255),
    (16, 190, 50, 255),
    (16, 206, 41, 255),
    (33, 222, 148, 255),
    (41, 222, 189, 255),
    (33, 181, 214, 255),
    (24, 165, 222, 255),
    (8, 123, 198, 255),
    (0, 115, 214, 255),
    (0, 102, 214, 255),
    (0, 90, 214, 255),
    (16, 41, 206, 255),
    (16, 49, 239, 255),
], dtype=np.uint8)

PALETTE = np.column_stack([
    np.interp(
        np.linspace(0, 1, SEGMENTS),
        np.linspace(0, 1, len(BASE_PALETTE)),
        BASE_PALETTE[:, channel],
    )
    for channel in range(4)
]).astype(np.uint8)

BG = (0, 0, 0, 255)
PEAK_COLOR = (150, 150, 150, 255)


# Only settings are exported to the entry point.
__all__ = [name for name in globals() if name.isupper()]
