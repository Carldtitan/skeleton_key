"""Race: the same task done three ways, all acting as the same logged-in user.

  frontier_browser      - frontier model (Anthropic, optional baseline) driving a browser
  frontier_skeleton_key - the same frontier model calling the generated operations as tools
  open_browser          - open model on Vultr driving a browser (identical harness to the frontier lane)
  skeleton_key          - the same open model on Vultr, text only, calling the generated operations as tools

Fairness rules, so a failure is the model's fault and not the harness's or the scorer's:
  - Ground truth comes from an oracle (the site's verified operations, called directly before the race),
    never from a contestant.
  - Browser lanes see the screenshot, the numbered elements AND the whole page's text; scrolling moves
    ~80% of a screen; repeated back-and-forth is pointed out to the agent.
  - Every lane gets one retry when its reply can't be parsed.
  - The judge accepts values as the site displays them (e.g. rounded "4K"), any order, extra detail.
  - Benchmark tasks live in bench_tasks/<domain>.json and are checked for passability: the oracle's
    answer must be findable in the task's UI pages, or the task is reported invalid.
"""
import asyncio
import json
import os
import pathlib
import secrets
import time
from urllib.parse import urlparse

from . import db, gateway, llm, workers
from .browser import SandboxBrowser
from .config import ANTHROPIC_API_KEY, BROWSE_MODEL, FRONTIER_MODEL, JUDGE_MODEL, TOOL_AGENT_MODEL
from .explorer import element_listing, perform

MAX_BROWSER_STEPS = 20
MAX_TOOL_TURNS = 8
TASKS_DIR = pathlib.Path(__file__).with_name("bench_tasks")

# System prompts are static (the task goes in the user message) so the prompt prefix can be cached.
BROWSER_PROMPT = """You operate a web browser for the user, who is already logged in to {site}.
Each step you get a screenshot, the numbered interactive elements, and the full text of the current page
(including parts not on screen), plus your own notes and the steps so far. Use them to complete the task.
Keep "notes" up to date with every fact you have found that the task needs (you will not see earlier pages
again). Reply with ONLY JSON:
{{"thought": "<short>", "notes": "<all facts gathered so far>",
  "action": "click" | "type" | "scroll" | "navigate" | "back" | "answer",
  "element": <id>, "text": "<text to type>", "submit": <bool>, "url": "<url>", "direction": "down" | "up",
  "answer": "<final answer for the user, only with action answer>"}}"""

TOOL_PROMPT = """You help the user with their {site} account using the provided tools.
Call tools as needed, then give a short final answer."""
TOOL_TOP_K = int(os.environ.get("SK_TOOL_TOP_K", "6"))

JUDGE_PROMPT = """You grade answers to a task about a website.

Task: {task}
Ground truth, fetched from the site's own data for this user just before the answers were produced:
{truth}

Rules:
- Correct = the answer is consistent with the ground truth and fully answers the task.
- Numbers may be given as the website displays them, including rounding ("4K" for 3,670; "103K" for 103,054).
- Order of items only matters if the task asks for an order ("first 3").
- Extra correct detail is fine. Missing, wrong or contradicting information is incorrect.
- "No answer" is incorrect.

Answers:
{answers}

Reply with ONLY JSON: {{"grades": {{"<contestant>": {{"correct": true|false, "why": "<one sentence>"}}}}}}"""

VALIDATE_PROMPT = """Task: {task}
Ground truth from the site's data: {truth}
Text of the site's pages a user would visit:
{pages}

Could a careful user answer the task correctly from these pages alone (numbers may appear rounded)?
Reply with ONLY JSON: {{"passable": true|false, "why": "<one sentence>"}}"""


# ---------- tasks ----------

def tasks(domain):
    path = TASKS_DIR / f"{domain}.json"
    return json.loads(path.read_text()) if path.exists() else []


def presets(domain):
    return [t["question"] for t in tasks(domain)]


def task_for(domain, question):
    return next((t for t in tasks(domain) if t["question"] == question), None)


async def oracle_truth(conn, task, base_url):
    """Call the task's oracle operations directly. Independent of every contestant."""
    truth = []
    for call in task.get("oracle", []):
        output, _ = await gateway.execute(conn, call["op"], call.get("params", {}), base_url)
        truth.append({"operation": call["op"], "params": call.get("params", {}), "output": output})
    return truth


# ---------- helpers ----------

def event(race_id, contestant, kind, **data):
    db.add_event(race_id, f"race_{kind}", {"contestant": contestant, **data})


def summarize_usage(race_id, phase):
    u = db.usage_summary(race_id).get(phase, {})
    return {"model_tokens": (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0),
            "cost_usd": round(u.get("cost_usd") or 0, 6), "model_calls": u.get("calls") or 0}


def oscillating(actions):
    """True if the last moves are scrolls that keep reversing direction."""
    last = actions[-4:]
    return (len(last) == 4 and all(a.startswith("scroll") for a in last)
            and len({a for a in last}) == 2 and last[0] != last[1])


async def cookie_browser(race_id, conn, name, url, sandboxes):
    """A sandbox with the connection's cookies, as its own job row so the live view can show it."""
    sub_id = f"{race_id}-{name}"
    db.create_job(sub_id, url, None, view_token=secrets.token_urlsafe(24), kind="race_part")
    worker, sb = await workers.create_sandbox(sub_id, "about:blank")
    sandboxes.append((worker, sb["id"], sub_id))
    db.update_job(sub_id, worker=worker, sandbox_id=sb["id"], cdp=sb["cdp"], vnc=sb["vnc"], status="racing")
    await asyncio.sleep(4)
    return sub_id, sb


async def add_cookies(b, conn):
    await b.context.add_cookies([{k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly",
                                                     "secure", "sameSite") if k in c} for c in conn["cookies"]])


# ---------- lanes ----------

async def browser_contestant(race_id, name, chat, conn, task, site_root, sandboxes):
    sub_id, sb = await cookie_browser(race_id, conn, name, site_root, sandboxes)
    event(race_id, name, "sandbox", job_id=sub_id)
    llm.meter.set((race_id, name))
    started, answer, steps, error, failure = time.monotonic(), None, 0, None, None
    try:
        async with SandboxBrowser(race_id, sb["cdp"]) as b:
            await add_cookies(b, conn)
            await b.page.goto(site_root, wait_until="domcontentloaded")
            started = time.monotonic()  # time the agent, not the sandbox boot
            event(race_id, name, "start")
            history, actions, notes = [], [], ""  # notes: the agent's own memory, carried to every later step
            for step in range(1, MAX_BROWSER_STEPS + 1):
                steps = step
                b.step = step
                obs = await b.observe(include_text=True)
                messages = [
                    {"role": "system", "content": BROWSER_PROMPT.format(site=conn["domain"])},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{obs['screenshot_b64']}"}},
                        {"type": "text", "text": f"Task: {task}\n\n"
                                                 f"Step {step}/{MAX_BROWSER_STEPS}. Page: {obs['title']} — {obs['url']}\n"
                                                 f"Your notes: {notes or '(none yet)'}\n"
                                                 f"Steps so far:\n{chr(10).join(history) or 'none'}\n\n"
                                                 f"Page text:\n{obs['text']}\n\n"
                                                 f"Elements:\n{element_listing(obs['elements'])}"},
                    ]},
                ]
                act = None
                for attempt in range(2):  # one retry on an unparseable reply, same for every lane
                    text, _ = await chat(messages)
                    try:
                        act = llm.parse_json(text)
                        break
                    except ValueError:
                        messages = messages + [{"role": "assistant", "content": text or "(empty)"},
                                               {"role": "user", "content": "Reply with only the JSON object."}]
                if act is None:
                    failure, error = "format_error", "no parseable action after a retry"
                    break
                event(race_id, name, "step", step=step, action=act.get("action"), thought=act.get("thought"))
                if act.get("notes"):
                    notes = str(act["notes"])[:2000]
                if act.get("action") == "answer":
                    answer = act.get("answer")
                    break
                try:
                    note = await perform(b, act, obs["elements"])
                    await b.settle()
                    label = f"{act.get('action')} {act.get('element') or act.get('direction') or act.get('url') or ''}".strip()
                    actions.append(label)
                    history.append(f"{step}. {label}: {note or 'ok'} ({act.get('thought') or ''})")
                    if oscillating(actions):
                        history.append("note: you keep scrolling back and forth; the page text above already "
                                       "contains the whole page")
                except Exception as e:
                    history.append(f"{step}. {act.get('action')} failed: {str(e).splitlines()[0][:80]}")
            else:
                failure = "step_limit"
    except Exception as e:
        failure, error = "harness_error", f"{type(e).__name__}: {str(e)[:200]}"
    return {"seconds": round(time.monotonic() - started, 1), "steps": steps, "answer": answer, "error": error,
            "failure": failure, **summarize_usage(race_id, name)}


SELECT_PROMPT = """You will answer a task about the user's {site} account using API operations.
Pick the operations you will need from this catalog (name, effect, summary):
{catalog}

Task: {task}
Pick at most {k}. Use read operations when the task only asks for information.
Reply with ONLY JSON: {{"tools": ["<name>", ...]}}"""


def catalog(site):
    return "\n".join(f"- {o['name']} ({'read' if o['spec'].get('side_effect') == 'read' else 'write'}): "
                     f"{o['spec'].get('summary') or ''}" for o in site["spec"]["operations"])


async def select_tools(site, task, race_id, lane, pick):
    """The lane's own model picks its operations from a one-line-per-operation catalog; only those get full
    definitions. `pick(prompt) -> text` calls that lane's model, so a wrong pick is that model's mistake."""
    tools = tool_specs(site)
    if len(tools) <= TOOL_TOP_K:
        return tools
    try:
        text = await pick(SELECT_PROMPT.format(site=site["domain"], catalog=catalog(site), task=task, k=TOOL_TOP_K))
        wanted = {str(n) for n in llm.parse_json(text).get("tools", [])}
    except Exception:
        wanted = set()
    chosen = [t for t in tools if t["function"]["name"] in wanted][:TOOL_TOP_K]
    if not chosen:
        chosen = tools  # unusable pick: offer everything rather than guess
    event(race_id, lane, "tools", offered=[t["function"]["name"] for t in chosen], of=len(tools))
    return chosen


def tool_specs(site):
    tools = []
    for op in site["spec"]["operations"]:
        s = op["spec"]
        props = {p["name"]: {"type": str(p.get("type", "string")).lower()
                             if str(p.get("type", "string")).lower() in
                             {"string", "integer", "number", "boolean", "array", "object"} else "string",
                             "description": p.get("description", "")} for p in s.get("params", [])}
        tools.append({"type": "function", "function": {
            "name": op["name"], "description": s.get("description") or s.get("summary", ""),
            "parameters": {"type": "object", "properties": props,
                           "required": [p["name"] for p in s.get("params", []) if p.get("required")]}}})
    return tools


async def skeleton_key_contestant(race_id, conn, task, base_url):
    """Text-only agent using the generated operations as tools. No browser, no screenshots."""
    name = "skeleton_key"
    llm.meter.set((race_id, name))
    site = db.get_site(conn["domain"])
    messages = [{"role": "system", "content": TOOL_PROMPT.format(site=conn["domain"])},
                {"role": "user", "content": task}]
    async def pick(prompt):
        return (await llm.chat(TOOL_AGENT_MODEL, [{"role": "user", "content": prompt}], max_tokens=4000))[0]
    # Picking tools is the agent's own work (a model call), so it counts toward its time.
    started = time.monotonic()
    event(race_id, name, "start")
    tools = await select_tools(site, task, race_id, name, pick)
    answer, error, failure, calls_made = None, None, None, []
    turns, retried = 0, False
    try:
        while turns < MAX_TOOL_TURNS:
            turns += 1
            msg = await llm.chat_tools(TOOL_AGENT_MODEL, messages, tools)
            calls = msg.get("tool_calls") or []
            if not calls and not (msg.get("content") or "").strip() and not retried:
                retried = True  # one retry on an empty reply, same rule as the browser lanes
                messages.append({"role": "user", "content": "Answer the task now."})
                continue
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                answer = (msg.get("content") or "").strip() or None
                break
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    output, _ = await gateway.execute(conn, fn["name"], args, base_url)
                    result = json.dumps(output, default=str)[:12000]
                except (gateway.GatewayError, ValueError) as e:
                    result = json.dumps({"error": getattr(e, "code", "bad_arguments"), "message": str(e)})
                calls_made.append(fn["name"])
                event(race_id, name, "step", step=turns, action=f"{fn['name']}()", thought=result[:160])
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        else:
            failure = "step_limit"
    except Exception as e:
        failure, error = "harness_error", f"{type(e).__name__}: {str(e)[:200]}"
    return {"seconds": round(time.monotonic() - started, 1), "steps": turns, "answer": answer, "error": error,
            "failure": failure, "tool_calls": len(calls_made), **summarize_usage(race_id, name)}


async def frontier_skeleton_key_contestant(race_id, conn, task, base_url):
    """The frontier model with the same generated operations as tools (same specs, same gateway, same limits)."""
    name = "frontier_skeleton_key"
    llm.meter.set((race_id, name))
    site = db.get_site(conn["domain"])

    async def pick(prompt):
        return (await llm.chat_anthropic(FRONTIER_MODEL, [{"role": "user", "content": prompt}]))[0]

    started = time.monotonic()  # includes picking tools, like the open-model lane
    event(race_id, name, "start")
    tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
              "input_schema": t["function"]["parameters"]}
             for t in await select_tools(site, task, race_id, name, pick)]
    system = TOOL_PROMPT.format(site=conn["domain"])
    messages = [{"role": "user", "content": task}]
    answer, error, failure, calls_made = None, None, None, []
    turns, retried = 0, False
    try:
        while turns < MAX_TOOL_TURNS:
            turns += 1
            response = await llm.chat_anthropic_tools(FRONTIER_MODEL, system, messages, tools)
            if response.stop_reason == "refusal":
                failure, error = "refusal", "the model declined"
                break
            uses = [b for b in response.content if b.type == "tool_use"]
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if not uses and not text and not retried:
                retried = True  # one retry on an empty reply, same rule as the other lanes
                messages += [{"role": "assistant", "content": response.content},
                             {"role": "user", "content": "Answer the task now."}]
                continue
            messages.append({"role": "assistant", "content": response.content})
            if not uses:
                answer = text or None
                break
            results = []
            for use in uses:
                try:
                    output, _ = await gateway.execute(conn, use.name, dict(use.input or {}), base_url)
                    result = json.dumps(output, default=str)[:12000]
                except gateway.GatewayError as e:
                    result = json.dumps({"error": e.code, "message": e.message})
                calls_made.append(use.name)
                event(race_id, name, "step", step=turns, action=f"{use.name}()", thought=result[:160])
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": result})
            messages.append({"role": "user", "content": results})
        else:
            failure = "step_limit"
    except Exception as e:
        failure, error = "harness_error", f"{type(e).__name__}: {str(e)[:200]}"
    return {"seconds": round(time.monotonic() - started, 1), "steps": turns, "answer": answer, "error": error,
            "failure": failure, "tool_calls": len(calls_made), **summarize_usage(race_id, name)}


async def judge(race_id, task, truth, results):
    answers = json.dumps({k: " ".join(str(v.get("answer") or "(no answer)").split()) for k, v in results.items()},
                         indent=1)
    llm.meter.set((race_id, "judge"))
    try:
        verdict, _ = await llm.chat_json(JUDGE_MODEL, [{"role": "user", "content": JUDGE_PROMPT.format(
            task=task, truth=json.dumps(truth, default=str)[:20000], answers=answers)}], max_tokens=8000)
        return verdict.get("grades", {})
    except Exception:
        return {}


async def run_race(race_id, conn, task, base_url):
    site = db.get_site(conn["domain"])
    u = urlparse(site["spec"]["login_url"])
    root = f"{u.scheme}://{u.netloc}/"
    sandboxes = []
    db.update_job(race_id, status="racing")
    try:
        spec = task_for(conn["domain"], task)
        truth = await oracle_truth(conn, spec, base_url) if spec else None
        db.add_event(race_id, "race_truth", {"source": "oracle" if spec else "none", "truth": truth})

        async def vultr_chat(messages):
            return await llm.chat(BROWSE_MODEL, messages, max_tokens=4000)

        async def frontier_chat(messages):
            return await llm.chat_anthropic(FRONTIER_MODEL, messages)

        runs = {}
        if ANTHROPIC_API_KEY:
            runs["frontier_browser"] = browser_contestant(race_id, "frontier_browser", frontier_chat, conn, task,
                                                          root, sandboxes)
            runs["frontier_skeleton_key"] = frontier_skeleton_key_contestant(race_id, conn, task, base_url)
        runs["open_browser"] = browser_contestant(race_id, "open_browser", vultr_chat, conn, task, root, sandboxes)
        runs["skeleton_key"] = skeleton_key_contestant(race_id, conn, task, base_url)
        async def finish(name, run):
            # Report each lane as soon as it ends, so the UI can freeze its clock before the slowest lane is done.
            r = await run
            event(race_id, name, "done", **{k: r.get(k) for k in ("seconds", "answer", "error", "model_tokens",
                                                                   "cost_usd")})
            return r

        names = list(runs)
        results = dict(zip(names, await asyncio.gather(*(finish(n, r) for n, r in runs.items()))))
        grades = await judge(race_id, task, truth, results) if truth else {}
        models = {"frontier_browser": FRONTIER_MODEL, "frontier_skeleton_key": FRONTIER_MODEL,
                  "open_browser": BROWSE_MODEL, "skeleton_key": TOOL_AGENT_MODEL}
        for k, v in results.items():
            v["model"] = models[k]
            v["correct"] = grades.get(k, {}).get("correct")
            v["why"] = grades.get(k, {}).get("why")
            if v["correct"] is False and not v["failure"]:
                v["failure"] = "no_answer" if not v["answer"] else "wrong_answer"
        db.add_event(race_id, "race_result", {"task": task, "graded": bool(truth), "results": results})
        db.update_job(race_id, status="done", status_detail=json.dumps({k: v["seconds"] for k, v in results.items()}))
    except Exception as e:
        db.update_job(race_id, status="failed", status_detail=f"{type(e).__name__}: {e}")
    finally:
        for worker, sb_id, sub_id in sandboxes:
            await workers.delete_sandbox(worker, sb_id)
            db.update_job(sub_id, view_token=None, status="done")
        db.update_job(race_id, view_token=None)


def start_race(conn, task, base_url, tasks_registry):
    race_id = "race_" + secrets.token_hex(5)
    site = db.get_site(conn["domain"])
    db.create_job(race_id, site["spec"]["login_url"], task, view_token=secrets.token_urlsafe(24), kind="race",
                  connection_id=conn["id"])
    tasks_registry[race_id] = asyncio.create_task(run_race(race_id, conn, task, base_url))
    return race_id


# ---------- passability check ----------

async def validate_tasks(conn, base_url):
    """For each benchmark task: fetch the oracle truth and the task's UI pages (logged in), and ask whether the
    answer is findable in those pages. Tasks that aren't passable in the UI must not be used to score browsers."""
    job_id = "validate_" + secrets.token_hex(4)
    sandboxes, report = [], []
    llm.meter.set((job_id, "validate"))
    try:
        _, sb = await cookie_browser(job_id, conn, "validator", "about:blank", sandboxes)
        async with SandboxBrowser(job_id, sb["cdp"]) as b:
            await add_cookies(b, conn)
            for t in tasks(conn["domain"]):
                try:
                    truth = await oracle_truth(conn, t, base_url)
                except gateway.GatewayError as e:
                    report.append({"id": t["id"], "passable": False, "why": f"oracle failed: {e.code}"})
                    continue
                pages = []
                for url in t.get("ui", []):
                    await b.page.goto(url, wait_until="domcontentloaded")
                    await b.settle()
                    text = await b.page.evaluate("() => (document.body.innerText || '').replace(/\\s+/g, ' ')")
                    pages.append(f"[{url}] {text[:5000]}")
                try:
                    verdict, _ = await llm.chat_json(JUDGE_MODEL, [{"role": "user", "content": VALIDATE_PROMPT.format(
                        task=t["question"], truth=json.dumps(truth, default=str)[:8000], pages="\n\n".join(pages))}],
                        max_tokens=6000)
                except Exception as e:  # one unreadable verdict shouldn't sink the whole report
                    verdict = {"passable": False, "why": f"validator failed: {type(e).__name__}: {str(e)[:120]}"}
                report.append({"id": t["id"], "question": t["question"], **verdict})
    finally:
        for worker, sb_id, sub_id in sandboxes:
            await workers.delete_sandbox(worker, sb_id)
            db.update_job(sub_id, view_token=None, status="done")
    return report


def generation_cost(domain):
    site = db.get_site(domain)
    if not site:
        return None
    total = db.usage_summary(site["job_id"])
    return round(sum(p["cost_usd"] or 0 for p in total.values()), 4)
