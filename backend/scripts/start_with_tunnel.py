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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # the project folder: tools/, data/
CLOUDFLARED = ROOT / "tools" / "cloudflared.exe"
PORT = "8000"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


TRIES = 3            # Cloudflare's free quick tunnels sometimes do not answer (22 Oct 2026, 16:16): try again
WAIT_SECONDS = 45    # for the tunnel's address, each try


def open_tunnel(log_file) -> tuple[subprocess.Popen | None, str | None]:
    """Start cloudflared and wait for its address. The output is read on its own thread, so a cloudflared that goes
    quiet cannot hold this up past WAIT_SECONDS (a blocking readline did: the window hung with no message)."""
    tunnel = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    found = threading.Event()
    url: list[str] = []

    def read() -> None:   # all of cloudflared's output to the log, for as long as it runs
        for line in tunnel.stdout:
            log_file.write(line)
            log_file.flush()
            m = URL_RE.search(line)
            if m and not url:
                url.append(m.group(0))
                found.set()

    threading.Thread(target=read, daemon=True).start()
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline and not found.is_set() and tunnel.poll() is None:
        found.wait(0.5)
    if url:
        return tunnel, url[0]
    tunnel.terminate()
    return None, None


CHECK_SECONDS = 30   # how often the tunnel is checked, through its public address, while the server runs
FAILS_TO_REPLACE = 3  # checks in a row that fail before the tunnel is replaced (about 1.5 minutes)


def get_tunnel(log_file) -> tuple[subprocess.Popen | None, str | None]:
    """A working tunnel and its address, trying up to TRIES times."""
    for attempt in range(1, TRIES + 1):
        print(f"Opening the phone line (Cloudflare tunnel), try {attempt} of {TRIES}...", flush=True)
        tunnel, url = open_tunnel(log_file)
        if url:
            return tunnel, url
        print(f"No tunnel address after {WAIT_SECONDS} s.", flush=True)
    return None, None


def announce(url: str) -> None:
    (ROOT / "data" / "public_url.txt").write_text(url, encoding="utf-8")   # the server reads it for every call
    print("=" * 70)
    print(f"Public address: {url}")
    print(f"Dashboard:      http://127.0.0.1:{PORT}")
    print("Keep this window open. Close it to stop the phone line and the server.")
    print("=" * 70, flush=True)


def reachable(url: str) -> bool:
    """This server answers at its public address, as Plivo will reach it."""
    try:
        with urllib.request.urlopen(f"{url}/api/live", timeout=15) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def main() -> int:
    if not CLOUDFLARED.exists():
        print("tools/cloudflared.exe not found")
        return 1
    (ROOT / "data").mkdir(exist_ok=True)
    log_file = (ROOT / "data" / "tunnel.log").open("w", encoding="utf-8")
    tunnel, url = get_tunnel(log_file)
    if not url:
        print("Could not get a tunnel address: Cloudflare's free tunnel did not answer. Wait a minute and run "
              "run_phone.bat again. See data/tunnel.log")
        return 1
    announce(url)

    env = dict(os.environ, PUBLIC_BASE_URL=url, PYTHONIOENCODING="utf-8")
    # --timeout-keep-alive: see run.bat (the dashboard's 5 s polls met uvicorn's 5 s idle cut: ECONNRESET).
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(ROOT / "backend"), "--host", "127.0.0.1", "--port", PORT,
         "--timeout-keep-alive", "75"],
        cwd=ROOT, env=env,
    )
    # Owner, 22 Oct 2026: the quick tunnel died ("Tunnel not found") and every call of a 952-contact campaign was
    # refused. A tunnel that stops answering is replaced; the server reads the new address from data/public_url.txt,
    # so it keeps running, and so does a campaign.
    fails = 0
    try:
        while server.poll() is None:
            time.sleep(CHECK_SECONDS)
            if server.poll() is not None:
                break
            ok = tunnel is not None and tunnel.poll() is None and reachable(url)
            fails = 0 if ok else fails + 1
            if fails < FAILS_TO_REPLACE:
                continue
            print(f"The phone line ({url}) stopped working. Opening a new one; the server keeps running...", flush=True)
            if tunnel is not None:
                tunnel.terminate()
            new_tunnel, new_url = get_tunnel(log_file)
            if new_url:
                tunnel, url, fails = new_tunnel, new_url, 0
                announce(url)
            else:
                tunnel = None
                print("Could not open a new phone line yet; trying again in a minute. Calls cannot connect meanwhile.",
                      flush=True)
                fails = FAILS_TO_REPLACE - 1   # one more failed check, then another try
        return server.returncode or 0
    except KeyboardInterrupt:
        server.terminate()
        return 0
    finally:
        if tunnel is not None:
            tunnel.terminate()


if __name__ == "__main__":
    sys.exit(main())
