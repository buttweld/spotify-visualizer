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
import queue
import socket
import subprocess
import signal
import threading
import textwrap
import time
import numpy as np
from drm_display import Screen
from PIL import Image, ImageDraw, ImageFont

from hdmi_cec import monitor_cec
from visualizer.state import DisplayState, Mode
from visualizer.render import Output, Spectrum
from visualizer.photos.controller import PhotoLibrary, PhotoFrames
from visualizer.photos.render import instruction_frame

from visualizer.config import *

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


remote_events = queue.SimpleQueue()


def handle_cec_press(button):
    """Keep CEC input responsive while the display handles queued actions."""
    remote_events.put(button)


def adjust_visual_delay(button):
    """Adjust spectrum timing once per Up/Down press outside the photo screen."""
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


def render_delay_info(text, font_size=12):
    """Render a small BGRA label, cached by the display loop until it changes."""
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    font = (ImageFont.truetype(font_path, font_size)
            if os.path.isfile(font_path) else ImageFont.load_default())
    width = font.getbbox(text)[2] + 1
    image = Image.new("RGB", (width, max(24, font_size * 2)), "black")
    ImageDraw.Draw(image).text((0, 0), text, font=font, fill="white")
    rgb = np.asarray(image, dtype=np.uint8)
    patch = np.empty((image.height, width, 4), dtype=np.uint8)
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


QR_PENDING_MENU = ("Scan to choose your photos", [
    "Preparing your QR code…", "Selected photos stay on this Pi until deleted.",
    "No selection? Your saved photos will continue.",
    "",
], None)


def draw_idle_screen(width, height, menu=QR_PENDING_MENU, connection_info=(), delay_text=None):
    """Idle is the photo-selection QR menu, including casting and SSH details."""
    title, lines, qr = menu
    wrapped = [part for line in lines for part in (textwrap.wrap(line, 62) or [""])]
    canvas = instruction_frame(title, (wrapped[:4] + list(connection_info))[:8], qr,
        size=(width, height), subtitle="SPOTIFY RECEIVER  /  SPECTRUM VISUALIZER /  GOOGLE PHOTOS SLIDE SHOW",
        footer="OK: next screen")
    if delay_text:
        overlay_photo_info(canvas, render_delay_info(delay_text))
    return canvas


def network_worker(updates):
    while not stop.is_set():
        user = os.environ.get("SUDO_USER") or getpass.getuser()
        ip = local_ipv4()
        updates.put([f"Spotify Connect: {DEVICE_NAME}",
                     f"SSH: {user}@{ip}", f"Hostname: {socket.gethostname()}"])
        stop.wait(IDLE_REFRESH)


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

    stop.clear()
    library = PhotoLibrary(PHOTO_CACHE, PHOTO_TOKEN, PHOTOS_SIZE, stop)
    photo_frames = PhotoFrames(PHOTOS_SIZE)
    audio_thread = threading.Thread(target=audio_worker, daemon=True, name="audio")
    cec_thread = threading.Thread(
        target=monitor_cec, args=(handle_cec_press, stop, CEC_DEVICE), daemon=True,
    )
    info_updates = queue.SimpleQueue()
    info_thread = threading.Thread(target=network_worker, args=(info_updates,), daemon=True)
    try:
        # Keep this DRM object alive for every screen. Reopening it changes HDMI
        # clocks on some TVs, which can interrupt the independent ALSA stream.
        with Screen(device=DRM_DEVICE, width=HDMI_SIZE[0], height=HDMI_SIZE[1]) as screen, Output(screen) as output:
            print(f"HDMI: {output.size}; idle {IDLE_SIZE}; spectrum {SPECTRUM_SIZE}; photos {PHOTOS_SIZE}", flush=True)
            state = DisplayState(time.monotonic(), IDLE_TIMEOUT, PHOTO_INTERVAL, PHOTO_INTERVAL_STEP)
            spectrum = Spectrum(SPECTRUM_SIZE)
            connection_info = [f"Spotify Connect: {DEVICE_NAME}", "SSH: detecting network…"]
            output.show(draw_idle_screen(*IDLE_SIZE, connection_info=connection_info))
            audio_thread.start()
            cec_thread.start()
            info_thread.start()
            library.start()
            library.select()

            paths = ()
            menu = QR_PENDING_MENU
            menu_revision = 0
            photos_revision = 0
            last_key = None
            previous_frame = time.monotonic()
            visible_mode = Mode.IDLE
            while not stop.is_set():
                now = time.monotonic()
                dt = min(now - previous_frame, .1)
                previous_frame = now
                with lock:
                    activity = last_audio_activity
                    target = delayed_spectrum(now)
                    delay_text = visual_delay_info(now)
                state.tick(now, activity, PLAYBACK_ACTIVE_WINDOW)
                while True:
                    try:
                        button = remote_events.get_nowait()
                    except queue.Empty:
                        break
                    if button == "okay":
                        state.cycle(now)
                    elif state.mode == Mode.PHOTOS:
                        state.photo_button(button, now)
                    else:
                        adjust_visual_delay(button)
                if state.mode != visible_mode:
                    visible_mode = state.mode
                    last_key = None
                    if state.mode == Mode.IDLE:
                        # Never reuse an error from a completed selection while
                        # requesting a new QR code. Active sessions can be reused.
                        menu = QR_PENDING_MENU
                        menu_revision += 1
                        library.select()
                    print(f"Screen: {state.mode.value}", flush=True)
                while True:
                    try:
                        connection_info = info_updates.get_nowait()
                        menu_revision += 1
                    except queue.Empty:
                        break
                while True:
                    try:
                        kind, value = library.events.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "photos":
                        paths = value
                        photos_revision += 1
                        state.set_photos(len(paths), now)
                    elif kind == "menu":
                        generation, updated_menu = value
                        if generation == library.generation:
                            menu = updated_menu
                            menu_revision += 1
                    elif kind == "selected" and paths and state.mode == Mode.IDLE:
                        state.switch(Mode.PHOTOS, now)
                if state.mode == Mode.IDLE:
                    key = (state.mode, menu_revision, delay_text)
                    if key != last_key:
                        output.show(draw_idle_screen(*IDLE_SIZE, menu=menu,
                            connection_info=connection_info, delay_text=delay_text))
                        last_key = key
                    if paths:
                        photo_frames.get(state.ordered_photos(paths), state.photo_index)
                elif state.mode == Mode.SPECTRUM:
                    canvas = spectrum.render(target, dt)
                    if delay_text:
                        show_delay_info(canvas, render_delay_info(delay_text), clear=False)
                    output.show(canvas)
                elif not paths:
                    key = (state.mode, "empty")
                    if key != last_key:
                        output.show(instruction_frame("No saved photos", [
                            "Press OK to open the QR screen and choose photos.",
                        ], size=PHOTOS_SIZE, subtitle="SPOTIFY RECEIVER  /  SPECTRUM VISUALIZER /  GOOGLE PHOTOS SLIDE SHOW",
                           footer="OK: QR screen"))
                        last_key = key
                else:
                    state.advance_photo(now)
                    ordered_paths = state.ordered_photos(paths)
                    frame = photo_frames.get(ordered_paths, state.photo_index)
                    interval_text = state.interval_info(now)
                    key = (state.mode, photos_revision, state.photo_index, id(frame), interval_text)
                    if frame is not None and key != last_key:
                        canvas = frame.copy()
                        if interval_text:
                            overlay_photo_info(canvas, render_delay_info(interval_text, 22))
                        output.show(canvas)
                        last_key = key
                    elif frame is None and ordered_paths[state.photo_index] in photo_frames.failed:
                        state.step_photo(1)
                        if len(photo_frames.failed.intersection(paths)) == len(paths):
                            paths = ()
                            state.set_photos(0, now)
                # The display never sleeps inside audio, photo download or decode work.
                stop.wait(max(0, (1 / FPS if state.mode == Mode.SPECTRUM else .05) - (time.monotonic() - now)))
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        stop.set()
        library.close()
        photo_frames.close()
        for thread in (audio_thread, cec_thread, info_thread):
            if thread.ident is not None:
                thread.join(timeout=1)


def overlay_photo_info(canvas, patch):
    """Place a readable label on a copy, preserving the cached original frame."""
    h, w = canvas.shape[:2]
    x = min(24, max(0, w - 1))
    y = min(24, max(0, h - 1))
    bottom, right = min(h, y + patch.shape[0]), min(w, x + patch.shape[1])
    canvas[y:bottom, x:right] = patch[:bottom - y, :right - x]


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    main()
