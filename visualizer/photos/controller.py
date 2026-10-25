"""Background selection/downloads and bounded asynchronous photo decoding."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import shutil
import tempfile
import threading

from .api import (DemoError, PhotosPicker, SETUP_URL, load_credentials,
                  save_credentials, wait_for_selection)
from .cache import cached_photos, prepare_photos, remember_selection
from .render import photo_frame


class PhotoLibrary:
    def __init__(self, cache_dir, token, size, shutdown):
        self.cache_dir, self.token, self.size = Path(cache_dir), Path(token), size
        self.shutdown = shutdown
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.cancel = threading.Event()
        self._guard = threading.Lock()
        self._selecting = False
        self._restart = False
        self.generation = 0
        self._menu = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="google-photos")

    def start(self):
        self.thread.start()

    def select(self):
        with self._guard:
            if self._selecting:
                if self.cancel.is_set():
                    self._restart = True
                elif self._menu is not None:
                    self.events.put(("menu", self._menu))
                return False
            self.generation += 1
            self._menu = None
            self._selecting = True
            self.cancel.clear()
            self.commands.put("select")
            return True

    def cancel_selection(self):
        with self._guard:
            self._restart = False
            self.cancel.set()

    def menu(self, title, lines, qr=None):
        self._menu = (self.generation, (title, lines, qr))
        self.events.put(("menu", self._menu))

    def _run(self):
        try:
            paths = cached_photos(self.cache_dir)
            self.events.put(("photos", tuple(paths)))
        except DemoError:
            pass
        except OSError:
            self.menu("Photo cache unavailable", ["Check the cache directory permissions."])
        while not self.shutdown.is_set():
            try:
                self.commands.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if not self.cancel.is_set():
                    self._select_once()
            except Exception as exc:
                if self.cancel.is_set() or self.shutdown.is_set():
                    continue
                message = str(exc) if isinstance(exc, DemoError) else "Google connection or download failed."
                # Never include HTTP URLs, access tokens or raw exceptions in logs.
                print(f"Google Photos: {message}", flush=True)
                self.menu("Keeping your saved photos", [message, "Cycle back here with OK to try again."])
                self.events.put(("fallback", None))
            finally:
                with self._guard:
                    self._selecting = False
                    if self._restart and not self.shutdown.is_set():
                        self._restart = False
                        self.generation += 1
                        self._menu = None
                        self._selecting = True
                        self.cancel.clear()
                        self.commands.put("select")

    def _select_once(self):
        if not self.token.exists():
            self.menu("Set up Google Photos", [
                "Scan for Google's setup guide.", "Enable Photos Picker API; create a Desktop client.",
                "Authorize via SSH; see README.md.",
            ], SETUP_URL)
            self.events.put(("fallback", None))
            return
        from google.auth.transport.requests import AuthorizedSession
        credentials = load_credentials(self.token)
        with AuthorizedSession(credentials) as transport:
            picker = PhotosPicker(transport, self.size)
            session_id, directory, published = None, None, False
            try:
                if self.cancel.is_set():
                    return
                session = picker.create()
                session_id = session["id"]
                self.menu("Scan to choose your photos", [
                    "Scan, sign in, choose photos, then tap Done.",
                    "Selected photos stay on this Pi until deleted.",
                    "No selection? Your saved photos will continue.",
                    "",
                ], session["pickerUri"])
                wait_for_selection(picker, session, stop=self.cancel)
                if self.cancel.is_set() or self.shutdown.is_set():
                    return
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                directory = tempfile.mkdtemp(prefix="google-photos-", dir=self.cache_dir)

                def progress(index):
                    self.menu("Preparing your slideshow", [
                        f"Downloading photo {index}…", "Your saved selection is kept until this completes.",
                    ])

                paths = prepare_photos(picker, session_id, directory,
                                       progress=progress, stop=self.cancel)
                if self.cancel.is_set() or self.shutdown.is_set():
                    return
                remember_selection(self.cache_dir, directory)
                published = True
                self.events.put(("photos", tuple(paths)))
                self.events.put(("selected", None))
            finally:
                if directory and not published:
                    shutil.rmtree(directory, ignore_errors=True)
                if session_id:
                    try:
                        picker.delete(session_id)
                    except Exception:
                        print("Google Photos: session cleanup failed; it will expire.", flush=True)
                save_credentials(credentials, self.token)

    def close(self):
        self.cancel.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)


class PhotoFrames:
    """Decode current/next photos outside the display and audio threads."""
    def __init__(self, size):
        self.size = size
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-decode")
        self.ready = {}
        self.pending = {}
        self.failed = set()

    def get(self, paths, index):
        if not paths:
            return None
        current = paths[index]
        wanted = {current, paths[(index + 1) % len(paths)]}
        self.ready = {path: frame for path, frame in self.ready.items() if path in wanted}
        for path, future in list(self.pending.items()):
            if future.done():
                del self.pending[path]
                try:
                    frame = future.result()
                    if path in wanted:
                        self.ready[path] = frame
                except Exception:
                    self.failed.add(path)
                    print(f"Skipping photo that could not be decoded: {path.name}", flush=True)
            elif path not in wanted and future.cancel():
                del self.pending[path]
        for path in (current, paths[(index + 1) % len(paths)]):
            if path not in self.ready and path not in self.pending and path not in self.failed and len(self.pending) < 2:
                self.pending[path] = self.executor.submit(photo_frame, path, self.size)
        return self.ready.get(current)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
