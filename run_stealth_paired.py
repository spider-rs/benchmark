"""Stealth_Bench_V1 paired reader: one visit per task, two reads.

OLD read: byte-for-byte the run_stealth_cdp.fetch_page timing (DOMContentLoaded,
then the SETTLE_MAX / MIN_LENGTH settle loop), scored with the untouched
is_blocked(). Its total is the number the bench reports today.

LATE read: same visit, same tab. If the old read looks like a challenge, keep
dwelling until the top document has seen no new top-level navigation and a
stable outerHTML length for STABLE_S, and no longer looks like a challenge, capped
at LATE_CAP_S from navigation start. Otherwise the late read is the old read.
Scored with is_blocked_v2().

Usage:
    python run_stealth_paired.py --browser local_headful_proxy --arm chrome-e9
    python run_stealth_paired.py --browser navi --arm navi-e9 [--only crocs.com,g2.com]
"""

import certifi, os

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import argparse
import asyncio
import importlib
import json
import time
from datetime import datetime
from pathlib import Path

from cdp_client import CDPClient, CDPError
from run_stealth_cdp import (
    HREF_JS,
    HTML_JS,
    INJECT_SRC,
    MIN_LENGTH,
    NAV_TIMEOUT,
    NET_BACKOFF,
    NET_RETRIES,
    SETTLE_MAX,
    SETTLE_STEP,
    TITLE_JS,
    NetworkError,
    _eval,
    content_blocked,
    load_average,
    title_challenged,
)
from run_stealth_eval import MAX_CONCURRENT, TASK_TIMEOUT, extract_url, is_blocked, load_tasks
from stealth_v2 import is_blocked_v2, looks_like_challenge, visible_text

LATE_CAP_S = 30.0
STABLE_S = 3.0
POLL_S = 0.5
LEN_JS = "(document.documentElement ? document.documentElement.outerHTML.length : 0) + '|' + document.readyState"


class VisitWatch:
    """Top-frame navigations, load events and Document responses for one session."""

    def __init__(self, cdp, session_id, t0_ref):
        self.q = cdp.events()
        self.cdp = cdp
        self.sid = session_id
        self.t0 = t0_ref
        self.top = None
        self.navs = []
        self.loads = []
        self.docs = {}  # frameId -> list of (t, status, url)
        self.last_nav = 0.0
        self.task = asyncio.create_task(self._run())

    def rel(self):
        return round(time.time() - self.t0[0], 3) if self.t0[0] else 0.0

    async def _run(self):
        while True:
            ev = await self.q.get()
            if ev.get("sessionId") != self.sid:
                continue
            m = ev.get("method")
            p = ev.get("params", {})
            if m == "Page.frameNavigated":
                fr = p.get("frame", {})
                if not fr.get("parentId"):
                    self.top = fr.get("id")
                    self.last_nav = time.time()
                    self.navs.append({
                        "t": self.rel(),
                        "url": (fr.get("url") or "")[:300],
                        "unreachableUrl": (fr.get("unreachableUrl") or "")[:300],
                    })
            elif m == "Page.loadEventFired":
                self.loads.append(self.rel())
            elif m == "Network.responseReceived" and p.get("type") == "Document":
                r = p.get("response", {})
                self.docs.setdefault(p.get("frameId"), []).append(
                    (self.rel(), r.get("status"), (r.get("url") or "")[:300])
                )

    def top_status(self, frame_hint=None):
        fid = self.top or frame_hint
        lst = self.docs.get(fid) or []
        return lst[-1][1] if lst else None

    def close(self):
        self.task.cancel()
        self.cdp.unsubscribe(self.q)


def snapshot(html, title, final_url, status, t, err=None):
    return {
        "t": round(t, 3),
        "html": html,
        "title": title,
        "final_url": final_url,
        "status": status,
        "error": err,
    }


async def fetch_page_paired(cdp_url: str, url: str) -> dict:
    cdp = await CDPClient.connect(cdp_url)
    ctx_id = target_id = None
    watch = None
    try:
        # ---- setup: identical to run_stealth_cdp.fetch_page ----
        try:
            ctx = await cdp.send("Target.createBrowserContext", {}, timeout=15)
            ctx_id = ctx.get("browserContextId")
        except Exception:
            ctx_id = None
        params = {"url": "about:blank"}
        if ctx_id:
            params["browserContextId"] = ctx_id
        try:
            target_id = (await cdp.send("Target.createTarget", params))["targetId"]
        except Exception:
            targets = (await cdp.send("Target.getTargets", {}))["targetInfos"]
            page = next(t for t in targets if t["type"] == "page")
            target_id = page["targetId"]
            ctx_id = None
        session_id = (
            await cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        )["sessionId"]

        # The watcher subscribes before Page.enable so it sees every event of the
        # visit. It only listens; it sends nothing, so the old read is unchanged.
        t0 = [0.0]
        watch = VisitWatch(cdp, session_id, t0)

        await cdp.send("Page.enable", session_id=session_id)
        await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=session_id)
        await cdp.send("Network.enable", session_id=session_id)
        if INJECT_SRC:
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": INJECT_SRC}, session_id=session_id
            )

        # ---- OLD read: identical to run_stealth_cdp.fetch_page ----
        old = None
        nav = {}
        q = cdp.events()
        try:
            t0[0] = time.time()
            nav = await cdp.send(
                "Page.navigate", {"url": url}, session_id=session_id, timeout=NAV_TIMEOUT
            )
            if nav.get("errorText"):
                raise NetworkError(nav["errorText"])
            deadline = time.time() + NAV_TIMEOUT
            main_requests: set = set()
            while time.time() < deadline:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                if ev.get("sessionId") != session_id:
                    continue
                params = ev.get("params", {})
                if ev.get("method") == "Page.lifecycleEvent" and params.get("name") == "DOMContentLoaded":
                    break
                if (
                    ev.get("method") == "Network.requestWillBeSent"
                    and params.get("type") == "Document"
                    and params.get("frameId") in (None, nav.get("frameId"))
                ):
                    main_requests.add(params.get("requestId"))
                if (
                    ev.get("method") == "Network.loadingFailed"
                    and params.get("type") == "Document"
                    and not params.get("canceled")
                    and (params.get("requestId") in main_requests or not main_requests)
                ):
                    raise NetworkError(params.get("errorText") or "net::ERR_FAILED")
        finally:
            cdp.unsubscribe(q)

        frame_hint = nav.get("frameId")
        try:
            html = ""
            settle_deadline = time.time() + SETTLE_MAX
            while True:
                html = await _eval(cdp, session_id, HTML_JS) or ""
                if len(html) >= MIN_LENGTH or time.time() >= settle_deadline:
                    break
                await asyncio.sleep(SETTLE_STEP)
            title = await _eval(cdp, session_id, TITLE_JS) or ""
            final_url = await _eval(cdp, session_id, HREF_JS) or url
            old = snapshot(html, title, final_url, watch.top_status(frame_hint), time.time() - t0[0])
        except (CDPError, Exception) as e:  # noqa: B014
            # In run_stealth_cdp this exception ends the task as a block. Record it
            # the same way for the old read, and still take the late read.
            old = snapshot("", "", url, watch.top_status(frame_hint), time.time() - t0[0],
                           err=f"{type(e).__name__}: {e}")

        # ---- LATE read ----
        dwell = looks_like_challenge(old["html"], old["title"], old["final_url"], old["status"]) or bool(old["error"])
        stop_reason = "old read not a challenge"
        if not dwell:
            late = dict(old)
        else:
            cap_at = t0[0] + LATE_CAP_S
            prev_len = -1
            last_change = time.time()
            checked_key = None
            stop_reason = "cap"
            while time.time() < cap_at:
                now = time.time()
                try:
                    v = await _eval(cdp, session_id, LEN_JS, timeout=5.0) or "0|loading"
                    ln_s, rs = str(v).split("|", 1)
                    ln = int(ln_s)
                except Exception:
                    ln, rs = -1, "loading"
                if ln != prev_len:
                    prev_len = ln
                    last_change = now
                quiet_since = max(last_change, watch.last_nav)
                if now - quiet_since >= STABLE_S and rs != "loading" and ln > 0:
                    key = (len(watch.navs), ln)
                    if key != checked_key:
                        checked_key = key
                        try:
                            h = await _eval(cdp, session_id, HTML_JS) or ""
                            ti = await _eval(cdp, session_id, TITLE_JS) or ""
                            hr = await _eval(cdp, session_id, HREF_JS) or url
                        except Exception:
                            h, ti, hr = "", "", url
                        if h and not looks_like_challenge(h, ti, hr, watch.top_status(frame_hint)):
                            stop_reason = "stable non-challenge"
                            break
                await asyncio.sleep(POLL_S)
            try:
                h = await _eval(cdp, session_id, HTML_JS) or ""
                ti = await _eval(cdp, session_id, TITLE_JS) or ""
                hr = await _eval(cdp, session_id, HREF_JS) or url
                late = snapshot(h, ti, hr, watch.top_status(frame_hint), time.time() - t0[0])
            except Exception as e:  # noqa: BLE001
                late = snapshot("", "", url, watch.top_status(frame_hint), time.time() - t0[0],
                                err=f"{type(e).__name__}: {e}")

        return {
            "old": old,
            "late": late,
            "dwelled": dwell,
            "stop_reason": stop_reason,
            "top_navs": list(watch.navs),
            "load_events": list(watch.loads),
            "top_docs": watch.docs.get(watch.top or frame_hint, []),
        }
    finally:
        if watch:
            watch.close()
        try:
            if target_id:
                await cdp.send("Target.closeTarget", {"targetId": target_id}, timeout=10)
            if ctx_id:
                await cdp.send("Target.disposeBrowserContext", {"browserContextId": ctx_id}, timeout=10)
        except Exception:
            pass
        await cdp.close()


async def fetch_paired_retrying(cdp_url, url):
    last = None
    for attempt in range(NET_RETRIES + 1):
        try:
            return await fetch_page_paired(cdp_url, url)
        except NetworkError as e:
            last = e
            if attempt < NET_RETRIES:
                await asyncio.sleep(NET_BACKOFF * (attempt + 1))
    raise last


def describe(s):
    return {
        "t": s["t"],
        "final_url": s["final_url"][:300],
        "status": s["status"],
        "title": s["title"][:200],
        "html_length": len(s["html"]),
        "visible_text_length": len(visible_text(s["html"])),
        "error": s["error"],
    }


async def run_task(task, sem, cdp_url, out_dir):
    async with sem:
        task_id = task.get("task_id", "unknown")
        website = task.get("website", "unknown")
        category = task.get("category", "unknown")
        url = extract_url(task["confirmed_task"]) or f"https://{website}"
        start = time.time()
        rec = {"task_id": task_id, "website": website, "category": category, "url": url}
        try:
            r = await asyncio.wait_for(
                fetch_paired_retrying(cdp_url, url), timeout=TASK_TIMEOUT + LATE_CAP_S + 20
            )
            old, late = r["old"], r["late"]
            if old["error"]:
                ob, orsn = True, old["error"]
            elif old["t"] > TASK_TIMEOUT:
                ob, orsn = True, f"Task timed out after {TASK_TIMEOUT}s"
            else:
                ob, orsn = is_blocked(old["html"], old["title"])
            if late["error"]:
                nb, nrsn = True, late["error"]
            else:
                nb, nrsn = is_blocked_v2(late["html"], late["title"], late["final_url"], late["status"])
            rec.update({
                "old": describe(old), "late": describe(late),
                "dwelled": r["dwelled"], "stop_reason": r["stop_reason"],
                "top_navs": r["top_navs"], "load_events": r["load_events"], "top_docs": r["top_docs"],
                "old_strict_score": 0 if (ob or title_challenged(old["title"])) else 1,
                "old_content_score": 0 if (ob or content_blocked(old["html"], old["title"])) else 1,
            })
            if out_dir:
                (out_dir / "html").mkdir(parents=True, exist_ok=True)
                (out_dir / "html" / f"{task_id}.old.html").write_text(old["html"])
                (out_dir / "html" / f"{task_id}.late.html").write_text(late["html"])
        except asyncio.TimeoutError:
            ob, orsn = True, "paired visit timed out"
            nb, nrsn = True, "paired visit timed out"
        except NetworkError as e:
            ob, orsn = True, f"NETWORK {e}"
            nb, nrsn = True, f"NETWORK {e}"
        except Exception as e:  # noqa: BLE001
            ob, orsn = True, f"{type(e).__name__}: {e}"
            nb, nrsn = True, f"{type(e).__name__}: {e}"
        rec.update({
            "old_score": 0 if ob else 1, "old_reason": orsn,
            "new_score": 0 if nb else 1, "new_reason": nrsn,
            "differs": ob != nb,
            "duration": round(time.time() - start, 2),
        })
        if out_dir:
            (out_dir / f"{task_id}.json").write_text(json.dumps(rec, indent=2))
        return rec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="navi")
    ap.add_argument("--arm", default="bare")
    ap.add_argument("--tasks", type=int, default=None)
    ap.add_argument("--only", default="", help="comma list of website names to run")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENT)
    ap.add_argument("--out", default=None, help="output dir (default run_data/paired_<arm>_<ts>)")
    args = ap.parse_args()

    provider = importlib.import_module(f"browsers.{args.browser}")
    cdp_url = await provider.connect()
    try:
        tasks = load_tasks()
        if args.only:
            want = {w.strip() for w in args.only.split(",") if w.strip()}
            tasks = [t for t in tasks if t.get("website") in want or t.get("task_id") in want]
        if args.tasks:
            tasks = tasks[: args.tasks]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(args.out) if args.out else Path(__file__).parent / "run_data" / f"paired_{args.browser}-{args.arm}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"paired reader | provider={args.browser} arm={args.arm} | {len(tasks)} tasks | concurrency {args.concurrency} | load {load_average()}")
        print(f"out: {out_dir}", flush=True)
        sem = asyncio.Semaphore(args.concurrency)
        results = []
        for coro in asyncio.as_completed([run_task(t, sem, cdp_url, out_dir) for t in tasks]):
            r = await coro
            results.append(r)
            o = "PASS " if r["old_score"] else "BLOCK"
            n = "PASS " if r["new_score"] else "BLOCK"
            flag = " FLIP" if r["differs"] else ""
            lt = r.get("late", {})
            print(f"  old {o} new {n}{flag:5} {r['website'][:28]:<28} {r['duration']:5.1f}s "
                  f"late[{lt.get('status')} {lt.get('html_length')}B {str(lt.get('title',''))[:40]!r}] {r['new_reason'][:50]}",
                  flush=True)
        b2p = [r for r in results if r["old_score"] == 0 and r["new_score"] == 1]
        p2b = [r for r in results if r["old_score"] == 1 and r["new_score"] == 0]
        summary = {
            "arm": args.arm, "browser": args.browser, "run_start": ts, "tasks": len(results),
            "concurrency": args.concurrency, "load_average_end": load_average(),
            "old_total": sum(r["old_score"] for r in results),
            "new_total": sum(r["new_score"] for r in results),
            "old_strict_total": sum(r.get("old_strict_score", 0) for r in results),
            "old_content_total": sum(r.get("old_content_score", 0) for r in results),
            "block_to_pass": [{"website": r["website"], "old_reason": r["old_reason"], "late": r.get("late")} for r in b2p],
            "pass_to_block": [{"website": r["website"], "new_reason": r["new_reason"], "old": r.get("old"), "late": r.get("late")} for r in p2b],
            "blocked_both": [{"website": r["website"], "old_reason": r["old_reason"], "new_reason": r["new_reason"], "late": r.get("late")} for r in results if r["old_score"] == 0 and r["new_score"] == 0],
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"\nOLD {summary['old_total']}/{len(results)}  NEW {summary['new_total']}/{len(results)}")
        print("BLOCK->PASS:", [r["website"] for r in b2p])
        print("PASS->BLOCK:", [r["website"] for r in p2b])
    finally:
        await provider.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
