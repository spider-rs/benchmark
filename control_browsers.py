"""Read the same URLs through the real browsers installed on this machine.

The stealth bench scores a page the moment DOMContentLoaded fires. When a task
goes red the first question is whether the site rejects this machine's IP or
this client. This script answers it: it launches each installed browser on its
own CDP port and reads the URL twice with the bench's own fetch_page, once
exactly as the bench does and once after a settle wait, so a self-resolving
challenge shows up as "blocked at first read, page after the wait".

Reference rule: stable Google Chrome is the default reference. Chrome Canary
and Chrome-for-Testing have both given false leads before (a phantom TLS
extension, a different accept-language, and Canary fails DataDome sites that
Brave and Edge pass from the same IP), so they are opt-in. Headless Chrome
must be given a normal user agent or Akamai and DataDome deny it on sight.

Usage:
    uv run python control_browsers.py https://kohls.com https://g2.com
    uv run python control_browsers.py --browsers chrome,brave,edge --wait 20 URL...
    uv run python control_browsers.py --headless --browsers chrome URL...

Nothing here touches scoring. It imports is_blocked from run_stealth_eval so
the verdict column is the bench's verdict.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cdp_client import CDPClient  # noqa: E402
from run_stealth_cdp import HREF_JS, HTML_JS, TITLE_JS, _eval, fetch_page  # noqa: E402
from run_stealth_eval import is_blocked  # noqa: E402

MAC_APPS = {
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "opera": "/Applications/Opera.app/Contents/MacOS/Opera",
    "canary": "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
}
LINUX_BINS = {
    "chrome": ["google-chrome-stable", "google-chrome"],
    "brave": ["brave-browser", "brave"],
    "edge": ["microsoft-edge-stable", "microsoft-edge"],
    "opera": ["opera"],
    "chromium": ["chromium", "chromium-browser"],
}
DEFAULT_BROWSERS = ["chrome", "brave", "edge"]
BASE_PORT = 9460


def find_binary(name: str):
    env = os.environ.get(f"{name.upper()}_BIN")
    if env:
        return env
    if sys.platform == "darwin":
        p = MAC_APPS.get(name)
        return p if p and os.path.exists(p) else None
    for b in LINUX_BINS.get(name, []):
        p = shutil.which(b)
        if p:
            return p
    return None


def version_of(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"version failed: {e}"


def launch(binary: str, port: int, profile: str, headless: bool, ua: str | None):
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1200,800",
    ]
    if headless:
        args.append("--headless=new")
    if ua:
        args.append(f"--user-agent={ua}")
    args.append("about:blank")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_cdp(port: int, timeout: float = 20.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return None


def headless_ua(version: dict) -> str | None:
    """A headless build announces itself in the UA. Hand it the stable string."""
    ua = version.get("User-Agent", "")
    if "Headless" not in ua:
        return None
    return ua.replace("HeadlessChrome", "Chrome")


async def settled_read(cdp_url: str, url: str, wait: float) -> dict:
    cdp = await CDPClient.connect(cdp_url)
    ctx = tid = None
    try:
        try:
            ctx = (await cdp.send("Target.createBrowserContext", {})).get("browserContextId")
        except Exception:  # noqa: BLE001
            ctx = None
        params = {"url": "about:blank"}
        if ctx:
            params["browserContextId"] = ctx
        tid = (await cdp.send("Target.createTarget", params))["targetId"]
        sid = (await cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
        await cdp.send("Page.enable", session_id=sid)
        await cdp.send("Page.navigate", {"url": url}, session_id=sid, timeout=30)
        await asyncio.sleep(wait)
        html = await _eval(cdp, sid, HTML_JS) or ""
        title = await _eval(cdp, sid, TITLE_JS) or ""
        href = await _eval(cdp, sid, HREF_JS) or url
        return {"html": html, "title": title, "final_url": href}
    finally:
        try:
            if tid:
                await cdp.send("Target.closeTarget", {"targetId": tid}, timeout=10)
            if ctx:
                await cdp.send("Target.disposeBrowserContext", {"browserContextId": ctx}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        await cdp.close()


def verdict(page: dict) -> str:
    blocked, reason = is_blocked(page["html"], page["title"])
    if blocked:
        return f"BLOCK {reason.replace('Blocked pattern detected: ', '')[:28]}"
    return "PASS"


async def probe(name: str, cdp_url: str, urls: list[str], wait: float) -> list[dict]:
    rows = []
    for url in urls:
        row = {"browser": name, "url": url}
        try:
            first = await asyncio.wait_for(fetch_page(cdp_url, url), timeout=60)
            row["first"] = verdict(first)
            row["first_len"] = len(first["html"])
        except Exception as e:  # noqa: BLE001
            row["first"] = f"ERR {type(e).__name__}"
            row["first_len"] = 0
        try:
            later = await asyncio.wait_for(settled_read(cdp_url, url, wait), timeout=wait + 60)
            row["settled"] = verdict(later)
            row["settled_len"] = len(later["html"])
            row["title"] = later["title"][:60]
        except Exception as e:  # noqa: BLE001
            row["settled"] = f"ERR {type(e).__name__}"
            row["settled_len"] = 0
            row["title"] = ""
        rows.append(row)
        print(
            f"  {name:<8} {url[:32]:<32} first {row['first']:<34} {row['first_len']:>8}B"
            f"   after {wait:.0f}s {row['settled']:<34} {row['settled_len']:>8}B",
            flush=True,
        )
    return rows


async def main():
    ap = argparse.ArgumentParser(description="Read URLs through the real browsers on this host")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--browsers", default=",".join(DEFAULT_BROWSERS),
                    help="comma list from chrome,brave,edge,opera,canary,chromium (default: chrome,brave,edge)")
    ap.add_argument("--wait", type=float, default=20.0, help="settle seconds for the second read")
    ap.add_argument("--headless", action="store_true", help="launch headless; the UA is rewritten to the stable string")
    ap.add_argument("--json", help="write the rows to this file")
    args = ap.parse_args()

    public_ip = "unknown"
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            public_ip = r.read().decode().strip()
    except Exception:  # noqa: BLE001
        pass
    print(f"public ip {public_ip}")

    rows = []
    for i, name in enumerate([b.strip() for b in args.browsers.split(",") if b.strip()]):
        binary = find_binary(name)
        if not binary:
            print(f"  {name:<8} not installed (set {name.upper()}_BIN to point at it)")
            continue
        port = BASE_PORT + i
        profile = tempfile.mkdtemp(prefix=f"control-{name}-")
        proc = launch(binary, port, profile, args.headless, None)
        try:
            version = wait_for_cdp(port)
            if not version:
                print(f"  {name:<8} did not answer on {port}")
                continue
            ua = headless_ua(version)
            if ua:
                # Relaunch with the stable UA. A "HeadlessChrome" UA is denied
                # by Akamai and DataDome before any fingerprint runs.
                proc.terminate()
                proc.wait(timeout=10)
                proc = launch(binary, port, profile, args.headless, ua)
                version = wait_for_cdp(port) or version
            print(f"{name}: {version_of(binary)} | {version.get('User-Agent', '')}")
            rows.extend(await probe(name, version["webSocketDebuggerUrl"], args.urls, args.wait))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
            shutil.rmtree(profile, ignore_errors=True)

    if args.json:
        Path(args.json).write_text(json.dumps({"public_ip": public_ip, "rows": rows}, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
