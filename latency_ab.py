"""Wall-clock A/B of two navi builds over pages that carry no challenge.

The stealth work changes code that every navigation touches, so the question
a challenge fix has to answer is what an ordinary page pays. This measures
that: the same URLs, the same read the bench performs, N navigations each,
reported as p50 and p95 per URL per build.

It never touches a bench challenge site. Those are rate limited and scored
elsewhere; mixing them in would spend budget and add the variance of an
anti-bot decision to a latency number.

Usage:
    uv run python latency_ab.py --label base --cdp ws://127.0.0.1:9404/devtools/browser
    uv run python latency_ab.py --label branch --cdp ws://...  --runs 10 --json out.json

Compare two saved runs:
    uv run python latency_ab.py --compare base.json branch.json
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from run_stealth_cdp import fetch_page  # noqa: E402

URLS = [
    "https://example.com",
    "https://www.wikipedia.org",
    "https://github.com",
    "https://news.ycombinator.com",
    "https://developer.mozilla.org/en-US/docs/Web/API/Document",
    "https://stackoverflow.com/questions",
]


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


async def measure(cdp_url: str, url: str, runs: int) -> dict:
    times, sizes, errors = [], [], 0
    for _ in range(runs):
        t0 = time.time()
        try:
            page = await asyncio.wait_for(fetch_page(cdp_url, url), timeout=60)
            times.append((time.time() - t0) * 1000)
            sizes.append(len(page["html"]))
        except Exception:  # noqa: BLE001
            errors += 1
    return {
        "url": url,
        "n": len(times),
        "errors": errors,
        "p50_ms": round(pct(times, 50), 1),
        "p95_ms": round(pct(times, 95), 1),
        "median_bytes": int(statistics.median(sizes)) if sizes else 0,
    }


def revision(cdp_url: str) -> str:
    http = cdp_url.replace("ws://", "http://").split("/devtools")[0]
    try:
        with urllib.request.urlopen(f"{http}/json/version", timeout=5) as r:
            d = json.load(r)
            return f"{d.get('navi_revision', '?')[:8]} v{d.get('navi_version', '?')}"
    except Exception:  # noqa: BLE001
        return "unknown"


def compare(base_path: str, branch_path: str):
    base = {r["url"]: r for r in json.loads(Path(base_path).read_text())["rows"]}
    branch = {r["url"]: r for r in json.loads(Path(branch_path).read_text())["rows"]}
    print(f"{'url':<52}{'base p50':>10}{'br p50':>9}{'d%':>7}{'base p95':>10}{'br p95':>9}{'d%':>7}")
    worst = 0.0
    for url in base:
        if url not in branch:
            continue
        b, c = base[url], branch[url]
        d50 = (c["p50_ms"] - b["p50_ms"]) / b["p50_ms"] * 100 if b["p50_ms"] else 0
        d95 = (c["p95_ms"] - b["p95_ms"]) / b["p95_ms"] * 100 if b["p95_ms"] else 0
        worst = max(worst, d95)
        print(f"{url[:50]:<52}{b['p50_ms']:>10.0f}{c['p50_ms']:>9.0f}{d50:>+7.1f}"
              f"{b['p95_ms']:>10.0f}{c['p95_ms']:>9.0f}{d95:>+7.1f}")
    print()
    verdict = "PASS" if worst <= 5 else "FAIL"
    print(f"worst p95 regression {worst:+.1f}% -> {verdict} (the bar is 5%)")
    return 0 if worst <= 5 else 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp")
    ap.add_argument("--label", default="run")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "BRANCH"))
    args = ap.parse_args()

    if args.compare:
        sys.exit(compare(*args.compare))
    if not args.cdp:
        ap.error("--cdp is required unless --compare is given")

    rev = revision(args.cdp)
    print(f"{args.label}: {rev}, {args.runs} navigations per url")
    rows = []
    for url in URLS:
        row = await measure(args.cdp, url, args.runs)
        rows.append(row)
        print(f"  {url[:50]:<52} p50 {row['p50_ms']:>7.0f} ms   p95 {row['p95_ms']:>7.0f} ms"
              f"   {row['median_bytes']:>9} B   errors {row['errors']}")
    if args.json:
        Path(args.json).write_text(json.dumps({"label": args.label, "revision": rev, "rows": rows}, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
