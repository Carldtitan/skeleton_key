"""Benchmark: validate the site's tasks, run each passable task several times through the race, report per lane.

Tasks come from control/app/bench_tasks/<domain>.json (question + oracle + UI pages). A task is only used if
the validator finds its oracle answer in the site's UI, so browser lanes are never scored on impossible tasks.
Results go to bench/ (gitignored: answers can contain the account owner's data).

Usage: python scripts/benchmark.py [runs_per_task] [domain]
"""
import collections
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
LANES = ("frontier_browser", "open_browser", "skeleton_key")
OUT = ROOT / "bench"


def run_race(client, domain, task):
    rid = client.post("/api/race", json={"domain": domain, "task": task}).json()["race_id"]
    while True:
        d = client.get(f"/api/race/{rid}").json()
        if d["status"] in ("done", "failed"):
            return rid, d
        time.sleep(4)


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    domain = sys.argv[2] if len(sys.argv) > 2 else "luma.com"
    OUT.mkdir(exist_ok=True)
    with httpx.Client(base_url=BASE, auth=AUTH, verify=certifi.where(), timeout=600) as client:
        validation = client.post(f"/api/bench/validate/{domain}").json()["tasks"]
        (OUT / "validation.json").write_text(json.dumps(validation, indent=1))
        for v in validation:
            print(f"validate {v['id']}: {'PASSABLE' if v.get('passable') else 'REJECTED'} - {v.get('why')}", flush=True)
        usable = [v["question"] for v in validation if v.get("passable")]

        rows = []
        for task in usable:
            for i in range(runs):
                rid, d = run_race(client, domain, task)
                for lane, r in ((d.get("result") or {}).get("results") or {}).items():
                    rows.append({"task": task, "run": i, "race": rid, "lane": lane, **{k: r.get(k) for k in (
                        "model", "seconds", "steps", "model_tokens", "cost_usd", "correct", "failure", "why",
                        "answer", "error")}})
                print(f"{rid} {task[:55]!r} run {i + 1}/{runs}: {d['status']}", flush=True)
                (OUT / "results.json").write_text(json.dumps(rows, indent=1))

    lanes = [l for l in LANES if any(r["lane"] == l for r in rows)]
    out = [f"Tasks used: {len(usable)} of {len(validation)} (rejected tasks are not passable in the UI)", "",
           "| lane | model | runs | success | median time | median cost | median tokens | failures |",
           "|---|---|---|---|---|---|---|---|"]
    for lane in lanes:
        rs = [r for r in rows if r["lane"] == lane]
        ok = sum(r["correct"] is True for r in rs)
        med = lambda k: statistics.median(r[k] or 0 for r in rs)
        fails = collections.Counter(r["failure"] for r in rs if r["correct"] is not True)
        out.append(f"| {lane} | {rs[0]['model']} | {len(rs)} | {ok}/{len(rs)} ({100 * ok // len(rs)}%) | "
                   f"{med('seconds'):.1f}s | ${med('cost_usd'):.4f} | {med('model_tokens'):,.0f} | "
                   f"{', '.join(f'{k}: {v}' for k, v in fails.items()) or '–'} |")
    out += ["", "| task | " + " | ".join(lanes) + " |", "|---|" + "---|" * len(lanes)]
    for task in usable:
        cells = []
        for lane in lanes:
            rs = [r for r in rows if r["task"] == task and r["lane"] == lane]
            cells.append(f"{sum(r['correct'] is True for r in rs)}/{len(rs)}")
        out.append(f"| {task} | " + " | ".join(cells) + " |")
    (OUT / "summary.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
