"""One-time Google login so the server can copy call recordings to the Drive folder (app/drive_sync.py).

Before running it (once, about 10 minutes, all free):
 1. https://console.cloud.google.com -> make a project (or pick one) -> "APIs & Services" -> Library -> "Google Drive API"
    -> Enable.
 2. "APIs & Services" -> "OAuth consent screen": User type External (or Internal for a Google Workspace company
    account), app name "Vedic Voice Agent", your e-mail; on "Test users" add the Google account that owns the folder.
    Then "Publish app" (In production), so the login does not run out after 7 days; Google shows an "unverified app"
    warning when you allow it: that is expected for your own app.
 3. "APIs & Services" -> Credentials -> Create credentials -> OAuth client ID -> Application type "Desktop app" ->
    Create -> Download JSON. Save the file as data/gdrive_client.json in the project folder.
 4. Run:  .venv\\Scripts\\python.exe backend\\scripts\\drive_login.py
    A browser opens: sign in with the account that owns (or can edit) the folder and allow access.

It writes data/gdrive_token.json (a secret: never share it or put it in git) and checks it can see the folder.
Restart the server afterwards; recordings are then copied after every call. No paid service is used.
"""
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import GDRIVE_CLIENT_FILE, GDRIVE_FOLDER_ID, GDRIVE_TOKEN_FILE  # noqa: E402

# Full Drive access: putting files into a folder this app did not create needs it (the narrower drive.file scope
# cannot see the owner's folder). The server only ever creates files in GDRIVE_FOLDER_ID.
SCOPE = "https://www.googleapis.com/auth/drive"


def main() -> None:
    if not GDRIVE_CLIENT_FILE.is_file():
        sys.exit(f"Put the OAuth client file from Google Cloud at {GDRIVE_CLIENT_FILE} first (see the steps at the top).")
    raw = json.loads(GDRIVE_CLIENT_FILE.read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web") or {}
    if not client.get("client_id"):
        sys.exit("That file is not an OAuth client: download it again as a 'Desktop app' OAuth client ID.")

    got: dict = {}
    state = secrets.token_urlsafe(16)

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Done. You can close this tab and go back to the terminal.</h3>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state})
    print("Opening the browser to sign in to Google. If it does not open, copy this link into it:\n" + url)
    webbrowser.open(url)
    for _ in range(600):
        if got:
            break
        time.sleep(0.5)
    if got.get("state") != state or not got.get("code"):
        sys.exit(f"No permission was given ({got.get('error') or 'timed out'}). Run it again.")

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": got["code"], "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Google refused the login: {r.text[:300]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        sys.exit("Google gave no long-lasting login (refresh token). Remove the app's access at "
                 "https://myaccount.google.com/permissions and run this again.")
    GDRIVE_TOKEN_FILE.write_text(json.dumps({
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
        "expires_at": time.time() + int(tok.get("expires_in") or 3600)}), encoding="utf-8")
    print(f"Login saved to {GDRIVE_TOKEN_FILE}.")

    f = httpx.get(f"https://www.googleapis.com/drive/v3/files/{GDRIVE_FOLDER_ID}",
                  params={"fields": "id,name,mimeType,capabilities/canAddChildren", "supportsAllDrives": "true"},
                  headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    if f.status_code != 200:
        sys.exit(f"Logged in, but this account cannot see the folder {GDRIVE_FOLDER_ID} ({f.status_code}). Sign in with "
                 "the account that owns it, or share the folder with this account as Editor, then run this again.")
    folder = f.json()
    if not (folder.get("capabilities") or {}).get("canAddChildren"):
        sys.exit(f"This account can see '{folder.get('name')}' but cannot add files to it: give it Editor access.")
    print(f"OK: recordings will be copied to the folder '{folder.get('name')}'. Restart the server to start.")


if __name__ == "__main__":
    main()
