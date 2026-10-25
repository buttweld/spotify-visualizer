"""Screen transitions and slideshow controls, independent of audio and HDMI."""
from dataclasses import dataclass, field
from enum import Enum
import random


class Mode(Enum):
    IDLE = "idle"
    SPECTRUM = "spectrum"
    PHOTOS = "photos"


@dataclass
class DisplayState:
    entered_at: float
    idle_timeout: float = 30.0
    interval: int = 30
    interval_step: int = 5
    mode: Mode = Mode.IDLE
    playback_active: bool = False
    photo_index: int = 0
    photo_count: int = 0
    photo_order: list = field(default_factory=list)
    next_photo_at: float = 0.0
    interval_info_until: float = 0.0

    def switch(self, mode, now):
        self.mode = mode
        self.entered_at = now
        self.next_photo_at = now + self.interval

    def cycle(self, now):
        modes = list(Mode)
        self.switch(modes[(modes.index(self.mode) + 1) % len(modes)], now)

    def tick(self, now, last_audio_activity, active_window=0.5):
        active = now - last_audio_activity < active_window
        started = active and not self.playback_active
        self.playback_active = active
        if self.mode == Mode.IDLE:
            if started:
                self.switch(Mode.SPECTRUM, now)
            elif now - self.entered_at >= self.idle_timeout:
                self.switch(Mode.PHOTOS, now)
        elif self.mode == Mode.SPECTRUM:
            if now - max(last_audio_activity, self.entered_at) >= self.idle_timeout:
                self.switch(Mode.PHOTOS, now)

    def set_photos(self, count, now):
        self.photo_count = count
        self.photo_index = 0
        self.shuffle_photos()
        self.next_photo_at = now + self.interval

    def shuffle_photos(self, previous=None):
        self.photo_order = list(range(self.photo_count))
        random.shuffle(self.photo_order)
        # Avoid showing the same photo twice at the boundary between cycles.
        if self.photo_count > 1 and self.photo_order[0] == previous:
            other = random.randrange(1, self.photo_count)
            self.photo_order[0], self.photo_order[other] = self.photo_order[other], self.photo_order[0]

    def step_photo(self, step):
        if not self.photo_count:
            return
        previous = self.photo_order[self.photo_index]
        self.photo_index += step
        if self.photo_index >= self.photo_count:
            self.shuffle_photos(previous)
        self.photo_index %= self.photo_count

    def ordered_photos(self, paths):
        return tuple(paths[index] for index in self.photo_order)

    def photo_button(self, button, now):
        if button in ("up", "down"):
            step = self.interval_step if button == "up" else -self.interval_step
            self.interval = max(self.interval_step, self.interval + step)
            self.interval_info_until = now + 2.0
        elif button in ("left", "right") and self.photo_count:
            step = 1 if button == "right" else -1
            self.step_photo(step)
        else:
            return
        self.next_photo_at = now + self.interval

    def interval_info(self, now):
        if now < self.interval_info_until:
            return f"Interval: {self.interval} s"
        return None

    def advance_photo(self, now):
        if self.photo_count and now >= self.next_photo_at:
            self.step_photo(1)
            self.next_photo_at = now + self.interval
            return True
        return False
