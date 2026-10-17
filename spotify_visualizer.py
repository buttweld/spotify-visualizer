"""Raspotify FIFO -> HDMI audio + Winamp-style DRM spectrum visualization.

Expected Raspotify settings:
    LIBRESPOT_BACKEND="pipe"
    LIBRESPOT_DEVICE="/run/spotify-vis/audio.pcm"
    LIBRESPOT_FORMAT="S16"

Start this program before initiating Spotify Connect playback.
"""

import os
import subprocess
import threading
import time

import numpy as np
from drm_display import Screen

# ---------------- CONFIGURATION ----------------

FIFO = "/run/spotify-vis/audio.pcm"
AUDIO_DEVICE = "plughw:CARD=vc4hdmi,DEV=0"
DRM_DEVICE = "/dev/dri/card0"

# Librespot's raw pipe output is normally 44.1 kHz, S16_LE, stereo.
RATE = 44100
CHANNELS = 2
FPS = 24
HOP = round(RATE / FPS)
FFT_SIZE = 8192

BANDS = 38
SEGMENTS = 21

WIDTH = 720
HEIGHT = 400

BAR_FALLOFF = 1.8
PEAK_HOLD = 0.20
PEAK_FALLOFF = 0.65

DB_MIN = -65.0
DB_MAX = 5.0

ALSA_BUFFER = 40_000    # 40 ms
ALSA_PERIOD = 10_000    # 10 ms

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

levels = np.zeros(BANDS, dtype=np.float32)
lock = threading.Lock()
stop = threading.Event()


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
                        "-B", str(ALSA_BUFFER),
                        "-F", str(ALSA_PERIOD),
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
                        levels[:] = updated

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
                levels.fill(0)
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

        next_frame = previous_frame = time.monotonic()
        frames = 0
        fps_start = previous_frame

        try:
            while True:
                start = time.monotonic()
                dt = min(start - previous_frame, 0.1)
                previous_frame = start

                with lock:
                    target = levels.copy()

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


if __name__ == "__main__":
    main()
