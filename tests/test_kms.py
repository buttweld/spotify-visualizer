import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from visualizer.kms import KmsError, Plane


class PlaneTests(unittest.TestCase):
    def setUp(self):
        self.screen = Mock()
        self.screen.get_screen_size.return_value = (1920, 1080)
        self.screen.display = SimpleNamespace(fd=7, crtc=SimpleNamespace(contents=SimpleNamespace(crtc_id=42)))
        self.lib = Mock()
        self.lib.kms_open.side_effect = self.open
        self.lib.kms_show.return_value = 0
        self.lib.kms_close.return_value = 0
        self.lib.kms_plane_id.return_value = 100
        self.lib.kms_error.return_value = b"no scaling plane"
        self.patches = [patch("visualizer.kms.Path.exists", return_value=True),
                        patch("visualizer.kms.Path.stat", return_value=SimpleNamespace(st_mtime_ns=1)),
                        patch("visualizer.kms.ctypes.CDLL", return_value=self.lib)]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, fd, crtc, width, height, sizes, count, out):
        out._obj.value = 123
        self.dimensions = list(sizes)
        return 0

    def test_preflight_deduplicates_screen_sizes_and_uploads_original_720_frame(self):
        plane = Plane(self.screen, [(1920,1080), (720,400), (1920,1080)])
        self.assertEqual(self.dimensions, [1920,1080,720,400])
        self.assertEqual(self.lib.kms_open.call_args.args[:4], (7,42,1920,1080))
        frame = np.zeros((400,720,4), dtype=np.uint8)
        plane.show(frame)
        args = self.lib.kms_show.call_args.args
        self.assertEqual(args[1:], (frame.ctypes.data, 720,400,720*4))
        self.screen.show.assert_not_called()
        plane.close()
        plane.close()
        self.lib.kms_close.assert_called_once()

    def test_failed_preflight_releases_partial_context(self):
        def failed(*args):
            self.open(*args)
            return -22
        self.lib.kms_open.side_effect = failed
        with self.assertRaisesRegex(KmsError, "no scaling plane"):
            Plane(self.screen, [(720,400)])
        self.lib.kms_close.assert_called_once()

    def test_rejects_unprepared_resolution_before_native_upload(self):
        plane = Plane(self.screen, [(720,400)])
        try:
            with self.assertRaisesRegex(ValueError, "not tested at startup"):
                plane.show(np.zeros((100,100,4), dtype=np.uint8))
            self.lib.kms_show.assert_not_called()
        finally:
            plane.close()

    def test_missing_native_build_has_actionable_message(self):
        with patch("visualizer.kms.Path.exists", return_value=False):
            with self.assertRaisesRegex(KmsError, "scripts/build_kms.py"):
                Plane(self.screen, [(720,400)])

    def test_present_error_is_not_silently_replaced_by_software_scaling(self):
        plane = Plane(self.screen, [(720,400)])
        self.lib.kms_show.return_value = -22
        try:
            with self.assertRaisesRegex(KmsError, "KMS presentation failed"):
                plane.show(np.zeros((400,720,4), dtype=np.uint8))
            self.screen.show.assert_not_called()
        finally:
            plane.close()

    def test_rejects_non_drm_backend_before_native_open(self):
        self.screen.display = SimpleNamespace(fd=None)
        with self.assertRaisesRegex(KmsError, "DRM/KMS backend"):
            Plane(self.screen, [(720,400)])
        self.lib.kms_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
