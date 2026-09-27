"""Human handoff: wait until the human says they have logged in inside the sandbox.

Only the human's Done button ends the handoff: login flows often continue after the session cookie
appears (onboarding, profile setup, 2FA), and only the human knows when they're finished. Detection
still runs, but only to update the on-screen message ("Looks logged in. Press Done when you're finished.").
"""
import asyncio
import re

from . import db, llm
from .browser import SandboxBrowser
from .config import BROWSE_MODEL

AUTH_COOKIE = re.compile(r"auth|session|sess|token|sid|jwt|login|user", re.I)
LOOKS_DONE = "Looks logged in. Press Done when you're finished."

LOGIN_CHECK = """Look at this screenshot of {site}. Is a user currently logged in (e.g. avatar/profile menu visible,
no 'Sign in' / 'Log in' button, no login form)? Reply ONLY with JSON {{"logged_in": true|false, "why": "<short>"}}"""


def auth_cookie_names(cookies):
    return {c["name"] for c in cookies if AUTH_COOKIE.search(c["name"])}


async def wait_for_login(job_id, cdp, site, manual_flag: asyncio.Event, poll=4, timeout=1800):
    hinted = False
    async with SandboxBrowser(job_id, cdp) as b:
        baseline = auth_cookie_names(await b.cookies())
        waited = 0
        while waited < timeout:
            if manual_flag.is_set():
                db.add_event(job_id, "login_detected", {"by": "human button"})
                return True
            new = auth_cookie_names(await b.cookies()) - baseline
            if new and not hinted:
                try:
                    verdict, _ = await llm.chat_json(BROWSE_MODEL, [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{await b.plain_screenshot()}"}},
                        {"type": "text", "text": LOGIN_CHECK.format(site=site)},
                    ]}], max_tokens=1500)
                except Exception:
                    verdict = {"logged_in": False}
                if verdict.get("logged_in"):
                    hinted = True  # tell the human, but never move on without them
                    db.update_job(job_id, status_detail=LOOKS_DONE)
                    db.add_event(job_id, "login_looks_done", {"new_cookies": sorted(new), "why": verdict.get("why")})
            await asyncio.sleep(poll)
            waited += poll
    return False
