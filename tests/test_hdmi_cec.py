import contextlib
import io
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from hdmi_cec import PressDetector, monitor_cec


class PressDetectorTests(unittest.TestCase):
    def test_named_and_multiline_messages(self):
        detector = PressDetector()
        self.assertIsNone(detector.feed("USER_CONTROL_PRESSED (0x44):", 0))
        self.assertEqual(detector.feed("    ui-cmd: up (0x01)", 0), "up")
        self.assertIsNone(detector.feed("USER_CONTROL_RELEASED (0x45)", 0.1))
        self.assertEqual(detector.feed("User Control Pressed: key=Down", 0.2), "down")

    def test_hold_emits_once_and_release_allows_quick_second_press(self):
        detector = PressDetector()
        line = "10:44:01"
        self.assertEqual(detector.feed(line, 0), "up")
        for index in range(1, 31):
            self.assertIsNone(detector.feed(line, index * 0.1))
        self.assertIsNone(detector.feed("10:45", 3.01))
        self.assertEqual(detector.feed(line, 3.02), "up")

    def test_lost_release_recovers_after_gap(self):
        detector = PressDetector()
        self.assertEqual(detector.feed("44:02", 0), "down")
        self.assertIsNone(detector.feed("44:02", 0.3))
        self.assertEqual(detector.feed("44:02", 0.9), "down")

    def test_unrelated_traffic_and_unsupported_keys(self):
        detector = PressDetector()
        self.assertIsNone(detector.feed("10:84:10:00:04", 0))
        self.assertIsNone(detector.feed("ui-cmd: up", 0))
        self.assertIsNone(detector.feed("10:44:41", 0))
        self.assertEqual(detector.feed("10:44:02", 0.1), "down")
        self.assertEqual(detector.feed("10:44:01", 0.2), "up")

    def test_raw_and_decoded_copy_do_not_emit_twice(self):
        detector = PressDetector()
        self.assertEqual(detector.feed("10:44:01", 0), "up")
        self.assertIsNone(detector.feed("User Control Pressed: ui-cmd: up (0x01)", 0.01))

    def test_split_raw_address(self):
        detector = PressDetector()
        self.assertEqual(detector.feed("1:0:44:01", 0), "up")
        self.assertIsNone(detector.feed("1:0:45", 0.1))
        self.assertEqual(detector.feed("1:0:44:01", 0.2), "up")


class MonitorTests(unittest.TestCase):
    def test_missing_executable_is_nonfatal(self):
        with patch("hdmi_cec.subprocess.Popen", side_effect=FileNotFoundError("cec-ctl")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                monitor_cec(self.fail, threading.Event())
        self.assertIn("CEC disabled:", output.getvalue())

    def test_pty_delivers_press_and_child_stops_on_shutdown(self):
        real_popen = subprocess.Popen
        children = []
        # Simulate cec-ctl writing partial lines and then waiting for shutdown.
        script = (
            "import os, time\n"
            "os.write(1, b'Physical Address : 1.0.0.0\\nLogical Address Mask : 0x0010\\n')\n"
            "os.write(1, b'USER_CONTROL_PRESSED (0x44):\\nui-c')\n"
            "time.sleep(0.03)\n"
            "os.write(1, b'md: up (0x01)\\n')\n"
            "time.sleep(60)\n"
        )

        def launch(command, **kwargs):
            self.assertEqual(command, [
                "cec-ctl", "-d", "/dev/cec0", "--playback", "--osd-name", "SpotifyVis",
                "--monitor",
            ])
            child = real_popen([sys.executable, "-c", script], **kwargs)
            children.append(child)
            return child

        stop = threading.Event()
        buttons = []

        def pressed(button):
            buttons.append(button)
            stop.set()

        thread = threading.Thread(target=monitor_cec, args=(pressed, stop), daemon=True)
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch("hdmi_cec.subprocess.Popen", side_effect=launch):
                thread.start()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(buttons, ["up"])
            self.assertIsNotNone(children[0].poll())
            self.assertIn("CEC: registering SpotifyVis", output.getvalue())
            self.assertIn("CEC: Logical Address Mask : 0x0010", output.getvalue())
        finally:
            stop.set()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
