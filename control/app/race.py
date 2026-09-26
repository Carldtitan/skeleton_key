"""Race: the same task done by a vision browser agent vs. one call to the generated API.

The browser side runs in its own sandbox (watchable via the live view) with the connection's cookies,
so both sides act as the same logged-in user. Time, model tokens and cost are measured for each.
"""
import asyncio
import secrets
import time
from urllib.parse import urlparse

from . import db, gateway, llm, workers
from .browser import SandboxBrowser
from .config import BROWSE_MODEL
from .explorer import element_listing, perform

TASK_PROMPT = """You operate a web browser for the user, who is already logged in to {site}.
Task: {task}
Use the page to find the answer. Reply with ONLY JSON:
{{"thought": "<short>", "action": "click" | "type" | "scroll" | "navigate" | "back" | "answer",
  "element": <id>, "text": "<text to type>", "submit": <bool>, "url": "<url>", "direction": "down" | "up",
  "answer": "<final answer for the user, only with action answer>"}}"""


async def browser_agent(race_id, cdp, site, task, max_steps=20):
    started = time.monotonic()
    history, answer, steps = [], None, 0
    async with SandboxBrowser(race_id, cdp) as b:
        for step in range(1, max_steps + 1):
            steps = step
            b.step = step
            obs = await b.observe()
            act, _ = await llm.chat_json(BROWSE_MODEL, [
                {"role": "system", "content": TASK_PROMPT.format(site=site, task=task)},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{obs['screenshot_b64']}"}},
                    {"type": "text", "text": f"Step {step}. Page: {obs['title']} — {obs['url']}\nDone so far: "
                                             f"{'; '.join(history[-10:]) or 'nothing'}\n\nElements:\n"
                                             f"{element_listing(obs['elements'])}"},
                ]},
            ], max_tokens=4000)
            db.add_event(race_id, "race_step", {"side": "browser", "step": step, "action": act.get("action"),
                                                "thought": act.get("thought")})
            if act.get("action") == "answer":
                answer = act.get("answer")
                break
            try:
                await perform(b, act, obs["elements"])
                await b.settle()
                history.append(f"{act.get('action')} {act.get('element') or act.get('url') or ''} ok")
            except Exception as e:
                history.append(f"{act.get('action')} failed: {str(e).splitlines()[0][:80]}")
    return {"seconds": round(time.monotonic() - started, 1), "steps": steps, "answer": answer}


async def api_side(conn, op, params, base_url):
    started = time.monotonic()
    try:
        output, _ = await gateway.execute(conn, op, params, base_url)
        return {"seconds": round(time.monotonic() - started, 2), "output": output, "model_tokens": 0, "cost_usd": 0.0}
    except gateway.GatewayError as e:
        return {"seconds": round(time.monotonic() - started, 2), "error": f"{e.code}: {e.message}"}


def generation_cost(domain):
    site = db.get_site(domain)
    if not site:
        return None
    total = db.usage_summary(site["job_id"])
    return round(sum(p["cost_usd"] or 0 for p in total.values()), 4)


async def run_race(race_id, conn, task, op, params, base_url):
    site = db.get_site(conn["domain"])
    worker, sb = await workers.create_sandbox(race_id, "about:blank")
    db.update_job(race_id, worker=worker, sandbox_id=sb["id"], cdp=sb["cdp"], vnc=sb["vnc"], status="racing")
    try:
        await asyncio.sleep(4)
        async with SandboxBrowser(race_id, sb["cdp"]) as b:  # same logged-in user as the API side
            await b.context.add_cookies([{k: c[k] for k in ("name", "value", "domain", "path", "expires",
                                                              "httpOnly", "secure", "sameSite") if k in c}
                                         for c in conn["cookies"]])
            u = urlparse(site["spec"]["login_url"])
            await b.page.goto(f"{u.scheme}://{u.netloc}/", wait_until="domcontentloaded")
        llm.meter.set((race_id, "race_browser"))
        browser_task = asyncio.create_task(browser_agent(race_id, sb["cdp"], conn["domain"], task))
        api = await api_side(conn, op, params, base_url)
        browser = await browser_task
        usage = db.usage_summary(race_id).get("race_browser", {})
        browser.update(model_tokens=(usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0),
                       cost_usd=round(usage.get("cost_usd") or 0, 5), model_calls=usage.get("calls", 0))
        gen_cost = generation_cost(conn["domain"])
        result = {"task": task, "operation": op, "browser": browser, "api": api,
                  "speedup": round(browser["seconds"] / max(api["seconds"], 0.01), 1),
                  "generation_cost_usd": gen_cost,
                  "break_even_calls": (round(gen_cost / browser["cost_usd"], 1)
                                       if gen_cost and browser["cost_usd"] else None)}
        db.add_event(race_id, "race_result", result)
        db.update_job(race_id, status="done", status_detail=f"browser {browser['seconds']}s vs api {api['seconds']}s")
        return result
    except Exception as e:
        db.update_job(race_id, status="failed", status_detail=f"{type(e).__name__}: {e}")
        raise
    finally:
        await workers.delete_sandbox(worker, sb["id"])
        db.update_job(race_id, view_token=None)


def start_race(conn, task, op, params, base_url, tasks):
    race_id = "race_" + secrets.token_hex(5)
    site = db.get_site(conn["domain"])
    db.create_job(race_id, site["spec"]["login_url"], task, view_token=secrets.token_urlsafe(24), kind="race",
                  connection_id=conn["id"])
    tasks[race_id] = asyncio.create_task(run_race(race_id, conn, task, op, params, base_url))
    return race_id
