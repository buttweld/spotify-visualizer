from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

from visualizer.photos import controller
from visualizer.photos.api import DemoError
from visualizer.photos.cache import cached_photos, remember_selection


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        previous = self.cache / "google-photos-previous"
        previous.mkdir()
        self.photo = previous / "0.photo"
        Image.new("RGB", (10, 10), "red").save(self.photo, format="PNG")
        remember_selection(self.cache, previous)
        self.token = self.cache / "token.json"
        self.token.touch()
        self.library = controller.PhotoLibrary(self.cache, self.token, (1280, 720), threading.Event())
        self.transport = MagicMock()
        self.picker = Mock()
        self.picker.create.return_value = {"id": "session", "pickerUri": "https://photos.google.com/picker/example"}
        self.picker.items.return_value = []
        modules = {"google.auth.transport.requests": types.SimpleNamespace(
            AuthorizedSession=Mock(return_value=self.transport))}
        self.patches = [patch.dict(sys.modules, modules),
                        patch.object(controller, "load_credentials", return_value=Mock()),
                        patch.object(controller, "save_credentials"),
                        patch.object(controller, "PhotosPicker", return_value=self.picker)]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_selection_keeps_old_cache_and_cleans_session(self):
        with patch.object(controller, "wait_for_selection"):
            with self.assertRaisesRegex(DemoError, "No displayable photos"):
                self.library._select_once()
        self.assertEqual(cached_photos(self.cache), [self.photo])
        self.assertEqual([p for p in self.cache.glob("google-photos-*") if p.is_dir()], [self.photo.parent])
        self.picker.delete.assert_called_once_with("session")

    def test_timeout_does_not_touch_previous_selection(self):
        with patch.object(controller, "wait_for_selection", side_effect=DemoError("timed out")):
            with self.assertRaisesRegex(DemoError, "timed out"):
                self.library._select_once()
        self.assertEqual(cached_photos(self.cache), [self.photo])
        self.picker.delete.assert_called_once_with("session")

    def test_success_replaces_marker_only_after_all_downloads_and_keeps_previous_bytes(self):
        self.picker.items.return_value = [{"id": "new"}]

        def download(item, path):
            self.assertEqual(cached_photos(self.cache), [self.photo])
            Image.new("RGB", (10, 10), "blue").save(path, format="PNG")

        self.picker.download.side_effect = download
        with patch.object(controller, "wait_for_selection"):
            self.library._select_once()
        latest = cached_photos(self.cache)
        self.assertNotEqual(latest, [self.photo])
        self.assertTrue(self.photo.exists())
        self.assertEqual(len(latest), 1)
        events = []
        while not self.library.events.empty():
            events.append(self.library.events.get_nowait()[0])
        self.assertEqual(events[-2:], ["photos", "selected"])

    def test_leaving_during_download_does_not_replace_cached_selection(self):
        self.picker.items.return_value = [{"id": "new"}]

        def download(item, path):
            Image.new("RGB", (10, 10), "blue").save(path, format="PNG")
            self.library.cancel_selection()

        self.picker.download.side_effect = download
        with patch.object(controller, "wait_for_selection"):
            self.library._select_once()
        self.assertEqual(cached_photos(self.cache), [self.photo])
        self.assertEqual([p for p in self.cache.glob("google-photos-*") if p.is_dir()], [self.photo.parent])

    def test_worker_turns_failure_into_fallback_event(self):
        self.library.commands.put("select")

        def fail():
            raise DemoError("No selection")

        original_menu = self.library.menu

        def failed_menu(*args):
            original_menu(*args)
            self.library.shutdown.set()

        with patch.object(self.library, "_select_once", side_effect=fail), \
                patch.object(self.library, "menu", side_effect=failed_menu):
            self.library._run()
        events = []
        while not self.library.events.empty():
            events.append(self.library.events.get_nowait()[0])
        self.assertEqual(events, ["photos", "menu", "fallback"])

    def test_intentional_cancellation_does_not_publish_error_menu(self):
        self.library.commands.put("select")

        def cancel():
            self.library.cancel_selection()
            self.library.shutdown.set()
            raise DemoError("Photo selection cancelled")

        with patch.object(self.library, "_select_once", side_effect=cancel):
            self.library._run()
        self.assertEqual(self.library.events.get_nowait()[0], "photos")
        self.assertTrue(self.library.events.empty())

    def test_reentering_qr_reuses_active_session_and_tags_stale_menus(self):
        self.library.select()
        self.library.menu("Scan to choose your photos", [], "https://photos.google.com/picker/one")
        original = self.library.events.get_nowait()
        self.assertFalse(self.library.select())
        self.assertEqual(self.library.events.get_nowait(), original)
        previous_generation = original[1][0]
        self.library._selecting = False  # completed session
        self.assertTrue(self.library.select())
        self.assertGreater(self.library.generation, previous_generation)
        self.assertIsNone(self.library._menu)

    def test_quick_reentry_queues_one_new_selection_after_cancellation(self):
        self.assertTrue(self.library.select())
        self.library.cancel_selection()
        self.assertFalse(self.library.select())
        self.assertTrue(self.library._restart)
        self.library.cancel_selection()
        self.assertFalse(self.library._restart)


class PhotoFrameTests(unittest.TestCase):
    def test_decode_is_asynchronous_and_only_current_next_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(4):
                path = Path(directory) / f"{index}.png"
                Image.new("RGB", (10, 20), "red").save(path)
                paths.append(path)
            frames = controller.PhotoFrames((320, 180))
            try:
                self.assertIsNone(frames.get(paths, 0))
                for future in list(frames.pending.values()):
                    future.result(timeout=2)
                self.assertEqual(frames.get(paths, 0).shape, (180, 320, 4))
                frames.get(paths, 2)
                for future in list(frames.pending.values()):
                    future.result(timeout=2)
                self.assertEqual(frames.get(paths, 2).shape, (180, 320, 4))
                self.assertLessEqual(len(frames.ready), 2)
                self.assertLessEqual(len(frames.pending), 2)
            finally:
                frames.close()


if __name__ == "__main__":
    unittest.main()
