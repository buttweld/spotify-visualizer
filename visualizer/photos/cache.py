"""Validated photo downloads and atomic publication of persistent selections."""
import os
from pathlib import Path
import tempfile
from PIL import Image
from .api import DemoError
from .render import instruction_frame

def prepare_photos(picker, session_id, directory, show=None, progress=None, stop=None):
    paths = []
    failed = 0
    for index, item in enumerate(picker.items(session_id)):
        path = Path(directory) / f"{index}.photo"
        if stop is not None and stop.is_set():
            raise DemoError("Photo download cancelled; keeping previous photos.")
        if progress:
            progress(index + 1)
        if show:
            show(instruction_frame("Preparing your slideshow", [
                f"Downloading photo {index + 1}…", "Photos stay on this device during the demo.",
                "The slideshow starts when downloads finish.",
            ]))
        try:
            picker.download(item, path)
            # Validate now so an unsupported file cannot crash a running slideshow.
            with Image.open(path) as image:
                image.load()
            paths.append(path)
        except (DemoError, OSError, ValueError):
            failed += 1
            path.unlink(missing_ok=True)
    if failed:
        print(f"Skipped {failed} photos that could not be downloaded or decoded.")
    if not paths:
        raise DemoError("No displayable photos selected. Restart and choose photos, not videos.")
    return paths

def remember_selection(cache_dir, directory):
    """Publish a completed selection without replacing an earlier cache on failure."""
    marker = cache_dir / "google-photos-latest.txt"
    fd, temporary = tempfile.mkstemp(dir=cache_dir, prefix=".photos-latest-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(Path(directory).name)
        os.replace(temporary, marker)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def cached_photos(cache_dir, directory=None):
    if directory is None:
        marker = cache_dir / "google-photos-latest.txt"
        try:
            name = marker.read_text().strip()
        except OSError as exc:
            raise DemoError("No saved slideshow. Run without --resume to select and download photos first.") from exc
        if not name.startswith("google-photos-") or Path(name).name != name:
            raise DemoError("Invalid saved slideshow location. Select photos again.")
        directory = cache_dir / name
    paths = []
    for path in sorted(Path(directory).glob("*.photo"),
                       key=lambda path: (int(path.stem) if path.stem.isdigit() else float("inf"), path.name)):
        try:
            with Image.open(path) as image:
                image.load()
            paths.append(path)
        except (OSError, ValueError):
            print(f"Skipping unreadable cached photo: {path.name}")
    if not paths:
        raise DemoError("No readable cached photos. Run without --resume to select photos again.")
    return paths
