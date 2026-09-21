"""Real headed Google Chrome on a throwaway profile, egressing through a proxy.

BENCH_PROXY        proxy URL for --proxy-server (e.g. http://127.0.0.1:9891), required
BENCH_CHROME_PORT  remote debugging port (default 9893)
BENCH_PROFILE_ROOT parent dir for the throwaway profile
Never headless.
"""

import json
import os
import subprocess
import tempfile
import time
import urllib.request

CHROME = os.environ.get(
    "BENCH_CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
_proc = None
PROFILE = None


async def connect() -> str:
    global _proc, PROFILE
    proxy = os.environ["BENCH_PROXY"]
    port = int(os.environ.get("BENCH_CHROME_PORT", "9893"))
    root = os.environ.get("BENCH_PROFILE_ROOT") or tempfile.gettempdir()
    PROFILE = tempfile.mkdtemp(prefix="paired-chrome.", dir=root)
    args = [
        CHROME,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1200,800",
        f"--proxy-server={proxy}",
        "--proxy-bypass-list=<-loopback>",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "about:blank",
    ]
    _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"chrome pid {_proc.pid} profile {PROFILE}", flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                v = json.load(r)
                print(f"chrome {v.get('Browser')}", flush=True)
                return v["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome did not expose CDP")


async def disconnect() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=15)
        except Exception:
            _proc.kill()
    _proc = None
