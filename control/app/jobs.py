"""Job lifecycle: sandbox -> human login -> exploration -> (analysis, generation, verification come next)."""
import asyncio
import secrets

from . import db, workers
from .config import EXPLORE_MAX_STEPS
from .explorer import explore
from .generator import Generator
from .handoff import wait_for_login

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


async def run_job(job_id):
    job = db.get_job(job_id)
    try:
        worker, sb = await workers.create_sandbox(job_id, job["site_url"])
        db.update_job(job_id, worker=worker, sandbox_id=sb["id"], cdp=sb["cdp"], vnc=sb["vnc"])
        await asyncio.sleep(4)  # let Chromium and noVNC come up

        ev = human_done.setdefault(job_id, asyncio.Event())
        ev.clear()
        set_status(job_id, "needs_human", "Log in to the site in the live view, then press Done.")
        if not await wait_for_login(job_id, sb["cdp"], job["site_url"], ev):
            set_status(job_id, "failed", "login timed out")
            return
        ev.clear()

        set_status(job_id, "exploring", "discovering actions")
        history = await explore(job_id, sb["cdp"], job["site_url"], job["hints"], EXPLORE_MAX_STEPS,
                                lambda reason: need_human(job_id, reason))
        set_status(job_id, "explored", f"{len(history)} steps, {db.request_count(job_id)} requests captured")
        await run_generation(job_id)
    except Exception as e:
        set_status(job_id, "failed", f"{type(e).__name__}: {e}")


async def run_generation(job_id, retry_failed=False):
    job = db.get_job(job_id)
    set_status(job_id, "generating", "retrying failed operations" if retry_failed
               else "writing and verifying one operation per endpoint")
    try:
        gen = Generator(job_id, job["site_url"])
        counts = await (gen.retry_failed() if retry_failed else gen.run())
        set_status(job_id, "generated", ", ".join(f"{v} {k}" for k, v in counts.items()))
    except Exception as e:
        set_status(job_id, "failed", f"generation: {type(e).__name__}: {e}")


def start_generation(job_id, retry_failed=False):
    tasks[job_id] = asyncio.create_task(run_generation(job_id, retry_failed))


def start_job(site_url, hints):
    job_id = secrets.token_hex(6)
    db.create_job(job_id, site_url, hints, view_token=secrets.token_urlsafe(24))
    tasks[job_id] = asyncio.create_task(run_job(job_id))
    return job_id


async def stop_job(job_id):
    job = db.get_job(job_id)
    if job_id in tasks:
        tasks[job_id].cancel()
    if job and job["sandbox_id"]:
        await workers.delete_sandbox(job["worker"], job["sandbox_id"])
    db.update_job(job_id, view_token=None)  # live-view link dies with the sandbox
    set_status(job_id, "stopped")
