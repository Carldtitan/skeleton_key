"""Race: the same task done three ways, all acting as the same logged-in user.

  frontier_browser - frontier model (Anthropic, optional baseline) driving a browser by screenshots
  open_browser     - open model on Vultr driving a browser by screenshots (same harness as above)
  skeleton_key     - the same open model on Vultr, text only, calling the generated operations as tools

Each side is measured for wall time, model tokens and cost; a judge model checks each answer
against the data the operations returned.
"""
import asyncio
import json
import secrets
import time
from urllib.parse import urlparse

from . import db, gateway, llm, workers
from .browser import SandboxBrowser
from .config import ANTHROPIC_API_KEY, BROWSE_MODEL, FRONTIER_MODEL, JUDGE_MODEL, TOOL_AGENT_MODEL
from .explorer import element_listing, perform

MAX_BROWSER_STEPS = 20
MAX_TOOL_TURNS = 8

BROWSER_PROMPT = """You operate a web browser for the user, who is already logged in to {site}.
Task: {task}
Use the page to complete the task. Reply with ONLY JSON:
{{"thought": "<short>", "action": "click" | "type" | "scroll" | "navigate" | "back" | "answer",
  "element": <id>, "text": "<text to type>", "submit": <bool>, "url": "<url>", "direction": "down" | "up",
  "answer": "<final answer for the user, only with action answer>"}}"""

TOOL_PROMPT = """You help the user with their {site} account using the provided tools.
Task: {task}
Call tools as needed, then give a short final answer."""

JUDGE_PROMPT = """Task: {task}
Ground truth (data returned by the site's own API for this user):
{truth}

Answers to grade:
{answers}

For each answer, is it correct and complete for the task given the ground truth? Reply with ONLY JSON:
{{"grades": {{"<contestant>": {{"correct": true|false, "why": "<short>"}}}}}}"""

PRESETS = {
    "luma.com": [
        "What are the first 3 event categories on Luma's discover page?",
        "Do I have any upcoming events? Answer yes or no and name them.",
        "Which calendars do I follow on Luma?",
        "How many events are in the AI category?",
    ],
}


def presets(domain):
    return PRESETS.get(domain, ["List what you can see on my account home page."])


def event(race_id, contestant, kind, **data):
    db.add_event(race_id, f"race_{kind}", {"contestant": contestant, **data})


def summarize_usage(race_id, phase):
    u = db.usage_summary(race_id).get(phase, {})
    return {"model_tokens": (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0),
            "cost_usd": round(u.get("cost_usd") or 0, 6), "model_calls": u.get("calls") or 0}


async def browser_contestant(race_id, name, chat, conn, task, site_root, sandboxes):
    """Screenshot-driven agent in its own sandbox, logged in with the connection's cookies."""
    sub_id = f"{race_id}-{name}"  # own job row, so the live-view proxy can show this sandbox
    db.create_job(sub_id, site_root, None, view_token=secrets.token_urlsafe(24), kind="race_part")
    worker, sb = await workers.create_sandbox(sub_id, "about:blank")
    sandboxes.append((worker, sb["id"], sub_id))
    db.update_job(sub_id, worker=worker, sandbox_id=sb["id"], cdp=sb["cdp"], vnc=sb["vnc"], status="racing")
    event(race_id, name, "sandbox", job_id=sub_id)
    await asyncio.sleep(4)
    llm.meter.set((race_id, name))
    started = time.monotonic()
    answer, steps, error = None, 0, None
    try:
        async with SandboxBrowser(race_id, sb["cdp"]) as b:
            await b.context.add_cookies([{k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly",
                                                             "secure", "sameSite") if k in c} for c in conn["cookies"]])
            await b.page.goto(site_root, wait_until="domcontentloaded")
            started = time.monotonic()  # time the agent, not the sandbox boot
            history = []
            for step in range(1, MAX_BROWSER_STEPS + 1):
                steps = step
                b.step = step
                obs = await b.observe()
                text, _ = await chat([
                    {"role": "system", "content": BROWSER_PROMPT.format(site=conn["domain"], task=task)},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{obs['screenshot_b64']}"}},
                        {"type": "text", "text": f"Step {step}. Page: {obs['title']} — {obs['url']}\nDone so far: "
                                                 f"{'; '.join(history[-10:]) or 'nothing'}\n\nElements:\n"
                                                 f"{element_listing(obs['elements'])}"},
                    ]},
                ])
                act = llm.parse_json(text)
                event(race_id, name, "step", step=step, action=act.get("action"), thought=act.get("thought"))
                if act.get("action") == "answer":
                    answer = act.get("answer")
                    break
                try:
                    note = await perform(b, act, obs["elements"])
                    await b.settle()
                    history.append(f"{act.get('action')} {act.get('element') or act.get('direction') or act.get('url') or ''}"
                                   f": {note or 'ok'}")
                except Exception as e:
                    history.append(f"{act.get('action')} failed: {str(e).splitlines()[0][:80]}")
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
    return {"seconds": round(time.monotonic() - started, 1), "steps": steps, "answer": answer, "error": error,
            **summarize_usage(race_id, name)}


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
    messages = [{"role": "system", "content": TOOL_PROMPT.format(site=conn["domain"], task=task)},
                {"role": "user", "content": task}]
    tools, truth, answer, error = tool_specs(site), [], None, None
    started = time.monotonic()
    turns = 0
    try:
        for turns in range(1, MAX_TOOL_TURNS + 1):
            msg = await llm.chat_tools(TOOL_AGENT_MODEL, messages, tools)
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                answer = (msg.get("content") or "").strip()
                break
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    output, _ = await gateway.execute(conn, fn["name"], args, base_url)
                    result = json.dumps(output, default=str)[:12000]
                    truth.append({"tool": fn["name"], "args": args, "output": output})
                except (gateway.GatewayError, ValueError) as e:
                    result = json.dumps({"error": getattr(e, "code", "bad_arguments"), "message": str(e)})
                event(race_id, name, "step", step=turns, action=f"{fn['name']}()", thought=result[:160])
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
    return {"seconds": round(time.monotonic() - started, 1), "steps": turns, "answer": answer, "error": error,
            "tool_calls": len(truth), **summarize_usage(race_id, name)}, truth


async def judge(race_id, task, truth, results):
    if not truth:
        return {}
    # JSON, whitespace collapsed: an answer starting with blank lines must not read as empty.
    answers = json.dumps({k: " ".join(str(v.get("answer") or f"(no answer: {v.get('error')})").split())
                          for k, v in results.items()}, indent=1)
    llm.meter.set((race_id, "judge"))
    try:
        verdict, _ = await llm.chat_json(JUDGE_MODEL, [{"role": "user", "content": JUDGE_PROMPT.format(
            task=task, truth=json.dumps(truth, default=str)[:20000], answers=answers)}], max_tokens=6000)
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
        async def vultr_chat(messages):
            return await llm.chat(BROWSE_MODEL, messages, max_tokens=4000)

        async def frontier_chat(messages):
            return await llm.chat_anthropic(FRONTIER_MODEL, messages)

        runs = {"open_browser": browser_contestant(race_id, "open_browser", vultr_chat, conn, task, root, sandboxes)}
        if ANTHROPIC_API_KEY:
            runs["frontier_browser"] = browser_contestant(race_id, "frontier_browser", frontier_chat, conn, task,
                                                          root, sandboxes)
        sk = asyncio.create_task(skeleton_key_contestant(race_id, conn, task, base_url))
        names = list(runs)
        done = await asyncio.gather(*runs.values())
        results = dict(zip(names, done))
        results["skeleton_key"], truth = await sk
        grades = await judge(race_id, task, truth, results)
        for k, v in results.items():
            v["correct"] = grades.get(k, {}).get("correct")
            v["why"] = grades.get(k, {}).get("why")
        models = {"frontier_browser": FRONTIER_MODEL, "open_browser": BROWSE_MODEL, "skeleton_key": TOOL_AGENT_MODEL}
        for k in results:
            results[k]["model"] = models[k]
        db.add_event(race_id, "race_result", {"task": task, "results": results})
        db.update_job(race_id, status="done", status_detail=json.dumps(
            {k: v["seconds"] for k, v in results.items()}))
    except Exception as e:
        db.update_job(race_id, status="failed", status_detail=f"{type(e).__name__}: {e}")
    finally:
        for worker, sb_id, sub_id in sandboxes:
            await workers.delete_sandbox(worker, sb_id)
            db.update_job(sub_id, view_token=None, status="done")
        db.update_job(race_id, view_token=None)


def start_race(conn, task, base_url, tasks):
    race_id = "race_" + secrets.token_hex(5)
    site = db.get_site(conn["domain"])
    db.create_job(race_id, site["spec"]["login_url"], task, view_token=secrets.token_urlsafe(24), kind="race",
                  connection_id=conn["id"])
    tasks[race_id] = asyncio.create_task(run_race(race_id, conn, task, base_url))
    return race_id


def generation_cost(domain):
    site = db.get_site(domain)
    if not site:
        return None
    total = db.usage_summary(site["job_id"])
    return round(sum(p["cost_usd"] or 0 for p in total.values()), 4)
