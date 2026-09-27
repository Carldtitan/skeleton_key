"""Draft demo slides (faster / cheaper / more accurate) from bench/results.json."""
import json
import pathlib
import statistics as st
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "slides"
d = json.load(open(ROOT / "bench/results.json"))
tasks = json.load(open(ROOT / "control/app/bench_tasks/luma.com.json", encoding="utf-8"))
order = [t for t in tasks if any(r["task"] == t["question"] for r in d)]
SHORT = {"categories_first3": "First 3 categories", "crypto_event_count": "Crypto event count",
         "ai_subscribers": "AI subscribers", "crypto_vs_climate": "Crypto vs Climate",
         "my_upcoming_events": "My upcoming events", "calendars_followed": "Calendars I follow",
         "most_subscribed_of_five": "Top of 5 categories", "most_subscribed_overall": "Most-subscribed category",
         "top2_subscribed": "Top 2 categories", "profile_name_bio": "My profile name & bio",
         "categories_under_600": "Categories under 600 events", "new_york_events": "New York events"}
g = defaultdict(list)
for r in d:
    g[(r["task"], r["lane"])].append(r)


def med(q, lane, k):
    return st.median(r[k] for r in g[(q, lane)])


def mean(qs, lane, k):
    return st.mean(r[k] for q in qs for r in g[(q, lane)])


BR, SK = "#2f76b0", "#b85632"
INK, INK2, MUTED, RULE = "#23150f", "#3d281f", "#7a5c4b", "#e3d6c8"
CSS = f"""*{{box-sizing:border-box}}
body{{margin:0;width:1920px;height:1080px;background:#f8f2e8;font-family:Inter,Helvetica,Arial,sans-serif;color:{INK}}}
.s{{padding:84px 120px 60px;height:100%;display:flex;flex-direction:column}}
.eb{{font-size:22px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#914126}}
h1{{font-size:84px;margin:14px 0 10px;letter-spacing:-.03em;line-height:1}}
.sub{{font-size:30px;color:#614537;margin:0 0 36px}}
.leg{{display:flex;gap:40px;font-size:24px;color:{INK2};margin-bottom:24px}}
.leg i{{display:inline-block;width:18px;height:18px;border-radius:4px;margin-right:10px;vertical-align:-2px}}
.foot{{margin-top:auto;font-size:18px;color:{MUTED}}}
svg text{{font-family:Inter,Helvetica,Arial,sans-serif}}"""
LEGEND = (f'<div class=leg><span><i style="background:{BR}"></i>Claude Opus 5 · vision browser</span>'
          f'<span><i style="background:{SK}"></i>Claude Opus 5 · Skeleton Key (no vision)</span></div>')


def page(name, body):
    (OUT / name).write_text(f"<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head>"
                            f"<body><div class=s>{body}</div></body></html>", encoding="utf-8")


# 1. Faster: seconds per task, paired horizontal bars.
rows = [(SHORT[t["id"]], med(t["question"], "frontier_browser", "seconds"),
         med(t["question"], "frontier_skeleton_key", "seconds")) for t in order]
W, rowh, x0 = 1680, 52, 430
mx = max(r[1] for r in rows)
sx = lambda v: (W - x0 - 130) * v / mx
svg = [f'<svg width="{W}" height="{len(rows) * rowh + 10}">']
for i, (n, b, s) in enumerate(rows):
    y = i * rowh + 6
    svg.append(f'<text x="{x0 - 22}" y="{y + 25}" text-anchor="end" font-size="22" fill="{INK2}">{n}</text>')
    svg.append(f'<rect x="{x0}" y="{y}" width="{sx(b):.0f}" height="17" rx="4" fill="{BR}"/>'
               f'<text x="{x0 + sx(b) + 10:.0f}" y="{y + 15}" font-size="19" fill="{INK2}">{b:.1f}s</text>')
    svg.append(f'<rect x="{x0}" y="{y + 21}" width="{max(sx(s), 4):.0f}" height="17" rx="4" fill="{SK}"/>'
               f'<text x="{x0 + sx(s) + 10:.0f}" y="{y + 36}" font-size="19" font-weight="700" fill="{INK}">{s:.1f}s</text>')
svg.append("</svg>")
wins = sum(1 for t in order for b, s in zip(g[(t["question"], "frontier_browser")], g[(t["question"], "frontier_skeleton_key")])
           if s["seconds"] < b["seconds"])
hard = [t["question"] for t in order if t["difficulty"] == "hard"]
page("1_faster.html", f"""<span class=eb>Faster</span><h1>Faster on every task.</h1>
<p class=sub>Skeleton Key won {wins} of {len(order) * 3} runs. Long tasks: {st.median(r['seconds'] for q in hard for r in g[(q, 'frontier_browser')]):.1f} s → {st.median(r['seconds'] for q in hard for r in g[(q, 'frontier_skeleton_key')]):.1f} s median.</p>
{LEGEND}{''.join(svg)}
<div class=foot>DRAFT · Median seconds per task, 3 runs each, 12 Luma tasks. Same model and same logged-in account in both lanes.</div>""")

# 2. Cheaper: long (multi-step) tasks only, tokens per question, paired columns.
rows = [(SHORT[t["id"]], med(t["question"], "frontier_browser", "model_tokens"),
         med(t["question"], "frontier_skeleton_key", "model_tokens")) for t in order if t["difficulty"] == "hard"]
tb, ts = mean(hard, "frontier_browser", "model_tokens"), mean(hard, "frontier_skeleton_key", "model_tokens")
cb, cs = mean(hard, "frontier_browser", "cost_usd"), mean(hard, "frontier_skeleton_key", "cost_usd")
H, colw, gap = 560, 220, 56
mx = max(r[1] for r in rows)
sy = lambda v: (H - 90) * v / mx
svg = ['<svg width="1680" height="650">']
for i, (n, b, s) in enumerate(rows):
    x = 30 + i * (colw + gap)
    for j, (v, c) in enumerate(((b, BR), (s, SK))):
        bx, bw, h = x + j * (colw / 2 + 3), colw / 2 - 3, sy(v)
        svg.append(f'<rect x="{bx:.0f}" y="{H - h:.0f}" width="{bw:.0f}" height="{h:.0f}" rx="4" fill="{c}"/>')
        svg.append(f'<text x="{bx + bw / 2:.0f}" y="{H - h - 12:.0f}" text-anchor="middle" font-size="21" '
                   f'font-weight="{700 if j else 400}" fill="{INK}">{v / 1000:.1f}k</text>')
    svg.append(f'<text x="{x + colw / 2:.0f}" y="{H + 38}" text-anchor="middle" font-size="20" fill="{INK2}">{n}</text>')
svg.append(f'<line x1="20" x2="1660" y1="{H}" y2="{H}" stroke="#c8b3a3" stroke-width="2"/></svg>')
page("2_cheaper.html", f"""<span class=eb>Cheaper</span><h1>{cb / cs:.0f}× cheaper on long tasks.</h1>
<p class=sub>{tb / ts:.1f}× fewer tokens per question ({tb:,.0f} → {ts:,.0f}). Cost per question ${cb:.3f} → ${cs:.3f}.</p>
{LEGEND}{''.join(svg)}
<div class=foot>DRAFT · Tokens per question (median of 3 runs) on the 6 multi-step Luma tasks. Averages across all runs in the headline.</div>""")

# 3. More accurate: Qwen alone vs Qwen + Skeleton Key.
qb = sum(r["correct"] for r in d if r["lane"] == "open_browser")
qs = sum(r["correct"] for r in d if r["lane"] == "skeleton_key")
n = sum(1 for r in d if r["lane"] == "skeleton_key")
cells = []
for t in order:
    b = sum(r["correct"] for r in g[(t["question"], "open_browser")])
    s = sum(r["correct"] for r in g[(t["question"], "skeleton_key")])
    cells.append(f'<tr><td>{SHORT[t["id"]]}</td><td class="{"bad" if b < 3 else ""}">{b}/3</td><td>{s}/3</td></tr>')
page("3_accurate.html", f"""<style>.two{{display:grid;grid-template-columns:520px 1fr;gap:90px;align-items:start}}
.big{{font-size:150px;font-weight:800;letter-spacing:-.04em;line-height:1}}.lab{{font-size:26px;color:#614537;margin:8px 0 48px}}
table{{border-collapse:collapse;font-size:23px;width:100%}}td,th{{padding:8px 16px;border-bottom:1px solid {RULE};text-align:left}}
th{{font-size:17px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED}}}
td+td,th+th{{text-align:center;width:220px}}td.bad{{color:#9a3b28;font-weight:800}}</style>
<span class=eb>More accurate</span><h1>Same model. More right answers.</h1>
<p class=sub>Qwen 3.8 (27B) alone, browsing with vision, vs the same model with Skeleton Key.</p>
<div class=two><div><div class=big style="color:{BR}">{qb}/{n}</div><div class=lab>Qwen alone · vision browser</div>
<div class=big style="color:{SK}">{qs}/{n}</div><div class=lab>Qwen + Skeleton Key · no vision</div></div>
<table><tr><th>Task</th><th>Qwen alone</th><th>+ Skeleton Key</th></tr>{''.join(cells)}</table></div>
<div class=foot>DRAFT · 12 Luma tasks × 3 runs, each answer graded against the site's real data.</div>""")
print("faster wins", wins, "| cheaper", round(cb / cs, 1), "x cost,", round(tb / ts, 1), "x tokens | accurate", qb, "vs", qs)
