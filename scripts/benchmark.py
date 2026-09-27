"""Benchmark: run each task several times through the race and report per-lane medians.

Results go to bench/ (gitignored: answers can contain the account owner's data).
Usage: python scripts/benchmark.py [runs_per_task]
"""
import json
import pathlib
import statistics
import sys
import time

import certifi
import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
BASE = "https://45-76-254-85.sslip.io"  # straight to the Vultr control plane
AUTH = ("admin", ENV["SK_ADMIN_PASSWORD"])
DOMAIN = "luma.com"
TASKS = [
    "What are the first 3 event categories on Luma's discover page?",
    "How many events are in the AI category?",
    "Which calendars do I follow on Luma?",
    "Do I have any upcoming events? Answer yes or no and name them.",
    "Name one upcoming event in the Tech category and when it starts.",
]
OUT = ROOT / "bench"


def run_race(client, task):
    rid = client.post("/api/race", json={"domain": DOMAIN, "task": task}).json()["race_id"]
    while True:
        d = client.get(f"/api/race/{rid}").json()
        if d["status"] in ("done", "failed"):
            return rid, d
        time.sleep(4)


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    OUT.mkdir(exist_ok=True)
    rows = []
    with httpx.Client(base_url=BASE, auth=AUTH, verify=certifi.where(), timeout=60) as client:
        for task in TASKS:
            for i in range(runs):
                rid, d = run_race(client, task)
                for lane, r in ((d.get("result") or {}).get("results") or {}).items():
                    rows.append({"task": task, "run": i, "race": rid, "lane": lane, **{k: r.get(k) for k in (
                        "model", "seconds", "steps", "model_tokens", "cost_usd", "correct", "answer", "error")}})
                print(f"{rid} {task[:50]!r} run {i + 1}/{runs}: {d['status']}", flush=True)
                (OUT / "results.json").write_text(json.dumps(rows, indent=1))

    lines = ["| lane | model | runs | success | median time | median tokens | median cost |", "|---|---|---|---|---|---|---|"]
    for lane in ("frontier_browser", "open_browser", "skeleton_key"):
        rs = [r for r in rows if r["lane"] == lane]
        if not rs:
            continue
        ok = sum(r["correct"] is True for r in rs)
        med = lambda k: statistics.median(r[k] or 0 for r in rs)
        lines.append(f"| {lane} | {rs[0]['model']} | {len(rs)} | {ok}/{len(rs)} ({100 * ok // len(rs)}%) | "
                     f"{med('seconds'):.1f}s | {med('model_tokens'):,.0f} | ${med('cost_usd'):.4f} |")
    per_task = ["", "| task | frontier | open browser | skeleton key |", "|---|---|---|---|"]
    for task in TASKS:
        cells = []
        for lane in ("frontier_browser", "open_browser", "skeleton_key"):
            rs = [r for r in rows if r["task"] == task and r["lane"] == lane]
            cells.append(f"{sum(r['correct'] is True for r in rs)}/{len(rs)}" if rs else "–")
        per_task.append(f"| {task} | " + " | ".join(cells) + " |")
    (OUT / "summary.md").write_text("\n".join(lines + per_task) + "\n")
    print("\n".join(lines + per_task))


if __name__ == "__main__":
    main()
