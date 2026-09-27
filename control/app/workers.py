"""Client for the worker daemons: places each sandbox on the least-loaded worker."""
import httpx

from .config import WORKER_TOKEN, WORKERS

HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


# A browser that just started hasn't used its memory yet, so count what each sandbox will take.
SANDBOX_MB = 700


async def pick_worker():
    best, best_free = None, None
    async with httpx.AsyncClient(timeout=5) as client:
        for w in WORKERS:
            try:
                h = (await client.get(f"http://{w}/health")).json()
            except httpx.HTTPError:
                continue
            free = h["free_mb"] - h["sandboxes"] * SANDBOX_MB
            if h["accepting"] and (best_free is None or free > best_free):
                best, best_free = w, free
    if not best:
        raise RuntimeError("no worker has capacity")
    return best


async def create_sandbox(job_id, start_url):
    worker = await pick_worker()
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"http://{worker}/sandboxes", headers=HEADERS,
                              json={"job_id": job_id, "start_url": start_url})
        r.raise_for_status()
        return worker, r.json()


async def delete_sandbox(worker, sandbox_id):
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(f"http://{worker}/sandboxes/{sandbox_id}", headers=HEADERS)
