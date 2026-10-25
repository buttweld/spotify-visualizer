"""Press-only HDMI-CEC input using the demo's cec-ctl/PTY transport."""

import os
import pty
import re
import select
import subprocess
import time


PRESSED = re.compile(r"USER[ _-]CONTROL[ _-]PRESSED", re.I)
RELEASED = re.compile(r"USER[ _-]CONTROL[ _-]RELEASED", re.I)
RAW_PRESSED = re.compile(
    r"(?<![\w:])(?:(?:[0-9a-f]{2}|[0-9a-f]:[0-9a-f]):)?44:([0-9a-f]{2})(?![\w:])", re.I,
)
RAW_RELEASED = re.compile(r"(?<![\w:])(?:[0-9a-f]{2}|[0-9a-f]:[0-9a-f]):45(?![\w:])", re.I)
KEY_FIELD = re.compile(r"\b(?:ui[-_ ]?(?:cmd|command)|key|code)\s*[:=]\s*(.+)", re.I)
KEY_CODES = {0x00: "okay", 0x01: "up", 0x02: "down", 0x03: "left", 0x04: "right"}


class PressDetector:
    """Parse named/multiline or raw events and discard held-key repeats.

    Releases only reset internal state. If a release is lost, a gap of at
    least 550 ms between pressed messages allows the next press through.
    Every repeat refreshes that timer, so a continuous hold emits once.
    """

    def __init__(self, release_timeout=0.55):
        self.release_timeout = release_timeout
        self.held_code = None
        self.last_press = float("-inf")
        self.pending = False

    def feed(self, line, now=None):
        now = time.monotonic() if now is None else now
        if RELEASED.search(line) or RAW_RELEASED.search(line):
            self.held_code = None
            self.pending = False
            return None

        raw = RAW_PRESSED.search(line)
        if PRESSED.search(line) or raw:
            self.pending = True

        if not self.pending:
            return None

        code = None
        field = KEY_FIELD.search(line)
        if raw:
            code = int(raw.group(1), 16)
        elif field:
            value = field.group(1).strip().lower()
            number = re.search(r"\b0x([0-9a-f]+)\b", value)
            if number:
                code = int(number.group(1), 16)
            elif value.isdecimal():
                code = int(value)
            else:
                names = {name: key for key, name in KEY_CODES.items()}
                names.update({"select": 0, "ok": 0, "select / ok": 0})
                code = names.get(value)
        else:
            return None  # cec-ctl may print ui-cmd on the following line.

        self.pending = False
        if code is None:
            return None
        repeated = code == self.held_code and now - self.last_press < self.release_timeout
        self.held_code = code
        self.last_press = now
        return None if repeated else KEY_CODES.get(code)


def monitor_cec(on_press, stop, device="/dev/cec0"):
    """Call on_press(button) for new presses until stop is set.

    Run in a worker thread; callbacks should finish quickly. Missing tools,
    devices, or monitor permissions disable CEC without stopping playback.
    """
    master_fd = slave_fd = None
    process = None
    try:
        master_fd, slave_fd = pty.openpty()
        # Monitoring alone does not claim a logical address. After a reboot
        # an unconfigured adapter can listen but the TV has no device to
        # address remote-control messages to. Register before monitoring.
        command = [
            "cec-ctl", "-d", device, "--playback", "--osd-name", "SpotifyVis",
            "--monitor",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None
        print(f"CEC: registering SpotifyVis as a playback device on {device}", flush=True)
        detector = PressDetector()
        buffer = b""
        last_line = "no diagnostic output"
        while not stop.is_set():
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break  # A closed PTY reports EIO on Linux.
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        last_line = line
                    if re.match(r"(?:Physical Address|Logical Address Mask|Logical Addresses|OSD Name)\s*:", line):
                        print(f"CEC: {line}", flush=True)
                    if re.match(r"Logical Address Mask\s*:\s*0x0+\b", line):
                        print(
                            "CEC warning: no logical address assigned; check the HDMI "
                            "connection and TV CEC settings.", flush=True,
                        )
                    button = detector.feed(line)
                    if button is not None and not stop.is_set():
                        on_press(button)
            elif process.poll() is not None:
                break
        if not stop.is_set():
            print(f"CEC disabled: cec-ctl stopped: {last_line}", flush=True)
    except OSError as exc:
        print(f"CEC disabled: {exc}", flush=True)
    finally:
        for fd in (slave_fd, master_fd):
            if fd is not None:
                os.close(fd)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
