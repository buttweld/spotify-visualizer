"""Raspotify FIFO -> HDMI audio + Winamp-style DRM spectrum visualization.

Expected Raspotify settings:
    LIBRESPOT_BACKEND="pipe"
    LIBRESPOT_DEVICE="/run/spotify-vis/audio.pcm"
    LIBRESPOT_FORMAT="S16"

Start this program before initiating Spotify Connect playback.
"""

from collections import deque
import os
import getpass
import re
import socket
import subprocess
import threading
import time
import numpy as np
from drm_display import Screen
from PIL import Image, ImageDraw, ImageFont

from hdmi_cec import monitor_cec

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

IDLE_TIMEOUT = 30.0  # seconds without non-silent PCM
IDLE_RMS_THRESHOLD = 0.0002
IDLE_REFRESH = 30.0  # seconds between network info refreshes
DEVICE_NAME = os.environ.get("LIBRESPOT_NAME", f"raspotify ({socket.gethostname()})")

BANDS = 38
SEGMENTS = 21

WIDTH = 720
HEIGHT = 400

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

WINDOW = np.hanning(FFT_SIZE).astype(np.float32)
FREQ = np.fft.rfftfreq(FFT_SIZE, 1.0 / RATE)
EDGES = np.geomspace(40, 16000, BANDS + 1)
BINS = []
for i in range(BANDS):
    start = max(1, int(np.searchsorted(FREQ, EDGES[i])))
    end = max(start + 1, int(np.searchsorted(FREQ, EDGES[i + 1])))
    BINS.append((start, end))

# Bounded history of 38-value FFT results (~160 bytes per frame).
spectrum_history = deque(maxlen=max(4, int(max(VISUAL_DELAY, VISUAL_DELAY_MAX) * FPS) + 5))
zero_levels = np.zeros(BANDS, dtype=np.float32)
lock = threading.Lock()
stop = threading.Event()
last_audio_activity = time.monotonic() - IDLE_TIMEOUT
visual_delay = min(VISUAL_DELAY_MAX, max(0.0, VISUAL_DELAY))
visual_delay_info_until = 0.0


def handle_cec_press(button):
    """Adjust spectrum timing once per Up/Down press, including while idle."""
    global visual_delay, visual_delay_info_until
    if button not in ("up", "down"):
        return
    adjustment = VISUAL_DELAY_STEP if button == "up" else -VISUAL_DELAY_STEP
    with lock:
        visual_delay = round(min(VISUAL_DELAY_MAX, max(0.0, visual_delay + adjustment)), 6)
        delay_ms = round(visual_delay * 1000)
        visual_delay_info_until = time.monotonic() + VISUAL_DELAY_INFO_TIMEOUT
    print(f"\nVisual delay: {delay_ms} ms", flush=True)


def visual_delay_info(now):
    """Return the temporary delay label. Caller must hold lock."""
    if now < visual_delay_info_until:
        return f"Delay: {round(visual_delay * 1000):+d} ms"
    return None


def render_delay_info(text):
    """Render a small BGRA label, cached by the display loop until it changes."""
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    font = (ImageFont.truetype(font_path, 12)
            if os.path.isfile(font_path) else ImageFont.load_default())
    width = font.getbbox(text)[2] + 1
    image = Image.new("RGB", (width, 24), "black")
    ImageDraw.Draw(image).text((0, 0), text, font=font, fill="white")
    rgb = np.asarray(image, dtype=np.uint8)
    patch = np.empty((24, width, 4), dtype=np.uint8)
    patch[:, :, :3] = rgb[:, :, ::-1]
    patch[:, :, 3] = 255
    return patch


def show_delay_info(canvas, patch, idle=False, clear=True):
    """Replace the delay line, including clearing it when the timer expires."""
    x, y = 24, 144 if idle else 24
    height, width = canvas.shape[:2]
    if y >= height or x >= width:
        return
    bottom = min(y + 24, height)
    if clear:
        canvas[y:bottom, x:] = BG
    if patch is not None:
        right = min(x + patch.shape[1], width)
        canvas[y:bottom, x:right] = patch[:bottom - y, :right - x]


def delayed_spectrum(now):
    """Return the newest eligible spectrum. Caller must hold lock."""
    cutoff = now - visual_delay
    # Keep older frames so increasing the delay can use them immediately.
    for timestamp, spectrum in reversed(spectrum_history):
        if timestamp <= cutoff:
            return spectrum
    return zero_levels


def local_ipv4():
    """Read the preferred LAN source IP without sending network traffic."""
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})", result.stdout)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        match = re.search(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/", result.stdout)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "Not connected"


def draw_idle_screen(width, height):
    """Render a plain, small-text idle screen on black."""
    ip = local_ipv4()
    hostname = socket.gethostname()
    user = getpass.getuser()

    lines = [
        "spotify-visualizer",
        f"stream to {DEVICE_NAME}",
        f"ssh {user}@{ip}",
        f"IP Address: {ip}",
        f"Hostname: {hostname}",
    ]

    image = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    font = (ImageFont.truetype(font_path, 12)
            if os.path.isfile(font_path) else ImageFont.load_default())

    x, y = 24, 24
    for line in lines:
        draw.text((x, y), line, font=font, fill="white")
        y += 24

    rgb = np.asarray(image, dtype=np.uint8)
    bgra = np.empty((height, width, 4), dtype=np.uint8)
    bgra[:, :, :3] = rgb[:, :, ::-1]
    bgra[:, :, 3] = 255

    return bgra


def read_chunk(fifo, size):
    """Accumulate a complete audio chunk; never discard partial sample frames."""
    chunks = []
    remaining = size

    while remaining and not stop.is_set():
        data = fifo.read(remaining)
        if not data:
            return None  # Writer disconnected; discard incomplete final chunk.
        chunks.append(data)
        remaining -= len(data)

    return b"".join(chunks) if not stop.is_set() else None


def audio_worker():
    """Consume Spotify PCM, forward it to HDMI, and compute FFT levels."""
    chunk_bytes = HOP * CHANNELS * 2
    buffer = np.zeros(FFT_SIZE, dtype=np.float32)
    global last_audio_activity

    while not stop.is_set():
        player = None
        try:
            print("Waiting for Spotify audio...", flush=True)
            # Blocking until Raspotify opens the FIFO for writing.
            with open(FIFO, "rb", buffering=0) as fifo:
                player = subprocess.Popen(
                    [
                        "aplay", "-q", "-D", AUDIO_DEVICE,
                        "-t", "raw", "-f", "S16_LE",
                        "-r", str(RATE), "-c", str(CHANNELS),
                        "-B", str(ALSA_BUFFER), "-F", str(ALSA_PERIOD),
                    ],
                    stdin=subprocess.PIPE,
                    bufsize=0,
                )
                print("Spotify connected: outputting HDMI audio", flush=True)
                buffer.fill(0)

                while not stop.is_set():
                    pcm = read_chunk(fifo, chunk_bytes)
                    if pcm is None:
                        break

                    # Feed audio first. An ALSA write may block until there is room.
                    player.stdin.write(pcm)

                    samples = np.frombuffer(pcm, dtype="<i2")
                    mono = samples.reshape(-1, CHANNELS).mean(axis=1)
                    mono = mono.astype(np.float32) / 32768.0
                    # Silence alone is not active playback. Update the
                    # timestamp while holding the same lock used by rendering.
                    rms = float(np.sqrt(np.mean(mono * mono)))
                    if rms > IDLE_RMS_THRESHOLD:
                        with lock:
                            last_audio_activity = time.monotonic()

                    n = min(len(mono), FFT_SIZE)
                    buffer[:-n] = buffer[n:]
                    buffer[-n:] = mono[-n:]

                    spectrum = np.abs(np.fft.rfft(buffer * WINDOW))
                    spectrum *= 2.0 / WINDOW.sum()
                    updated = np.empty(BANDS, dtype=np.float32)

                    for i, (a, b) in enumerate(BINS):
                        magnitude = float(np.max(spectrum[a:b]))
                        db = 20.0 * np.log10(max(magnitude, 1e-9))
                        updated[i] = np.clip(
                            (db - DB_MIN) / (DB_MAX - DB_MIN), 0.0, 1.0
                        )

                    with lock:
                        spectrum_history.append((time.monotonic(), updated))

        except (OSError, BrokenPipeError, ValueError) as exc:
            if not stop.is_set():
                print(f"Audio error: {exc}", flush=True)
                time.sleep(1)

        finally:
            if player is not None:
                if player.stdin is not None and not player.stdin.closed:
                    player.stdin.close()
                try:
                    player.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    player.terminate()
                    player.wait()

            with lock:
                spectrum_history.clear()
            buffer.fill(0)


def main():
    if not os.path.exists(FIFO):
        raise FileNotFoundError(f"FIFO does not exist: {FIFO}")

    with Screen(device=DRM_DEVICE, width=WIDTH, height=HEIGHT) as screen:
        w, h = screen.get_screen_size()
        print(f"Resolution: {w}x{h}", flush=True)
        print(f"Visualizer: {BANDS} bands, {SEGMENTS} segments @ {FPS} FPS", flush=True)

        canvas = np.empty((h, w, 4), dtype=np.uint8)
        canvas[:] = BG

        margin = int(w * 0.07)
        gap = max(3, int(w * 0.008))
        bar_w = (w - 2 * margin - (BANDS - 1) * gap) // BANDS
        if bar_w < 1:
            raise ValueError("Too many bands for this display width")

        bottom = int(h * 0.88)
        top = int(h * 0.13)
        area_h = bottom - top
        segment_step = max(2, area_h // SEGMENTS)
        segment_h = max(1, segment_step - 2)
        x_positions = [margin + i * (bar_w + gap) for i in range(BANDS)]

        displayed = np.zeros(BANDS, dtype=np.float32)
        peaks = np.zeros(BANDS, dtype=np.float32)
        peak_hold = np.zeros(BANDS, dtype=np.float32)

        thread = threading.Thread(target=audio_worker, daemon=True)
        thread.start()

        cec_thread = threading.Thread(
            target=monitor_cec, args=(handle_cec_press, stop, CEC_DEVICE), daemon=True,
        )

        next_frame = previous_frame = time.monotonic()
        frames = 0
        fps_start = previous_frame
        idle_visible = False
        idle_last_refresh = 0.0
        delay_text = None
        delay_patch = None

        try:
            cec_thread.start()
            while True:
                start = time.monotonic()
                dt = min(start - previous_frame, 0.1)
                previous_frame = start

                with lock:
                    idle = start - last_audio_activity >= IDLE_TIMEOUT
                    target = delayed_spectrum(start)
                    new_delay_text = visual_delay_info(start)

                delay_info_changed = new_delay_text != delay_text
                if delay_info_changed:
                    delay_text = new_delay_text
                    delay_patch = render_delay_info(delay_text) if delay_text is not None else None

                if idle:
                    refresh_idle = not idle_visible or start - idle_last_refresh >= IDLE_REFRESH
                    if refresh_idle:
                        canvas[:] = draw_idle_screen(w, h)
                        idle_last_refresh = start
                        idle_visible = True
                        print("\nIdle screen active", flush=True)
                    if refresh_idle or delay_info_changed:
                        show_delay_info(canvas, delay_patch, idle=True)
                        screen.show(canvas)
                    # No need to redraw or calculate FFT graphics while idle.
                    time.sleep(0.10)
                    next_frame = previous_frame = time.monotonic()
                    displayed.fill(0)
                    peaks.fill(0)
                    peak_hold.fill(0)
                    continue

                if idle_visible:
                    print("\nPlayback detected; resuming spectrum", flush=True)
                    idle_visible = False
                    canvas[:] = BG
                    next_frame = previous_frame = time.monotonic()
                    dt = 0.0

                displayed = np.maximum(target, displayed - BAR_FALLOFF * dt)

                for i in range(BANDS):
                    if displayed[i] >= peaks[i]:
                        peaks[i] = displayed[i]
                        peak_hold[i] = PEAK_HOLD
                    elif peak_hold[i] > 0:
                        peak_hold[i] -= dt
                    else:
                        peaks[i] = max(
                            displayed[i], peaks[i] - PEAK_FALLOFF * dt
                        )

                # Clear the old label before drawing bars, so even small
                # display modes can draw spectrum beneath the overlay.
                show_delay_info(canvas, None)
                canvas[max(0, top - 5):min(h, bottom + 5), :] = BG

                for i in range(BANDS):
                    x = x_positions[i]
                    x2 = x + bar_w
                    count = min(SEGMENTS, max(0, int(displayed[i] * SEGMENTS)))

                    for j in range(count):
                        y = bottom - (j + 1) * segment_step
                        canvas[y:y + segment_h, x:x2] = PALETTE[j]

                    peak_seg = int(peaks[i] * SEGMENTS)
                    if peak_seg > 0:
                        py = max(top, bottom - (peak_seg + 1) * segment_step)
                        canvas[py:py + 3, x:x2] = PEAK_COLOR

                if delay_patch is not None:
                    # Draw after the bars to keep the label on top.
                    show_delay_info(canvas, delay_patch, clear=False)
                screen.show(canvas)
                frames += 1
                now = time.monotonic()
                if now - fps_start >= 5:
                    print(f"\rFPS: {frames / (now - fps_start):.1f}",
                          end="", flush=True)
                    frames = 0
                    fps_start = now

                next_frame += 1.0 / FPS
                if next_frame < now:
                    next_frame = now
                delay = next_frame - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        except KeyboardInterrupt:
            print("\nStopping...", flush=True)

        finally:
            stop.set()
            # The worker can be blocked waiting for FIFO data; daemon=True
            # ensures Ctrl+C can still terminate the application.
            thread.join(timeout=1)
            if cec_thread.ident is not None:
                cec_thread.join(timeout=3)


if __name__ == "__main__":
    main()
