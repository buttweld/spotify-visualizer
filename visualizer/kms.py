"""Native-resolution uploads to an atomic KMS overlay, using drm-display's fd."""
import ctypes
from pathlib import Path

import numpy as np


class KmsError(RuntimeError):
    pass


class Plane:
    def __init__(self, screen, sizes):
        self.context = ctypes.c_void_p()
        backend = getattr(screen, "display", None)
        fd = getattr(backend, "fd", None)
        crtc = getattr(backend, "crtc", None)
        if not isinstance(fd, int) or fd < 0 or crtc is None:
            raise KmsError("Hardware scaling requires the DRM/KMS backend and an active CRTC.")
        self.size = tuple(screen.get_screen_size())
        unique_sizes = list(dict.fromkeys(tuple(size) for size in sizes))
        if not 1 <= len(unique_sizes) <= 3 or any(
                len(size) != 2 or any(not isinstance(value, int) or not 1 <= value <= 16384 for value in size)
                for size in unique_sizes):
            raise KmsError("Configure one to three positive screen resolutions, up to 16384 pixels per dimension.")
        self.sizes = set(unique_sizes)
        root = Path(__file__).resolve().parents[1]
        library_path = root / ".cache" / "native" / "libkms_plane.so"
        if not library_path.exists():
            raise KmsError("KMS helper is missing. Run: .venv/bin/python scripts/build_kms.py")
        if library_path.stat().st_mtime_ns < (root / "native" / "kms_plane.c").stat().st_mtime_ns:
            raise KmsError("KMS helper is older than its source. Run: .venv/bin/python scripts/build_kms.py")
        try:
            self.lib = ctypes.CDLL(str(library_path))
        except OSError as exc:
            raise KmsError("Cannot load KMS helper. Rebuild it on this Pi with scripts/build_kms.py.") from exc
        self.lib.kms_open.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_void_p)]
        self.lib.kms_open.restype = ctypes.c_int
        self.lib.kms_show.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_size_t]
        self.lib.kms_show.restype = ctypes.c_int
        self.lib.kms_error.argtypes = [ctypes.c_void_p]
        self.lib.kms_error.restype = ctypes.c_char_p
        self.lib.kms_plane_id.argtypes = [ctypes.c_void_p]
        self.lib.kms_plane_id.restype = ctypes.c_uint32
        self.lib.kms_close.argtypes = [ctypes.c_void_p]
        self.lib.kms_close.restype = ctypes.c_int
        dimensions = (ctypes.c_uint32 * (len(unique_sizes) * 2))(
            *(value for size in unique_sizes for value in size))
        result = self.lib.kms_open(fd, crtc.contents.crtc_id, *self.size,
                                   dimensions, len(unique_sizes), ctypes.byref(self.context))
        if result:
            message = self.lib.kms_error(self.context).decode("utf-8", "replace")
            self.close()
            raise KmsError(f"Hardware scaling unavailable: {message}")
        self.plane_id = self.lib.kms_plane_id(self.context)
        print(f"KMS hardware scaling: overlay {self.plane_id}, HDMI {self.size[0]}x{self.size[1]}, "
              f"sources {unique_sizes}; plane-only commits, no mode changes", flush=True)

    def show(self, frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 4:
            raise ValueError("KMS frame must be an H×W×4 uint8 BGRA array")
        height, width = frame.shape[:2]
        if (width, height) not in self.sizes:
            raise ValueError(f"Frame resolution {width}x{height} was not tested at startup")
        frame = np.ascontiguousarray(frame)
        result = self.lib.kms_show(self.context, frame.ctypes.data, width, height, frame.strides[0])
        if result:
            message = self.lib.kms_error(self.context).decode("utf-8", "replace")
            raise KmsError(f"KMS presentation failed: {message}")

    def close(self):
        if self.context:
            context, self.context = self.context, ctypes.c_void_p()
            if self.lib.kms_close(context):
                print("KMS warning: overlay cleanup failed; the DRM owner will restore the display.", flush=True)
