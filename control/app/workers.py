"""Client for the worker daemons: places each sandbox on the least-loaded worker."""
import httpx

from .config import WORKER_TOKEN, WORKERS

HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


async def pick_worker():
    best, best_free = None, -1
    async with httpx.AsyncClient(timeout=5) as client:
        for w in WORKERS:
            try:
                h = (await client.get(f"http://{w}/health")).json()
            except httpx.HTTPError:
                continue
            if h["accepting"] and h["free_mb"] > best_free:
                best, best_free = w, h["free_mb"]
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
