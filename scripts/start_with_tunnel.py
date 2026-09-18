"""Start a Cloudflare quick tunnel, then the server with PUBLIC_BASE_URL set to the tunnel address.

The tunnel address changes every time this is started, so it is passed to the server as an
environment variable instead of being written into .env. Makes no paid API calls.
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLOUDFLARED = ROOT / "tools" / "cloudflared.exe"
PORT = "8000"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def main() -> int:
    if not CLOUDFLARED.exists():
        print("tools/cloudflared.exe not found")
        return 1
    (ROOT / "data").mkdir(exist_ok=True)
    tunnel = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    url = None
    deadline = time.time() + 45
    log_path = ROOT / "data" / "tunnel.log"
    log_file = log_path.open("w", encoding="utf-8")
    while time.time() < deadline and url is None:
        line = tunnel.stdout.readline()
        if not line:
            if tunnel.poll() is not None:
                break
            continue
        log_file.write(line)
        m = URL_RE.search(line)
        if m:
            url = m.group(0)
    if not url:
        print("Could not get a tunnel address. See data/tunnel.log")
        tunnel.terminate()
        return 1

    def drain() -> None:  # keep reading so the tunnel never blocks on a full pipe
        for line in tunnel.stdout:
            log_file.write(line)
            log_file.flush()

    threading.Thread(target=drain, daemon=True).start()
    (ROOT / "data" / "public_url.txt").write_text(url, encoding="utf-8")
    print("=" * 70)
    print(f"Public address: {url}")
    print(f"Dashboard:      http://127.0.0.1:{PORT}")
    print("Keep this window open. Close it to stop the phone line and the server.")
    print("=" * 70, flush=True)

    env = dict(os.environ, PUBLIC_BASE_URL=url, PYTHONIOENCODING="utf-8")
    try:
        return subprocess.call(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
            cwd=ROOT, env=env,
        )
    finally:
        tunnel.terminate()


if __name__ == "__main__":
    sys.exit(main())
