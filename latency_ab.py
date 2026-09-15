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


async def interleave(base_cdp: str, branch_cdp: str, runs: int, out: str | None):
    """Alternate the two builds one navigation at a time.

    A block of base runs followed by a block of branch runs compares two
    builds and also two moments. On a machine shared with other work those
    moments differ by more than the code does: the same build measured twice
    an hour apart has read 0.8 s and 62 s here. Alternating puts both builds
    through the same minute, so a drift in load lands on both.
    """
    print(f"interleaved, {runs} navigations per url per build")
    print(f"  base   {revision(base_cdp)}")
    print(f"  branch {revision(branch_cdp)}")
    base_rows, branch_rows = [], []
    for url in URLS:
        b_times, c_times, b_err, c_err = [], [], 0, 0
        for _ in range(runs):
            for cdp, times in ((base_cdp, b_times), (branch_cdp, c_times)):
                t0 = time.time()
                try:
                    await asyncio.wait_for(fetch_page(cdp, url), timeout=60)
                    times.append((time.time() - t0) * 1000)
                except Exception:  # noqa: BLE001
                    if cdp == base_cdp:
                        b_err += 1
                    else:
                        c_err += 1
        row = lambda t, e: {"url": url, "n": len(t), "errors": e,  # noqa: E731
                            "p50_ms": round(pct(t, 50), 1), "p95_ms": round(pct(t, 95), 1),
                            "median_bytes": 0}
        base_rows.append(row(b_times, b_err))
        branch_rows.append(row(c_times, c_err))
        b, c = base_rows[-1], branch_rows[-1]
        d50 = (c["p50_ms"] - b["p50_ms"]) / b["p50_ms"] * 100 if b["p50_ms"] else 0
        d95 = (c["p95_ms"] - b["p95_ms"]) / b["p95_ms"] * 100 if b["p95_ms"] else 0
        print(f"  {url[:46]:<48} base p50 {b['p50_ms']:>7.0f} p95 {b['p95_ms']:>7.0f}"
              f" | branch p50 {c['p50_ms']:>7.0f} p95 {c['p95_ms']:>7.0f} | d50 {d50:+6.1f}% d95 {d95:+6.1f}%")
    worst = 0.0
    for b, c in zip(base_rows, branch_rows):
        if b["p95_ms"]:
            worst = max(worst, (c["p95_ms"] - b["p95_ms"]) / b["p95_ms"] * 100)
    print()
    print(f"worst p95 regression {worst:+.1f}% -> {'PASS' if worst <= 5 else 'FAIL'} (the bar is 5%)")
    if out:
        Path(out).write_text(json.dumps({"mode": "interleave", "base": base_rows, "branch": branch_rows}, indent=2))
        print(f"wrote {out}")
    return 0 if worst <= 5 else 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp")
    ap.add_argument("--label", default="run")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "BRANCH"))
    ap.add_argument("--interleave", nargs=2, metavar=("BASE_CDP", "BRANCH_CDP"),
                    help="alternate the two builds navigation by navigation")
    args = ap.parse_args()

    if args.compare:
        sys.exit(compare(*args.compare))
    if args.interleave:
        sys.exit(await interleave(args.interleave[0], args.interleave[1], args.runs, args.json))
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
