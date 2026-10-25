#!/usr/bin/env python3
"""Display HDMI-CEC remote button events on Raspberry Pi HDMI.

Requires: cec-ctl (v4l-utils), drm_display, numpy, Pillow.
Run with sudo because cec-ctl --monitor requires root.
Uses a pseudo-terminal to match the working interactive cec-ctl command.

Stop any other DRM/KMS renderer first (e.g. spotify-visualizer).
"""

import queue
import os
import pty
import select
import re
import subprocess
import threading
import time

import numpy as np
from drm_display import Screen
from PIL import Image, ImageDraw, ImageFont

CEC_DEVICE = "/dev/cec0"
DRM_DEVICE = "/dev/dri/card0"
WIDTH, HEIGHT = 1920, 1080

# Example monitor messages can differ between v4l-utils versions.
# Prefer named CEC messages but also recognize raw opcode 0x44 / 0x45.
PRESSED = re.compile(r"(?:USER[ _-]CONTROL[ _-]PRESSED|User Control Pressed)", re.I)
RELEASED = re.compile(r"(?:USER[ _-]CONTROL[ _-]RELEASED|User Control Released)", re.I)
RAW_PRESSED = re.compile(r"\b(?:[0-9a-f]:[0-9a-f]:)?44:([0-9a-f]{2})\b", re.I)
KEY_FIELD = re.compile(r"(?:ui[-_ ]?cmd|ui[-_ ]?command|key|code)\s*[:=]\s*(.+)", re.I)

# HDMI-CEC UI command codes (common keys).
KEY_CODES = {
    0x00: "Select / OK", 0x01: "Up", 0x02: "Down", 0x03: "Left",
    0x04: "Right", 0x09: "Home", 0x0D: "Exit / Back",
    0x20: "0", 0x21: "1", 0x22: "2", 0x23: "3", 0x24: "4",
    0x25: "5", 0x26: "6", 0x27: "7", 0x28: "8", 0x29: "9",
    0x30: "Channel Up", 0x31: "Channel Down", 0x32: "Previous Channel",
    0x41: "Volume Up", 0x42: "Volume Down", 0x43: "Mute",
    0x44: "Play", 0x45: "Stop", 0x46: "Pause", 0x47: "Record",
    0x48: "Rewind", 0x49: "Fast Forward", 0x4A: "Eject",
    0x4B: "Forward", 0x4C: "Backward", 0x53: "Electronic Guide",
    0x60: "Play Function", 0x61: "Pause Play Function",
    0x71: "F1 / Blue", 0x72: "F2 / Red", 0x73: "F3 / Green",
    0x74: "F4 / Yellow",
}


def monitor_cec(events: queue.Queue, stop: threading.Event) -> None:
    # A PTY gives cec-ctl terminal-style output, as in a working SSH session.
    # It also avoids its stdout being buffered when redirected to a pipe.
    command = [
        "cec-ctl", "-d", CEC_DEVICE, "--playback", "--osd-name", "SpotifyVis",
        "--monitor",
    ]
    print("Listening:", " ".join(command), flush=True)
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=slave_fd, stderr=slave_fd,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(master_fd)
        os.close(slave_fd)
        events.put(("error", f"Unable to launch cec-ctl: {exc}"))
        return
    finally:
        # The child has its own copy; close ours.
        try:
            os.close(slave_fd)
        except OSError:
            pass

    buffer = b""
    last_line = ""
    try:
        while not stop.is_set():
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break  # PTY closes with EIO when the child exits.
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    last_line = line
                    print("CEC:", line, flush=True)
                    if RELEASED.search(line):
                        events.put(("released", ""))
                    elif PRESSED.search(line):
                        match = KEY_FIELD.search(line)
                        key = match.group(1).strip() if match else ""
                        if key:
                            # Keep the human-readable value, not trailing raw code.
                            key = re.sub(r"\s*\(0x[0-9a-f]+\).*", "", key, flags=re.I)
                            events.put(("pressed", key))
                        else:
                            raw_match = RAW_PRESSED.search(line)
                            if raw_match:
                                code = int(raw_match.group(1), 16)
                                key = KEY_CODES.get(code, f"CEC code 0x{code:02X}")
                            else:
                                key = "Key event (see terminal)"
                            events.put(("pressed", key))
                    else:
                        raw_match = RAW_PRESSED.search(line)
                        if raw_match:
                            code = int(raw_match.group(1), 16)
                            events.put(("pressed", KEY_CODES.get(code, f"CEC code 0x{code:02X}")))
            if process.poll() is not None:
                break

        if not stop.is_set():
            code = process.poll()
            message = f"cec-ctl exited ({code}): {last_line or 'no diagnostic output'}"
            print(message, flush=True)
            events.put(("error", message))
    finally:
        os.close(master_fd)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def render(width: int, height: int, button: str, detail: str) -> np.ndarray:
    picture = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(picture)
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    try:
        title_font = ImageFont.truetype(path, 18)
        key_font = ImageFont.truetype(path, 38)
        small_font = ImageFont.truetype(path, 14)
    except OSError:
        title_font = key_font = small_font = ImageFont.load_default()

    draw.text((24, 24), "HDMI-CEC REMOTE TEST", font=title_font, fill="white")
    draw.text((24, 96), "Last button:", font=small_font, fill="#AAAAAA")
    draw.text((24, 128), button[:30], font=key_font, fill="#FFFFFF")
    draw.text((24, 300), detail[:70], font=small_font, fill="#AAAAAA")
    draw.text((24, 348), "Press TV remote keys. Ctrl+C to quit.", font=small_font, fill="#777777")

    rgb = np.asarray(picture, dtype=np.uint8)
    bgra = np.empty((height, width, 4), dtype=np.uint8)
    bgra[:, :, :3] = rgb[:, :, ::-1]
    bgra[:, :, 3] = 255
    return bgra


def main() -> None:
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    worker = threading.Thread(target=monitor_cec, args=(events, stop), daemon=True)
    worker.start()

    try:
        with Screen(device=DRM_DEVICE, width=WIDTH, height=HEIGHT) as screen:
            width, height = screen.get_screen_size()
            button, detail = "Waiting...", f"Listening on {CEC_DEVICE}"
            screen.show(render(width, height, button, detail))
            while True:
                try:
                    kind, value = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "pressed":
                    button = value
                    detail = "Pressed at " + time.strftime("%H:%M:%S")
                elif kind == "released":
                    detail = "Released at " + time.strftime("%H:%M:%S")
                else:
                    button, detail = "Monitor error", value
                screen.show(render(width, height, button, detail))
    except KeyboardInterrupt:
        print("\nStopping demo.")
    finally:
        stop.set()
        worker.join(timeout=1)


if __name__ == "__main__":
    main()
