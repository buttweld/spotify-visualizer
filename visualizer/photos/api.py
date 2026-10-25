"""Authenticated Google Photos Picker API and desktop authorization."""
import json
import os
from pathlib import Path
import tempfile
import time

WIDTH, HEIGHT = 1920, 1080
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
API = "https://photospicker.googleapis.com/v1"
SETUP_URL = "https://developers.google.com/photos/picker/guides/get-started-picker"
TOKEN_PATH = Path(".google-photos-token.json")

class DemoError(Exception):
    """An actionable error that is safe to show on the display."""

def save_credentials(credentials, path):
    """Replace the token atomically, with permissions set before writing secrets."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="credentials-photos-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def authorize(args):
    try:
        with args.client_secrets.open() as stream:
            config = json.load(stream)
    except FileNotFoundError as exc:
        raise DemoError(
            f"OAuth client JSON not found: {args.client_secrets}. "
            "Copy the downloaded Desktop app JSON to this path, or pass --client-secrets PATH. "
            "The sync script excludes credential files."
        ) from exc
    except (OSError, ValueError) as exc:
        raise DemoError("Cannot read OAuth client JSON. Check its permissions and download it again if invalid.") from exc
    installed = config.get("installed") if isinstance(config, dict) else None
    if not isinstance(installed, dict) or not all(installed.get(key) for key in (
            "client_id", "client_secret", "auth_uri", "token_uri")):
        raise DemoError("Expected a Desktop app OAuth client JSON. Download it from Google Cloud Credentials.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise DemoError(
            "Google authorization dependencies are missing. Run: "
            ".venv/bin/pip install -r requirements.txt"
        ) from exc

    # A desktop OAuth client uses a loopback callback, including over SSH -L.
    flow = InstalledAppFlow.from_client_config(
        config, [SCOPE], autogenerate_code_verifier=True,
    )
    credentials = flow.run_local_server(
        host="127.0.0.1", port=args.auth_port, open_browser=False,
        timeout_seconds=300, access_type="offline", prompt="consent",
        authorization_prompt_message=(
            "Open this URL in your computer's browser (not your phone).\n"
            "For a headless Pi, first forward the callback port using SSH.\n{url}"
        ),
    )
    save_credentials(credentials, args.token)
    print(f"Authorization saved to {args.token}. Start the demo to select photos.")

def load_credentials(path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not path.exists():
        raise DemoError("Run --authorize first; see the Google Photos setup section in README.md.")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), [SCOPE])
        if not credentials.valid:
            if not credentials.refresh_token:
                raise DemoError("Google access expired. Run --authorize again.")
            credentials.refresh(Request())
            save_credentials(credentials, path)
        return credentials
    except DemoError:
        raise
    except Exception as exc:
        raise DemoError("Cannot load or refresh Google access. Run --authorize again.") from exc

class PhotosPicker:
    def __init__(self, transport, size=(WIDTH, HEIGHT)):
        self.transport = transport
        self.width, self.height = size

    def request(self, method, resource, **kwargs):
        response = self.transport.request(method, API + resource, timeout=30, **kwargs)
        try:
            if not response.ok:
                hints = {
                    401: "Run --authorize again.",
                    403: "Enable Photos Picker API, check the scope and your test user.",
                    404: "The selection session expired. Restart the demo to select again.",
                    429: "Google's request quota was reached. Try again later.",
                }
                raise DemoError(f"Google Photos returned HTTP {response.status_code}. "
                                + hints.get(response.status_code, "Check your connection and retry."))
            return response.json() if response.content else {}
        finally:
            response.close()

    def create(self):
        return self.request("POST", "/sessions", json={})

    def get(self, session_id):
        from urllib.parse import quote
        return self.request("GET", "/sessions/" + quote(session_id, safe=""))

    def delete(self, session_id):
        from urllib.parse import quote
        self.request("DELETE", "/sessions/" + quote(session_id, safe=""))

    def items(self, session_id):
        page_token = ""
        while True:
            page = self.request("GET", "/mediaItems", params={
                "sessionId": session_id, "pageSize": 100, "pageToken": page_token,
            })
            for item in page.get("mediaItems", []):
                if item.get("type") == "PHOTO":
                    yield item
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def download(self, item, path):
        # Picker base URLs require bearer authentication, unlike public image URLs.
        url = item["mediaFile"]["baseUrl"] + f"=w{self.width}-h{self.height}"
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".googleusercontent.com"):
            raise DemoError("Google returned an unexpected photo host.")
        with self.transport.get(url, timeout=60, stream=True) as response:
            if not response.ok:
                raise DemoError(f"Photo download returned HTTP {response.status_code}.")
            size = 0
            with open(path, "wb") as stream:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise DemoError("Photo exceeds the demo's 32 MB download limit.")
                    stream.write(chunk)

def duration(value, default):
    try:
        result = float(value.removesuffix("s"))
        return result if 0 < result < 86400 else default
    except (ValueError, TypeError, AttributeError):
        return default

def wait_for_selection(picker, session, clock=time.monotonic, sleep=time.sleep, stop=None):
    config = session.get("pollingConfig", {})
    deadline = clock() + duration(config.get("timeoutIn"), 1200)
    while not session.get("mediaItemsSet"):
        interval = duration(session.get("pollingConfig", {}).get("pollInterval"), 5)
        remaining = deadline - clock()
        if remaining <= 0:
            raise DemoError("Photo selection timed out. Restart the demo for a new QR code.")
        if stop is not None:
            if stop.wait(min(interval, remaining)):
                raise DemoError("Photo selection cancelled; keeping previous photos.")
        else:
            sleep(min(interval, remaining))
        if clock() >= deadline:
            raise DemoError("Photo selection timed out. Restart the demo for a new QR code.")
        session = picker.get(session["id"])
    return session
