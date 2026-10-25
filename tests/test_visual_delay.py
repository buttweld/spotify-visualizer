import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from hdmi_cec import PressDetector


# The host has no DRM device or drm_display package. Stub only that hardware
# import; load the real module and use its actual numpy arrays and lock.
with patch.dict(sys.modules, {"drm_display": types.SimpleNamespace(Screen=None)}):
    spec = importlib.util.spec_from_file_location(
        "visualizer_under_test", Path(__file__).resolve().parents[1] / "spotify_visualizer.py",
    )
    visualizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(visualizer)


class VisualDelayTests(unittest.TestCase):
    def setUp(self):
        visualizer.visual_delay = visualizer.VISUAL_DELAY
        visualizer.visual_delay_info_until = 0.0
        visualizer.spectrum_history.clear()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_adjustments_bounds_and_unassigned_buttons(self):
        visualizer.adjust_visual_delay("up")
        self.assertAlmostEqual(visualizer.visual_delay, visualizer.VISUAL_DELAY + visualizer.VISUAL_DELAY_STEP)
        visualizer.adjust_visual_delay("down")
        self.assertEqual(visualizer.visual_delay, visualizer.VISUAL_DELAY)
        for button in ("okay", "left", "right"):
            visualizer.adjust_visual_delay(button)
            self.assertEqual(visualizer.visual_delay, visualizer.VISUAL_DELAY)
        for _ in range(100):
            visualizer.adjust_visual_delay("down")
        self.assertEqual(visualizer.visual_delay, 0)
        for _ in range(100):
            visualizer.adjust_visual_delay("up")
        self.assertEqual(visualizer.visual_delay, 2)

    def test_delay_increase_can_select_retained_older_frame(self):
        history = visualizer.spectrum_history
        visualizer.visual_delay = 0.4
        history.extend([(9.0, "old"), (9.6, "middle"), (10.0, "new")])
        self.assertEqual(visualizer.delayed_spectrum(10), "middle")
        visualizer.visual_delay = 1.0
        self.assertEqual(visualizer.delayed_spectrum(10), "old")
        self.assertEqual(len(history), 3)
        visualizer.visual_delay = 0
        self.assertEqual(visualizer.delayed_spectrum(10), "new")

    def test_remote_hold_adjusts_once_then_release_allows_another_step(self):
        detector = PressDetector()
        for line, now in [("10:44:01", 0), ("10:44:01", 0.1),
                          ("10:44:01", 0.2), ("10:45", 0.3), ("10:44:01", 0.4)]:
            button = detector.feed(line, now)
            if button is not None:
                visualizer.adjust_visual_delay(button)
        self.assertAlmostEqual(visualizer.visual_delay, visualizer.VISUAL_DELAY + 2 * visualizer.VISUAL_DELAY_STEP)

    def test_delay_label_expires_two_seconds_after_last_press(self):
        self.assertIsNone(visualizer.visual_delay_info(0))
        visualizer.visual_delay = 0.2
        with patch.object(visualizer.time, "monotonic", return_value=10):
            visualizer.adjust_visual_delay("up")
        self.assertEqual(visualizer.visual_delay_info(10), "Delay: +220 ms")
        self.assertEqual(visualizer.visual_delay_info(11.99), "Delay: +220 ms")
        self.assertIsNone(visualizer.visual_delay_info(12))
        with patch.object(visualizer.time, "monotonic", return_value=11):
            visualizer.adjust_visual_delay("down")
        self.assertEqual(visualizer.visual_delay_info(12.99), "Delay: +200 ms")
        self.assertIsNone(visualizer.visual_delay_info(13))

    def test_ignored_buttons_do_not_show_label_but_limit_press_does(self):
        visualizer.adjust_visual_delay("okay")
        self.assertEqual(visualizer.visual_delay_info_until, 0)
        visualizer.visual_delay = 0
        with patch.object(visualizer.time, "monotonic", return_value=5):
            visualizer.adjust_visual_delay("down")
        self.assertEqual(visualizer.visual_delay_info(5), "Delay: +0 ms")

    def test_label_positions_and_erasure_preserve_other_content(self):
        patch_image = visualizer.render_delay_info("Delay: +220 ms")
        for idle, y in [(False, 24), (True, 144)]:
            with self.subTest(idle=idle):
                canvas = np.empty((400, 720, 4), dtype=np.uint8)
                canvas[:] = visualizer.BG
                canvas[0, 0] = (1, 2, 3, 255)
                visualizer.show_delay_info(canvas, patch_image, idle=idle)
                self.assertTrue(canvas[y:y + 24, 24:, :3].any())
                self.assertFalse(canvas[:y, 24:, :3].any())
                visualizer.show_delay_info(canvas, None, idle=idle)
                self.assertFalse(canvas[y:y + 24, 24:, :3].any())
                self.assertEqual(tuple(canvas[0, 0]), (1, 2, 3, 255))

    def test_label_clips_to_small_display(self):
        patch_image = visualizer.render_delay_info("Delay: +2000 ms")
        for shape in [(10, 10, 4), (30, 30, 4), (150, 40, 4)]:
            canvas = np.zeros(shape, dtype=np.uint8)
            visualizer.show_delay_info(canvas, patch_image)
            visualizer.show_delay_info(canvas, patch_image, idle=True)

    def test_startup_and_maximum_delay_history(self):
        self.assertIs(visualizer.delayed_spectrum(0), visualizer.zero_levels)
        visualizer.spectrum_history.append((1, "future"))
        self.assertIs(visualizer.delayed_spectrum(1), visualizer.zero_levels)
        history = visualizer.spectrum_history
        history.clear()
        for index in range(4 * visualizer.FPS):
            history.append((index / visualizer.FPS, index))
        visualizer.visual_delay = visualizer.VISUAL_DELAY_MAX
        now = (4 * visualizer.FPS - 1) / visualizer.FPS
        self.assertIsNot(visualizer.delayed_spectrum(now), visualizer.zero_levels)
        self.assertEqual(len(history), history.maxlen)


if __name__ == "__main__":
    unittest.main()
