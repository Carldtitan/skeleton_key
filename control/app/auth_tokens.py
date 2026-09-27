"""Session auth beyond cookies.

Many apps (Firebase, Supabase, Auth0, ...) authenticate API calls with a short-lived bearer token that the
web app keeps in browser storage, not in a cookie. A session is therefore cookies + auth headers. Tokens
expire (Firebase: ~1 hour), so we also keep the browser's storage state (IndexedDB included, where
refresh tokens live) and mint a fresh token by opening the site in a sandbox with that state; the site's
own code refreshes the token and we read it off its next request. No human needed.
"""
import asyncio
import json
import re
from urllib.parse import urlparse

from . import db, workers
from .browser import SandboxBrowser
from .endpoints import site_domain

AUTH_HEADER = re.compile(r"^(authorization|x-[a-z0-9-]*(auth|access|id)-token)$", re.I)


def _is_auth(name, value):
    return bool(AUTH_HEADER.match(name)) and "turnstile" not in name.lower() and len(value or "") > 10


def latest_recorded(job_id, domain):
    """The newest auth headers the site's own requests carried during a job."""
    found = {}
    for r in db.requests_for(job_id):
        if not (urlparse(r["url"]).hostname or "").endswith(domain):
            continue
        for k, v in json.loads(r["req_headers"] or "{}").items():
            if _is_auth(k, v):
                found[k.lower()] = v
    return found


async def _listen(context, page, url, domain, seconds):
    found = {}

    async def on_request(req):
        if not (urlparse(req.url).hostname or "").endswith(domain):
            return
        try:
            headers = await req.all_headers()
        except Exception:
            headers = req.headers
        for k, v in headers.items():
            if _is_auth(k, v):
                found[k.lower()] = v

    context.on("request", on_request)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        pass
    for _ in range(seconds * 2):
        await asyncio.sleep(0.5)
        if found and _ >= 4:
            break
    return found


async def capture_live(cdp, url):
    """Fresh auth headers from a logged-in sandbox (the site's own code refreshes its token on load)."""
    domain = site_domain(url)
    async with SandboxBrowser("auth", cdp, record=False) as b:
        page = await b.context.new_page()
        try:
            return await _listen(b.context, page, url, domain, 15)
        finally:
            await page.close()


async def capture_session(cdp, url):
    """Everything the live logged-in browser currently authenticates with: its cookies (some sites renew a
    short-lived session cookie every minute, e.g. Clerk) and any auth headers its requests carry."""
    domain = site_domain(url)
    async with SandboxBrowser("auth", cdp, record=False) as b:
        page = await b.context.new_page()
        try:
            headers = await _listen(b.context, page, url, domain, 8)
            cookies = await b.context.cookies()
        finally:
            await page.close()
    return cookies, headers


async def storage_state(cdp):
    """Cookies + localStorage + IndexedDB of the logged-in sandbox, to re-mint tokens later."""
    async with SandboxBrowser("auth", cdp, record=False) as b:
        return await b.context.storage_state(indexed_db=True)


async def refresh_from_state(state, url):
    """Open the site in a throwaway sandbox with the saved storage and read the fresh auth headers."""
    domain = site_domain(url)
    worker, sb = await workers.create_sandbox("auth-refresh", "about:blank")
    try:
        await asyncio.sleep(4)
        async with SandboxBrowser("auth", sb["cdp"], record=False) as b:
            context = await b.browser.new_context(storage_state=state)
            page = await context.new_page()
            try:
                return await _listen(context, page, url, domain, 20)
            finally:
                await context.close()
    finally:
        await workers.delete_sandbox(worker, sb["id"])


def site_root(url):
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}/"


async def refresh(conn, job_cdp=None, url=None):
    """Best source first: the job's still-running sandbox, else a new sandbox from the saved state."""
    if job_cdp:
        try:
            headers = await capture_live(job_cdp, url)
            if headers:
                return headers
        except Exception:
            pass
    if conn and conn.get("storage_state"):
        try:
            return await refresh_from_state(conn["storage_state"], url)
        except Exception:
            return {}
    return {}
