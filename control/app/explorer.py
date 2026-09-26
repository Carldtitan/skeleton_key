"""Exploration agent: clicks through the logged-in app to discover every distinct atomic action.

The goal is breadth, not a task: each distinct request the app makes (list events, get event,
RSVP, cancel RSVP, ...) later becomes its own granular API operation. Every step is labelled
with the user-level action it performed so captured requests can be named from the UI.
"""
import json

from . import db, llm
from .browser import SandboxBrowser
from .config import BROWSE_MODEL
from .endpoints import endpoint_key, is_app_api, site_domain

SYSTEM = """You are the exploration agent of Skeleton Key. A human has already logged in to {site} in this browser.
Your job: discover EVERY distinct action a user can perform on this site with their account, so each one can later
become its own small API operation (like git add / git push / git pull are separate commands).

How to explore:
- You are scored on NEW API endpoints discovered. After each step you are told which new endpoints your action
  triggered. If a kind of page stops producing new endpoints, move to a different section of the site.
- Do not open the same kind of page twice (one event page is enough), and do not linger in modals.
- Visit each main section (nav bar, sidebar, profile menu, settings) and open detail pages, tabs, filters, search,
  pagination and menus.
- Reading actions (list, view, search, filter) are always allowed.
- Reversible write actions are allowed ONLY if you undo them in your very next steps (e.g. RSVP then cancel the
  RSVP, follow then unfollow, like then unlike, save then unsave, subscribe then unsubscribe). Mark them
  is_write=true. Undo options are often hidden in a menu on the same button (e.g. "Subscribed" opens "Unsubscribe").
  When a step undoes an earlier write, set "undoes" to that write's label.
- NEVER do irreversible or costly actions: payments, purchases, deleting things, sending messages or invites to
  other people, publishing public content, changing account email/password, logging out. You may OPEN such a
  form to see its fields, then back out without submitting.
- If you see a login wall, CAPTCHA, 2FA prompt or "session expired", answer action "need_human".
- When you have covered the site, answer action "done".
{hints}
Reply with ONLY a JSON object:
{{"thought": "<one short sentence>",
  "action": "click" | "type" | "select" | "scroll" | "navigate" | "back" | "need_human" | "done",
  "element": <element id for click/type/select>,
  "text": "<text to type, or option to select>",
  "submit": <true to press Enter after typing>,
  "url": "<url for navigate>",
  "direction": "down" | "up",
  "label": "<the user-level action this step performs, e.g. 'open event details', 'rsvp to event', 'search events'>",
  "is_write": <true if this step changes data on the server>,
  "undoes": "<label of the earlier write this step reverses, or null>"}}"""


def element_listing(elements):
    lines = []
    for e in elements:
        extra = f" href={e['href'][:60]}" if e.get("href") else ""
        kind = f" ({e['type']})" if e.get("type") else ""
        lines.append(f"[{e['id']}] <{e['tag']}{kind}> {e['text']}{extra}")
    return "\n".join(lines) or "(no interactive elements visible)"


def resolve_element(page, element, elements):
    """Models sometimes answer with the element's text instead of its number; accept both."""
    try:
        return page.locator(f'[data-sk="{int(element)}"]').first
    except (TypeError, ValueError):
        wanted = str(element or "").strip().lower()
        for e in elements:
            if wanted and wanted in e["text"].lower():
                return page.locator(f'[data-sk="{e["id"]}"]').first
        raise ValueError(f"unknown element {element!r}")


async def perform(b: SandboxBrowser, act, elements):
    page = b.page
    a = act.get("action")
    if a in ("click", "type", "select"):
        loc = resolve_element(page, act.get("element"), elements)
        if a == "click":
            await loc.click(timeout=8_000)
        elif a == "type":
            await loc.fill(str(act.get("text", "")), timeout=8_000)
            if act.get("submit"):
                await loc.press("Enter")
        else:
            await loc.select_option(label=str(act.get("text", "")), timeout=8_000)
    elif a == "scroll":
        await page.mouse.wheel(0, 700 if act.get("direction", "down") == "down" else -700)
    elif a == "navigate":
        await page.goto(act["url"], wait_until="domcontentloaded", timeout=30_000)
    elif a == "back":
        await page.go_back(wait_until="domcontentloaded", timeout=30_000)


async def explore(job_id, cdp, site, hints, max_steps, on_need_human, patience=10):
    """Run the exploration loop. on_need_human(reason) is awaited when a human must step in."""
    hint_text = f"\nUser hints about what matters on this site: {hints}\n" if hints else ""
    domain = site_domain(site)
    history = []
    # Endpoints seen before exploration started (e.g. during login) count as known.
    known = {endpoint_key(r["method"], r["url"], r["req_body"]) for r in db.requests_for(job_id)
             if is_app_api(r["method"], r["url"], r["resource_type"], domain)}
    pending_undo = {}  # label of an unreverted write -> step it happened
    no_new_streak = 0
    async with SandboxBrowser(job_id, cdp) as b:
        for step in range(1, max_steps + 1):
            b.step = step
            obs = await b.observe()
            recent = "\n".join(f"{h['step']}. {h['label']} ({h['action']}) -> {h['result']}" for h in history[-25:])
            undo_text = ""
            if pending_undo:
                undo_text = ("\n\nIMPORTANT: you must undo these writes now, before anything else: "
                             + "; ".join(pending_undo) + "\n")
            messages = [
                {"role": "system", "content": SYSTEM.format(site=site, hints=hint_text)},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{obs['screenshot_b64']}"}},
                    {"type": "text", "text": (
                        f"Step {step}/{max_steps}. Current page: {obs['title']} — {obs['url']}\n\n"
                        f"Actions so far:\n{recent or '(none yet)'}\n\n"
                        f"API endpoints discovered so far ({len(known)}):\n"
                        + "\n".join(sorted(known)[-60:]) + undo_text + "\n\n"
                        f"Interactive elements (numbers match the red labels):\n{element_listing(obs['elements'])}"
                    )},
                ]},
            ]
            try:
                act, usage = await llm.chat_json(BROWSE_MODEL, messages, max_tokens=4000)
            except Exception as e:
                db.add_event(job_id, "error", {"step": step, "error": f"model: {e}"})
                continue

            db.add_event(job_id, "step", {"step": step, "action": act.get("action"), "label": act.get("label"),
                                          "thought": act.get("thought"), "element": act.get("element"),
                                          "is_write": act.get("is_write"), "tokens": usage.get("total_tokens")})
            if act.get("action") == "done":
                break
            if act.get("action") == "need_human":
                await on_need_human(act.get("thought") or "the agent needs a human")
                history.append({"step": step, "label": "human stepped in", "action": "need_human", "result": "resumed"})
                continue

            url_before = b.page.url
            try:
                await perform(b, act, obs["elements"])
                await b.settle()
                result = "ok"
            except Exception as e:
                result = f"failed: {str(e).splitlines()[0][:120]}"

            new = []
            for r in db.requests_for(job_id, step):
                if is_app_api(r["method"], r["url"], r["resource_type"], domain):
                    k = endpoint_key(r["method"], r["url"], r["req_body"])
                    if k not in known:
                        known.add(k)
                        new.append(k)
            no_new_streak = 0 if new else no_new_streak + 1

            label = act.get("label") or act.get("action")
            if act.get("undoes"):
                for pending in list(pending_undo):
                    if pending.lower() in str(act["undoes"]).lower() or str(act["undoes"]).lower() in pending.lower():
                        pending_undo.pop(pending)
            elif act.get("is_write") and result == "ok":
                pending_undo[label] = step
            for pending, at in list(pending_undo.items()):
                if step - at >= 4:
                    db.add_event(job_id, "unreverted_write", {"write": pending, "step": at})
                    pending_undo.pop(pending)

            db.save_step(job_id, step, action=act.get("action"), label=label, is_write=act.get("is_write"),
                         url_before=url_before, url_after=b.page.url,
                         detail={"element": act.get("element"), "text": act.get("text"), "result": result,
                                 "new_endpoints": new, "undoes": act.get("undoes")})
            db.add_event(job_id, "coverage", {"step": step, "new": new, "total": len(known)})
            history.append({"step": step, "label": label, "action": act.get("action"),
                            "result": f"{result}, new endpoints: {', '.join(new) if new else 'none'}"})
            if no_new_streak >= patience and not pending_undo:
                db.add_event(job_id, "status", {"status": "exploring", "detail":
                                                f"stopping: {patience} steps without a new endpoint"})
                break
        for pending, at in pending_undo.items():
            db.add_event(job_id, "unreverted_write", {"write": pending, "step": at})
        db.save_session(job_id, await b.cookies())
    return history
