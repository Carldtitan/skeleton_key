"""Job lifecycle.

generate: sandbox -> human login -> exploration -> generation/verification -> publish -> connection
connect:  sandbox -> human login -> store the session on a (new or existing) connection
"""
import asyncio
import secrets

from . import db, gateway, llm, publisher, workers
from .config import EXPLORE_MAX_STEPS, PUBLIC_URL
from .endpoints import site_domain
from .explorer import explore
from .generator import Generator
from .handoff import wait_for_login
from .browser import SandboxBrowser

# In-memory signals for jobs running in this process.
human_done: dict[str, asyncio.Event] = {}
tasks: dict[str, asyncio.Task] = {}


def set_status(job_id, status, detail=None):
    db.update_job(job_id, status=status, status_detail=detail)
    db.add_event(job_id, "status", {"status": status, "detail": detail})


async def need_human(job_id, reason):
    """Pause the job until the human presses 'Done' in the live view."""
    ev = human_done.setdefault(job_id, asyncio.Event())
    ev.clear()
    set_status(job_id, "needs_human", reason)
    await ev.wait()
    set_status(job_id, "exploring", "human finished, resuming")


async def sandbox_and_login(job_id, url):
    """Start a sandbox on the least-loaded worker and wait for the human to log in."""
    worker, sb = await workers.create_sandbox(job_id, url)
    db.update_job(job_id, worker=worker, sandbox_id=sb["id"], cdp=sb["cdp"], vnc=sb["vnc"])
    await asyncio.sleep(4)  # let Chromium and noVNC come up
    ev = human_done.setdefault(job_id, asyncio.Event())
    ev.clear()
    set_status(job_id, "needs_human", "Log in to the site in the live view, then press Done.")
    llm.meter.set((job_id, "login"))
    ok = await wait_for_login(job_id, sb["cdp"], url, ev)
    ev.clear()
    return worker, sb, ok


async def run_job(job_id):
    job = db.get_job(job_id)
    try:
        worker, sb, ok = await sandbox_and_login(job_id, job["site_url"])
        if not ok:
            set_status(job_id, "failed", "login timed out")
            return
        set_status(job_id, "exploring", "discovering actions")
        llm.meter.set((job_id, "explore"))
        history = await explore(job_id, sb["cdp"], job["site_url"], job["hints"], EXPLORE_MAX_STEPS,
                                lambda reason: need_human(job_id, reason))
        set_status(job_id, "explored", f"{len(history)} steps, {db.request_count(job_id)} requests captured")
        await run_generation(job_id)
        await run_publish(job_id, create_connection=True)
    except Exception as e:
        set_status(job_id, "failed", f"{type(e).__name__}: {e}")


async def run_generation(job_id, retry_failed=False):
    job = db.get_job(job_id)
    set_status(job_id, "generating", "retrying failed operations" if retry_failed
               else "writing and verifying one operation per endpoint")
    llm.meter.set((job_id, "generate"))
    try:
        gen = Generator(job_id, job["site_url"])
        counts = await (gen.retry_failed() if retry_failed else gen.run())
        set_status(job_id, "generated", ", ".join(f"{v} {k}" for k, v in counts.items()))
    except Exception as e:
        set_status(job_id, "failed", f"generation: {type(e).__name__}: {e}")


async def run_publish(job_id, create_connection=False):
    set_status(job_id, "publishing", "naming, redaction, OpenAPI")
    llm.meter.set((job_id, "publish"))
    try:
        summary = await publisher.publish(job_id, PUBLIC_URL)
        if create_connection:
            # The person who generated the API is already logged in: give them a connection right away.
            conn_id, key = gateway.new_connection(summary["domain"], db.get_session(job_id) or [], job_id)
            db.add_event(job_id, "connection_created", {"connection_id": conn_id, "api_key": key,
                                                        "mcp_url": f"{PUBLIC_URL}/mcp/{key}"})
        set_status(job_id, "published", f"{summary['operations']} operations at /specs/{summary['domain']}")
    except Exception as e:
        set_status(job_id, "failed", f"publish: {type(e).__name__}: {e}")


async def run_connect(job_id):
    """Login-only job: capture a fresh session for a new or existing connection, then drop the sandbox."""
    job = db.get_job(job_id)
    sb = None
    try:
        worker, sb, ok = await sandbox_and_login(job_id, job["site_url"])
        if not ok:
            set_status(job_id, "failed", "login timed out")
            return
        async with SandboxBrowser(job_id, sb["cdp"]) as b:
            cookies = await b.cookies()
        domain = site_domain(job["site_url"])
        if job["connection_id"] and db.get_connection(job["connection_id"]):
            db.update_connection(job["connection_id"], cookies=cookies, status="active", job_id=job_id)
            db.add_event(job_id, "connection_refreshed", {"connection_id": job["connection_id"]})
        else:
            conn_id, key = gateway.new_connection(domain, cookies, job_id)
            db.add_event(job_id, "connection_created", {"connection_id": conn_id, "api_key": key,
                                                        "mcp_url": f"{PUBLIC_URL}/mcp/{key}"})
        set_status(job_id, "connected", domain)
    except Exception as e:
        set_status(job_id, "failed", f"{type(e).__name__}: {e}")
    finally:
        if sb:
            await workers.delete_sandbox(job["worker"] or db.get_job(job_id)["worker"], sb["id"])
            db.update_job(job_id, view_token=None)


def start_generation(job_id, retry_failed=False):
    tasks[job_id] = asyncio.create_task(run_generation(job_id, retry_failed))


def start_publish(job_id, create_connection=False):
    tasks[job_id] = asyncio.create_task(run_publish(job_id, create_connection))


def start_job(site_url, hints):
    job_id = secrets.token_hex(6)
    db.create_job(job_id, site_url, hints, view_token=secrets.token_urlsafe(24))
    tasks[job_id] = asyncio.create_task(run_job(job_id))
    return job_id


def start_connect(domain, connection_id=None):
    site = db.get_site(domain)
    if not site:
        raise ValueError(f"no published API for {domain}")
    job_id = "conn_job_" + secrets.token_hex(5)
    db.create_job(job_id, site["spec"]["login_url"], None, view_token=secrets.token_urlsafe(24), kind="connect",
                  connection_id=connection_id)
    tasks[job_id] = asyncio.create_task(run_connect(job_id))
    return job_id


async def stop_job(job_id):
    job = db.get_job(job_id)
    if job_id in tasks:
        tasks[job_id].cancel()
    if job and job["sandbox_id"]:
        await workers.delete_sandbox(job["worker"], job["sandbox_id"])
    db.update_job(job_id, view_token=None)  # live-view link dies with the sandbox
    set_status(job_id, "stopped")
