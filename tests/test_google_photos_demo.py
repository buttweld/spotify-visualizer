import os
from argparse import Namespace
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np
from PIL import Image

from visualizer.photos import api, cache, render
from visualizer.photos import demo


class AuthorizationTests(unittest.TestCase):
    def test_missing_client_file_explains_manual_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(client_secrets=Path(directory) / "missing.json")
            with self.assertRaisesRegex(api.DemoError, "sync script excludes credential files"):
                api.authorize(args)

    def test_service_account_json_explains_desktop_client_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            path.write_text('{"type":"service_account"}')
            with self.assertRaisesRegex(api.DemoError, "Desktop app OAuth client JSON"):
                api.authorize(Namespace(client_secrets=path))


class PickerTests(unittest.TestCase):
    def test_pagination_filters_videos_and_empty_pages(self):
        picker = api.PhotosPicker(Mock())
        picker.request = Mock(side_effect=[
            {"mediaItems": [{"type": "VIDEO"}, {"type": "PHOTO", "id": "a"}], "nextPageToken": "two"},
            {"nextPageToken": "three"},
            {"mediaItems": [{"type": "PHOTO", "id": "b"}]},
        ])
        self.assertEqual([item["id"] for item in picker.items("session")], ["a", "b"])
        self.assertEqual(picker.request.call_args.kwargs["params"]["pageToken"], "three")

    def test_polling_uses_server_interval_and_stops_at_timeout(self):
        elapsed = [0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += seconds

        session = {"id": "session", "pollingConfig": {"pollInterval": "3.5s", "timeoutIn": "8s"}}
        picker = Mock()
        picker.get.return_value = session
        with self.assertRaisesRegex(api.DemoError, "timed out"):
            api.wait_for_selection(picker, session, lambda: elapsed[0], sleep)
        self.assertEqual(sleeps, [3.5, 3.5, 1])
        self.assertEqual(picker.get.call_count, 2)

    def test_polling_returns_completed_session(self):
        picker = Mock()
        picker.get.return_value = {"id": "session", "mediaItemsSet": True}
        result = api.wait_for_selection(picker, {"id": "session"}, lambda: 0, lambda _: None)
        self.assertTrue(result["mediaItemsSet"])

    def test_download_uses_authenticated_transport_and_display_dimensions(self):
        transport = MagicMock()
        response = transport.get.return_value.__enter__.return_value
        response.ok = True
        response.iter_content.return_value = [b"image", b" bytes"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo"
            api.PhotosPicker(transport).download({"mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/photo",
            }}, path)
            self.assertEqual(path.read_bytes(), b"image bytes")
        self.assertEqual(transport.get.call_args.args[0],
                         "https://lh3.googleusercontent.com/photo=w1920-h1080")

    def test_api_error_shows_action_without_sensitive_response(self):
        transport = MagicMock()
        response = transport.request.return_value
        response.ok = False
        response.status_code = 403
        response.text = "private data"
        with self.assertRaisesRegex(api.DemoError, "Enable Photos Picker API"):
            api.PhotosPicker(transport).create()
        response.close.assert_called_once()

    def test_failed_images_are_skipped_and_valid_image_is_retained(self):
        picker = Mock()
        picker.items.return_value = [{"id": "bad"}, {"id": "good"}]

        def download(item, path):
            if item["id"] == "bad":
                path.write_bytes(b"invalid")
            else:
                Image.new("RGB", (10, 10), "red").save(path, format="PNG")

        picker.download.side_effect = download
        with tempfile.TemporaryDirectory() as directory:
            paths = cache.prepare_photos(picker, "session", directory, Mock())
            self.assertEqual(len(paths), 1)
            self.assertFalse((Path(directory) / "0.photo").exists())


class DisplayTests(unittest.TestCase):
    def test_saved_selection_survives_restart_in_download_order(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            photos = cache_dir / "google-photos-selected"
            photos.mkdir()
            for name in ("10.photo", "2.photo", "0.photo"):
                Image.new("RGB", (10, 10), "red").save(photos / name, format="PNG")
            (photos / "1.photo").write_bytes(b"incomplete download")
            cache.remember_selection(cache_dir, photos)
            self.assertEqual([p.name for p in cache.cached_photos(cache_dir)],
                             ["0.photo", "2.photo", "10.photo"])
            self.assertEqual(cache.cached_photos(cache_dir, photos), cache.cached_photos(cache_dir))

    def test_resume_missing_cache_explains_how_to_download(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(api.DemoError, "without --resume"):
                cache.cached_photos(Path(directory))

    def test_resume_plays_without_loading_google_credentials(self):
        screen_class = MagicMock()
        screen = screen_class.return_value.__enter__.return_value
        screen.get_screen_size.return_value = (1920, 1080)
        args = Namespace(drm_device="test", resume="", cache_dir=Path(".cache"))
        with patch.dict("sys.modules", {"drm_display": types.SimpleNamespace(Screen=screen_class)}), \
                patch.object(demo, "cached_photos", return_value=[Path("cached.photo")]), \
                patch.object(demo, "load_credentials") as credentials, \
                patch.object(demo, "play_slideshow") as play:
            demo.run_demo(args)
            credentials.assert_not_called()
            play.assert_called_once_with([Path("cached.photo")], screen.show)

    def test_portrait_is_letterboxed_and_converted_to_bgra(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.png"
            Image.new("RGB", (200, 400), "red").save(path)
            frame = render.photo_frame(path)
        self.assertEqual(frame.shape, (1080, 1920, 4))
        np.testing.assert_array_equal(frame[500, 0], [0, 0, 0, 255])
        np.testing.assert_array_equal(frame[500, 960], [0, 0, 255, 255])

    def test_loop_changes_photos_every_second_and_wraps(self):
        now = [0]
        shown = []

        def show(frame):
            shown.append((now[0], frame))

        def sleep(seconds):
            now[0] += seconds

        with patch.object(demo, "photo_frame", side_effect=lambda path: path), \
                patch("visualizer.state.random.shuffle", side_effect=lambda order: None):
            demo.play_slideshow(["a", "b"], show, lambda: now[0], sleep,
                                stop=lambda: len(shown) == 3)
        self.assertEqual(shown, [(0, "a"), (1, "b"), (2, "a")])

    def test_credentials_saved_with_private_permissions(self):
        credentials = Mock()
        credentials.to_json.return_value = '{"refresh_token":"test"}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            api.save_credentials(credentials, path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), credentials.to_json.return_value)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
