"""Guest mode: anyone can try Skeleton Key without an account.

A guest takes one of MAX_GUESTS seats (one click, no password). The seat lasts SEAT_HOURS and is held
longer only while one of the guest's connections is still live. A guest may connect each published site
with their own account; every connection lasts CONNECTION_HOURS, then its logged-in browser is closed
and its API key stops working. Guests can't generate, see the operator's connections, or approve lessons.
"""
import asyncio
import hashlib
import hmac
import secrets
import time

from . import db, workers

MAX_GUESTS = 4
SEAT_HOURS = 6
CONNECTION_HOURS = 6
COOKIE = "sk_guest"


class Full(Exception):
    def __init__(self, frees_at):
        self.frees_at = frees_at


def _sign(secret, guest_id):
    return hmac.new(secret.encode(), f"guest:{guest_id}".encode(), hashlib.sha256).hexdigest()[:32]


def cookie_value(secret, guest_id):
    return f"{guest_id}.{_sign(secret, guest_id)}"


def from_cookie(secret, value):
    """The guest id in a valid cookie whose seat is still active, else None."""
    guest_id, _, sig = (value or "").partition(".")
    if not guest_id or not hmac.compare_digest(sig, _sign(secret, guest_id)):
        return None
    g = db.get_guest(guest_id)
    return guest_id if g and not g["ended"] and g["expires"] > time.time() else None


def seats():
    active = db.active_guests()
    return {"used": len(active), "max": MAX_GUESTS,
            "frees_at": active[0]["expires"] if len(active) >= MAX_GUESTS else None}


def start():
    """Take a seat; raises Full when every seat is held."""
    s = seats()
    if s["used"] >= MAX_GUESTS:
        raise Full(s["frees_at"])
    guest_id = "g_" + secrets.token_hex(8)
    db.create_guest(guest_id, time.time() + SEAT_HOURS * 3600)
    return guest_id


def connection_expiry(guest_id):
    """A new connection lasts CONNECTION_HOURS; the seat is held at least that long too."""
    expires = time.time() + CONNECTION_HOURS * 3600
    g = db.get_guest(guest_id)
    if g and g["expires"] < expires:
        db.update_guest(guest_id, expires=expires)
    return expires


async def end_connection(conn):
    """Close the connection's logged-in browser and invalidate its key's session."""
    job = db.get_job(conn["job_id"]) if conn.get("job_id") else None
    if job and job.get("sandbox_id") and job.get("view_token") is not None:
        try:
            await workers.delete_sandbox(job["worker"], job["sandbox_id"])
        except Exception:
            pass
        db.update_job(job["id"], view_token=None)
    db.update_connection(conn["id"], status="ended", cookies=[], auth_headers={}, storage_state=None)


async def end_guest(guest_id):
    for c in db.guest_connections(guest_id):
        if c["status"] != "ended":
            await end_connection(c)
    db.update_guest(guest_id, ended=1)


async def cleanup_loop(interval=60):
    """End expired guest connections and free expired seats."""
    while True:
        now = time.time()
        try:
            for c in db.guest_connections():
                if c["status"] != "ended" and (c.get("expires") or 0) < now:
                    await end_connection(c)
            with db.conn() as cx:
                expired = [r["id"] for r in cx.execute("SELECT id FROM guests WHERE ended=0 AND expires<=?", (now,))]
            for g in expired:
                await end_guest(g)
        except Exception:
            pass
        await asyncio.sleep(interval)
