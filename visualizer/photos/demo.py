"""Standalone Google Photos slideshow demo and authorization command."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
from pathlib import Path
import tempfile
import time

from .api import (DemoError, PhotosPicker, SETUP_URL, TOKEN_PATH, authorize,
                  load_credentials, save_credentials, wait_for_selection)
from .cache import cached_photos, prepare_photos, remember_selection
from .render import WIDTH, HEIGHT, instruction_frame, photo_frame
from ..state import DisplayState
SLIDE_SECONDS = 1.0

def play_slideshow(paths, show, clock=time.monotonic, sleep=time.sleep, stop=lambda: False):
    # Only current and next frames live in RAM, important on a Pi Zero 2 W.
    with ThreadPoolExecutor(max_workers=1) as executor:
        order = DisplayState(0)
        order.set_photos(len(paths), 0)
        frame = photo_frame(paths[order.photo_order[0]])
        while not stop():
            show(frame)
            deadline = clock() + SLIDE_SECONDS
            order.step_photo(1)
            upcoming = executor.submit(photo_frame, paths[order.photo_order[order.photo_index]])
            sleep(max(0, deadline - clock()))
            frame = upcoming.result()

def run_demo(args):
    from drm_display import Screen

    with Screen(device=args.drm_device, width=WIDTH, height=HEIGHT) as screen:
        if tuple(screen.get_screen_size()) != (WIDTH, HEIGHT):
            raise DemoError("Display must support 1920x1080. Check HDMI mode configuration.")
        try:
            if args.resume is not None:
                directory = Path(args.resume) if args.resume else None
                paths = cached_photos(args.cache_dir, directory)
                print(f"Resuming {len(paths)} cached photos (no Google connection required).")
                play_slideshow(paths, screen.show)
                return
            from google.auth.transport.requests import AuthorizedSession

            if not args.token.exists():
                screen.show(instruction_frame("Set up Google Photos", [
                    "1. Scan for Google's setup guide.", "2. Enable Photos Picker API.",
                    "3. Create a Desktop OAuth client.", "4. Run --authorize using SSH.",
                    "See README.md for exact commands.",
                ], SETUP_URL))
                print("No authorization yet. See README.md; press Ctrl+C to exit.")
                while True:
                    time.sleep(1)
            credentials = load_credentials(args.token)
            with AuthorizedSession(credentials) as transport:
                # Persist bytes across Ctrl+C and restarts. Each selection has its
                # own directory so a failed download cannot overwrite the last one.
                directory = tempfile.mkdtemp(prefix="google-photos-", dir=args.cache_dir)
                picker = PhotosPicker(transport)
                session_id = None
                try:
                    session = picker.create()
                    session_id = session["id"]
                    screen.show(instruction_frame("Scan to choose your photos", [
                        "1. Scan with your phone camera.", "2. Use the authorized Google account.",
                        "3. Select photos and tap Done.", "Your slideshow starts automatically.",
                    ], session["pickerUri"]))
                    wait_for_selection(picker, session)
                    paths = prepare_photos(picker, session_id, directory, screen.show)
                    remember_selection(args.cache_dir, directory)
                    print(f"Saved {len(paths)} photos. Restart with --resume to play them again.")
                    # Keep the selection alive until all photo bytes are cached.
                    picker.delete(session_id)
                    session_id = None
                    play_slideshow(paths, screen.show)
                finally:
                    if session_id:
                        try:
                            picker.delete(session_id)
                        except Exception:
                            print("Could not clean up the Google selection session; it will expire.")
                    save_credentials(credentials, args.token)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # Do not put credential-bearing request URLs or tokens on HDMI/in logs.
            message = str(exc) if isinstance(exc, DemoError) else "Connection or file error. Check setup and retry."
            print(message)
            import textwrap
            screen.show(instruction_frame("Unable to start slideshow", textwrap.wrap(message, 65) + [
                "Press Ctrl+C, resolve the issue, then restart.",
            ]))
            while True:
                time.sleep(1)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true", help="Authorize once using a computer browser; no HDMI required")
    parser.add_argument("--client-secrets", type=Path, default=Path("credentials-google-photos.json"))
    parser.add_argument("--token", type=Path, default=TOKEN_PATH)
    parser.add_argument("--auth-port", type=int, default=8080, help="Loopback callback port (forward over SSH)")
    parser.add_argument("--drm-device", default="/dev/dri/card0")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"), help="Persistent photo cache; use disk rather than tmpfs on a Pi")
    parser.add_argument("--resume", nargs="?", const="", metavar="DIRECTORY",
                        help="Play the last saved selection offline, or photos from a specified cache directory")
    args = parser.parse_args()
    try:
        if args.authorize:
            authorize(args)
        else:
            args.cache_dir.mkdir(parents=True, exist_ok=True)
            run_demo(args)
    except KeyboardInterrupt:
        print("\nStopped Google Photos demo.")
    except Exception as exc:
        if isinstance(exc, DemoError):
            parser.exit(1, f"{exc}\n")
        if args.authorize:
            if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
                parser.exit(1, f"Pi callback port {args.auth_port} is busy. Use another --auth-port and matching SSH -L ports.\n")
            if isinstance(exc, PermissionError):
                parser.exit(1, "Authorization file permission error. Check access to the credentials and token directory.\n")
            parser.exit(1, f"Authorization failed ({type(exc).__name__}). "
                        "Check internet access, approve consent, and open the URL on the computer running the SSH tunnel within 5 minutes.\n")
        parser.exit(1, "Could not start demo. Check dependencies, credentials, DRM permissions and callback port.\n")
