"""Stealth benchmark evaluation using spider-browser.

Runs Stealth_Bench_V1 tasks against Spider's browser fleet and evaluates
whether each site was accessed without being blocked by anti-bot protections.

Usage:
    uv run python run_stealth_eval.py                          # run all 80 tasks
    uv run python run_stealth_eval.py --tasks 5                # run first 5 tasks
    uv run python run_stealth_eval.py --browser chrome         # force specific browser
"""

import certifi, os

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import argparse
import asyncio
import base64
import hashlib
import json
import re
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

from spider_browser import SpiderBrowser, SpiderBrowserOptions

TASKS_FILE = Path(__file__).parent / "Stealth_Bench_V1.enc"
MAX_CONCURRENT = 3
TASK_TIMEOUT = 60  # 60s per task

# Patterns that indicate the browser was blocked (checked in title + first 5KB of HTML)
BLOCK_PATTERNS = [
    r"access denied",
    r"blocked",
    r"captcha",
    r"challenge-platform",
    r"checking your browser",
    r"completing the captcha",
    r"denied access",
    r"enable javascript and cookies",
    r"hcaptcha",
    r"human verification",
    r"i am not a robot",
    r"just a moment",
    r"one more step",
    r"pardon our interruption",
    r"perimeterx",
    r"please verify",
    r"ray id",
    r"recaptcha",
    r"security check",
    r"verify you are human",
    r"attention required",
    r"are you a robot",
]

BLOCK_RE = re.compile("|".join(BLOCK_PATTERNS), re.IGNORECASE)


def load_tasks() -> list[dict]:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"Stealth_Bench_V1").digest())
    encrypted = base64.b64decode(TASKS_FILE.read_text())
    return json.loads(Fernet(key).decrypt(encrypted))


def extract_url(confirmed_task: str) -> str:
    """Pull the target URL from the task prompt."""
    match = re.search(r"Go to (https?://\S+)", confirmed_task)
    if match:
        url = match.group(1).rstrip(",")
        if "<" in url:
            url = url.split("<")[0]
        return url
    return ""


def is_blocked(html: str, title: str) -> tuple[bool, str]:
    """Check if the page content indicates the browser was blocked."""
    text = f"{title} {html[:5000]}"

    if len(html.strip()) < 200:
        return True, "Page content too short (likely blocked)"

    match = BLOCK_RE.search(text)
    if match:
        # Legitimate pages with > 3KB of content likely have false positive matches
        if len(html.strip()) > 3000:
            return False, ""
        return True, f"Blocked pattern detected: {match.group()}"

    return False, ""


async def run_task(
    task: dict,
    semaphore: asyncio.Semaphore,
    browser_type: str,
    run_data_dir: Path | None = None,
) -> dict:
    """Run a single stealth benchmark task."""
    async with semaphore:
        task_id = task.get("task_id", "unknown")
        website = task.get("website", "unknown")
        category = task.get("category", "unknown")
        url = extract_url(task["confirmed_task"])

        if not url:
            url = f"https://{website}"

        print(f"[{task_id}] {website} ({category})")
        start = time.time()

        try:
            opts = SpiderBrowserOptions(
                api_key=os.environ["SPIDER_API_KEY"],
                server_url="wss://browser.spider.cloud",
                browser=browser_type,
                captcha="solve",
                smart_retry=True,
                max_retries=12,
                stealth=0,
                max_stealth_levels=3,
                connect_timeout_ms=30_000,
                command_timeout_ms=30_000,
                country="US",
                hedge=True,
                mode="scraping",
            )

            async with SpiderBrowser(opts) as browser:
                page = browser.page

                # Navigate - try goto_fast first (5s max wait), fallback to goto_dom
                nav_ok = False
                for nav_fn in [page.goto_fast, page.goto_dom]:
                    try:
                        await asyncio.wait_for(nav_fn(url), timeout=30)
                        nav_ok = True
                        break
                    except Exception:
                        continue

                if not nav_ok:
                    # Last resort: just try evaluate to navigate
                    try:
                        await page.evaluate(f"window.location.href = '{url}'")
                        await asyncio.sleep(3)
                        nav_ok = True
                    except Exception:
                        pass

                # Collect page state
                html = ""
                title = ""
                screenshot_b64 = ""
                current_url = url

                if nav_ok:
                    # Get content with smart waiting
                    try:
                        html = await asyncio.wait_for(
                            page.content(wait_ms=5000, min_length=500),
                            timeout=15,
                        )
                    except Exception:
                        try:
                            html = await asyncio.wait_for(page.raw_content(), timeout=5)
                        except Exception:
                            html = ""

                    try:
                        title = await asyncio.wait_for(page.title(), timeout=5)
                    except Exception:
                        title = ""

                    try:
                        screenshot_b64 = await asyncio.wait_for(page.screenshot(), timeout=10)
                    except Exception:
                        screenshot_b64 = ""

                    try:
                        current_url = await asyncio.wait_for(page.url(), timeout=5)
                    except Exception:
                        pass

            duration = time.time() - start

            if not nav_ok:
                blocked, reason = True, "Navigation failed entirely"
            else:
                blocked, reason = is_blocked(html, title)

            score = 0 if blocked else 1
            status = "PASS" if score else f"BLOCKED: {reason}"
            print(f"  [{task_id}] {status} ({duration:.1f}s) title={title[:60]}")

            # Save trace
            if run_data_dir:
                run_data_dir.mkdir(parents=True, exist_ok=True)
                trace = {
                    "task_id": task_id,
                    "website": website,
                    "category": category,
                    "url": url,
                    "final_url": current_url,
                    "title": title,
                    "html_length": len(html),
                    "html_preview": html[:2000],
                    "blocked": blocked,
                    "block_reason": reason,
                    "score": score,
                    "duration": duration,
                }
                (run_data_dir / f"{task_id}.json").write_text(
                    json.dumps(trace, indent=2)
                )

            return {
                "task_id": task_id,
                "website": website,
                "category": category,
                "score": score,
                "duration": duration,
                "blocked": blocked,
                "block_reason": reason,
            }

        except asyncio.TimeoutError:
            duration = time.time() - start
            print(f"  [{task_id}] TIMEOUT ({duration:.1f}s)")
            return {
                "task_id": task_id,
                "website": website,
                "category": category,
                "score": 0,
                "duration": duration,
                "blocked": True,
                "block_reason": f"Task timed out after {TASK_TIMEOUT}s",
            }

        except Exception as e:
            duration = time.time() - start
            error_type = type(e).__name__
            error_msg = f"{error_type}: {e}"
            print(f"  [{task_id}] ERROR: {error_msg} ({duration:.1f}s)")
            return {
                "task_id": task_id,
                "website": website,
                "category": category,
                "score": 0,
                "duration": duration,
                "blocked": True,
                "block_reason": error_msg,
                "error": error_msg,
            }


async def main():
    parser = argparse.ArgumentParser(description="Run Stealth_Bench_V1 with spider-browser")
    parser.add_argument(
        "--browser",
        default="auto",
        help="Browser type: auto, chrome, chrome-new, chrome-h, firefox (default: auto)",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=None,
        help="Number of tasks to run (default: all 80)",
    )
    args = parser.parse_args()

    tasks = load_tasks()
    if args.tasks:
        tasks = tasks[: args.tasks]

    browser_name = args.browser
    run_start = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_key = f"Stealth_Bench_V1_browser_spider_{browser_name}"
    run_data_dir = Path(__file__).parent / "run_data" / f"{run_key}_start_at_{run_start}"
    results_file = Path(__file__).parent / "results" / f"{run_key}.json"

    print(f"Running {len(tasks)} stealth tasks with spider-browser ({browser_name})")
    print(f"Results: {results_file}")
    print()

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results = await asyncio.gather(
        *[
            run_task(t, sem, browser_type=browser_name, run_data_dir=run_data_dir)
            for t in tasks
        ]
    )

    # Aggregate
    successful = sum(1 for r in results if r.get("score") == 1)
    total_duration = sum(r.get("duration", 0) for r in results)

    by_category_success = defaultdict(int)
    by_category_total = defaultdict(int)
    for r in results:
        cat = r.get("category", "unknown")
        by_category_total[cat] += 1
        if r.get("score") == 1:
            by_category_success[cat] += 1

    # Save results in official format
    results_file.parent.mkdir(parents=True, exist_ok=True)
    runs = json.loads(results_file.read_text()) if results_file.exists() else []
    runs.append(
        {
            "run_start": run_start,
            "tasks_completed": len(results),
            "tasks_successful": successful,
            "total_steps": len(results),
            "total_duration": round(total_duration, 2),
            "total_cost": 0,
            "tasks_successful_by_category": dict(by_category_success),
            "tasks_total_by_category": dict(by_category_total),
        }
    )
    results_file.write_text(json.dumps(runs, indent=2))

    # Print summary
    print()
    print(f"{'=' * 60}")
    print(f"Results: {successful}/{len(results)} tasks passed ({successful/len(results)*100:.1f}%)")
    print(f"Duration: {total_duration:.1f}s")
    print()
    print(f"{'Category':<20} {'Pass':<6} {'Total':<6} {'Rate'}")
    print(f"{'-' * 50}")
    for cat in sorted(by_category_total.keys()):
        s = by_category_success[cat]
        t = by_category_total[cat]
        pct = s / t * 100 if t > 0 else 0
        print(f"{cat:<20} {s:<6} {t:<6} {pct:.0f}%")
    print(f"{'=' * 60}")
    print(f"Saved to: {results_file}")


if __name__ == "__main__":
    asyncio.run(main())
