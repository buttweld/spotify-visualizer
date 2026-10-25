import importlib.util
from pathlib import Path
import queue
import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from visualizer.render import Output, Spectrum
from visualizer.photos.render import instruction_frame

with patch.dict(sys.modules, {"drm_display": types.SimpleNamespace(Screen=None)}):
    spec = importlib.util.spec_from_file_location(
        "runtime_under_test", Path(__file__).resolve().parents[1] / "spotify_visualizer.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class RenderingTests(unittest.TestCase):
    def test_each_logical_resolution_and_fixed_hdmi_output(self):
        spectrum = Spectrum((480, 270))
        frame = spectrum.render(np.ones(38, dtype=np.float32), .04)
        self.assertEqual(frame.shape, (270, 480, 4))
        screen = Mock()
        screen.get_screen_size.return_value = (1920, 1080)
        with patch("visualizer.render.Plane") as plane_class:
            with Output(screen, [(480, 270), (1280, 720)]) as output:
                output.show(frame)
                output.show(instruction_frame("Scan to choose your photos", [], size=(1280, 720)))
            plane_class.assert_called_once_with(screen, [(480, 270), (1280, 720)])
            uploads = plane_class.return_value.show.call_args_list
            self.assertIs(uploads[0].args[0], frame)
            self.assertEqual(uploads[0].args[0].shape, (270, 480, 4))
            self.assertEqual(uploads[1].args[0].shape, (720, 1280, 4))
            plane_class.return_value.close.assert_called_once()
        # Only the initial black underlay is copied at the HDMI resolution.
        screen.show.assert_called_once()
        self.assertEqual(screen.show.call_args.args[0].shape, (1080, 1920, 4))
        self.assertFalse(screen.show.call_args.args[0].any())
        screen.close.assert_not_called()

    def test_small_interval_overlay_is_clipped(self):
        frame = np.zeros((10, 10, 4), dtype=np.uint8)
        app.overlay_photo_info(frame, app.render_delay_info("Interval: 30 s", 22))
        self.assertEqual(frame.shape, (10, 10, 4))


class RuntimeTests(unittest.TestCase):
    def test_screen_switches_and_remote_actions_keep_one_audio_thread_and_drm_session(self):
        now = [100.]
        stopped = [False]
        waits = [0]
        shutdown = Mock()
        shutdown.is_set.side_effect = lambda: stopped[0]
        shutdown.set.side_effect = lambda: stopped.__setitem__(0, True)
        events = queue.SimpleQueue()
        library = Mock()
        library.events = queue.Queue()
        library.generation = 2
        library.events.put(("photos", (Path("0.photo"), Path("1.photo"))))
        frames = Mock()
        frames.get.return_value = np.zeros((180, 320, 4), dtype=np.uint8)
        audio, cec, network = Mock(), Mock(), Mock()
        for thread in (audio, cec, network):
            thread.ident = 1
        screen_class = MagicMock()
        screen_class.return_value.__enter__.return_value.get_screen_size.return_value = (1920, 1080)

        def wait(seconds):
            waits[0] += 1
            now[0] += .1
            if waits[0] == 1:
                app.last_audio_activity = now[0]  # first sound -> spectrum
            elif waits[0] == 2:
                events.put("okay")  # spectrum -> photos
            elif waits[0] == 3:
                events.put("up")
                events.put("right")
            elif waits[0] == 4:
                now[0] += 2  # label expires without a photo change
            elif waits[0] == 5:
                events.put("okay")  # photos -> idle QR
                library.events.put(("menu", (1, ("Cancelled", ["old session"], None))))
            elif waits[0] == 6:
                events.put("down")  # adjust spectrum delay while idle
            elif waits[0] == 7:
                now[0] += 31  # idle -> photos
            else:
                stopped[0] = True

        shutdown.wait.side_effect = wait
        with patch.object(app, "stop", shutdown), \
                patch.object(app, "remote_events", events), \
                patch.object(app, "last_audio_activity", 0), \
                patch.object(app, "Screen", screen_class), \
                patch.object(app, "Output") as output_class, \
                patch.object(app, "PhotoLibrary", return_value=library), \
                patch.object(app, "PhotoFrames", return_value=frames), \
                patch.object(app.threading, "Thread", side_effect=[audio, cec, network]), \
                patch.object(app.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(app.os.path, "exists", return_value=True), \
                patch.object(app, "PHOTOS_SIZE", (320, 180)), \
                patch.object(app, "render_delay_info", wraps=app.render_delay_info) as labels, \
                patch.object(app, "draw_idle_screen", wraps=app.draw_idle_screen) as idle_menus:
            output_class.return_value.__enter__.return_value.size = (1920, 1080)
            app.main()
            label_texts = [call.args[0] for call in labels.call_args_list]
            self.assertIn("Interval: 35 s", label_texts)
            self.assertNotIn("Interval: 30 s", label_texts)
            rendered_photos = [call.args[0] for call in output_class.return_value.__enter__.return_value.show.call_args_list
                               if call.args[0].shape == (180, 320, 4)]
            self.assertFalse(rendered_photos[0].any())
            self.assertTrue(rendered_photos[1].any())
            self.assertFalse(rendered_photos[2].any())
            self.assertTrue(idle_menus.called)
            for call in idle_menus.call_args_list:
                self.assertEqual(call.kwargs.get("menu", app.QR_PENDING_MENU)[0],
                                 "Scan to choose your photos")
        screen_class.assert_called_once()
        audio.start.assert_called_once()
        cec.start.assert_called_once()
        self.assertEqual(library.select.call_count, 2)
        library.cancel_selection.assert_not_called()
        library.close.assert_called_once()
        frames.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
