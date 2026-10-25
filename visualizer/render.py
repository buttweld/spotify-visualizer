"""Spectrum rendering and scaling with one stable HDMI output mode."""
import numpy as np
import time
from . import config as c
from .kms import Plane


class Output:
    """Upload native-sized frames; the KMS overlay handles display scaling."""
    def __init__(self, screen, sizes=None):
        self.screen = screen
        self.size = tuple(screen.get_screen_size())
        self.plane = Plane(screen, sizes or (c.IDLE_SIZE, c.SPECTRUM_SIZE, c.PHOTOS_SIZE))
        self._source = None
        self._frames = 0
        self._present_seconds = 0.0
        self._stats_start = 0.0
        try:
            # Clear the legacy primary plane once, before audio starts. It stays
            # black underneath the scaled overlay and supplies letterbox borders.
            screen.show(np.zeros((self.size[1], self.size[0], 4), dtype=np.uint8))
        except Exception:
            self.plane.close()
            raise

    def show(self, frame):
        started = time.monotonic()
        self.plane.show(frame)
        finished = time.monotonic()
        source = (frame.shape[1], frame.shape[0])
        if source != self._source:
            self._source = source
            self._stats_start = started
            self._frames = 0
            self._present_seconds = 0.0
        self._frames += 1
        self._present_seconds += finished - started
        elapsed = finished - self._stats_start
        if source == c.SPECTRUM_SIZE and elapsed >= 5:
            fps = self._frames / elapsed
            print(f"KMS spectrum: {fps:.1f} FPS, {frame.nbytes * fps / 1e6:.1f} MB/s source data, "
                  f"{1000 * self._present_seconds / self._frames:.1f} ms mean present "
                  f"({source[0]}x{source[1]} -> {self.size[0]}x{self.size[1]})", flush=True)
            self._stats_start = finished
            self._frames = 0
            self._present_seconds = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.plane.close()


class Spectrum:
    def __init__(self, size):
        self.w, self.h = size
        self.canvas = np.empty((self.h, self.w, 4), dtype=np.uint8)
        self.canvas[:] = c.BG
        self.margin = int(self.w * .07)
        self.gap = max(3, int(self.w * .008))
        self.bar_width = (self.w - 2 * self.margin - (c.BANDS - 1) * self.gap) // c.BANDS
        if self.bar_width < 1:
            raise ValueError("Too many bands for the spectrum resolution")
        self.bottom = int(self.h * .88)
        self.top = int(self.h * .13)
        self.segment_step = max(2, (self.bottom - self.top) // c.SEGMENTS)
        self.segment_height = max(1, self.segment_step - 2)
        self.displayed = np.zeros(c.BANDS, dtype=np.float32)
        self.peaks = np.zeros(c.BANDS, dtype=np.float32)
        self.peak_hold = np.zeros(c.BANDS, dtype=np.float32)

    def render(self, target, dt):
        self.canvas[:] = c.BG
        self.displayed = np.maximum(target, self.displayed - c.BAR_FALLOFF * dt)
        for i in range(c.BANDS):
            if self.displayed[i] >= self.peaks[i]:
                self.peaks[i] = self.displayed[i]
                self.peak_hold[i] = c.PEAK_HOLD
            elif self.peak_hold[i] > 0:
                self.peak_hold[i] -= dt
            else:
                self.peaks[i] = max(self.displayed[i], self.peaks[i] - c.PEAK_FALLOFF * dt)
            x = self.margin + i * (self.bar_width + self.gap)
            count = min(c.SEGMENTS, max(0, int(self.displayed[i] * c.SEGMENTS)))
            for j in range(count):
                y = self.bottom - (j + 1) * self.segment_step
                self.canvas[y:y + self.segment_height, x:x + self.bar_width] = c.PALETTE[j]
            peak = int(self.peaks[i] * c.SEGMENTS)
            if peak > 0:
                y = max(self.top, self.bottom - (peak + 1) * self.segment_step)
                self.canvas[y:y + 3, x:x + self.bar_width] = c.PEAK_COLOR
        return self.canvas
