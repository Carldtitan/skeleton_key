import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import zipfile
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import db, gateway, jobs, liveview, mcp_server, race
from .config import LIVE_BASE, ANTHROPIC_API_KEY, BROWSE_MODEL, FRONTIER_MODEL, HEALTH_CHECK_SECONDS, PUBLIC_URL
from .endpoints import site_domain

ADMIN_PASSWORD = os.environ["SK_ADMIN_PASSWORD"]
SESSION_COOKIE = "sk_session"
SESSION_VALUE = hmac.new(ADMIN_PASSWORD.encode(), b"skeleton-key-session", hashlib.sha256).hexdigest()
RUNNING = ("starting", "needs_human", "exploring", "generating", "publishing", "racing")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    # Jobs don't survive a restart (their asyncio tasks are gone); mark them so the UI is honest.
    for j in db.list_jobs():
        if j["status"] in RUNNING:
            db.update_job(j["id"], status="interrupted", status_detail="control plane restarted")
    health = asyncio.create_task(gateway.health_loop(HEALTH_CHECK_SECONDS, PUBLIC_URL))
    yield
    health.cancel()


app = FastAPI(title="Skeleton Key", lifespan=lifespan, docs_url=None, redoc_url=None)
basic = HTTPBasic(auto_error=False)
app.include_router(liveview.router)
app.include_router(mcp_server.router)


def admin(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
    """Operator auth: the UI's session cookie, or HTTP basic auth for scripts."""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie and hmac.compare_digest(cookie, SESSION_VALUE):
        return
    if creds and secrets.compare_digest(creds.password, ADMIN_PASSWORD):
        return
    raise HTTPException(401, "login required")


def live_url(job):
    if not job or not job.get("view_token") or not job.get("vnc"):
        return None
    # noVNC resolves `path` relative to the page, so plain "websockify" lands on the proxied socket.
    return (f"{LIVE_BASE}/live/{job['id']}/{job['view_token']}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
            f"&show_dot=true&path=websockify")


# --- Login (the UI itself is served by Vercel, which forwards /api here) ---------------------------


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    response.set_cookie(SESSION_COOKIE, SESSION_VALUE, httponly=True, secure=True, samesite="lax",
                        max_age=7 * 86400)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(admin)])
def me():
    return {"ok": True, "frontier": FRONTIER_MODEL if ANTHROPIC_API_KEY else None}


# --- Home ----------------------------------------------------------------------------------------

def connection_view(conn):
    if not conn:
        return None
    return {"id": conn["id"], "status": conn["status"], "checked": conn.get("checked"),
            "mcp_url": f"{PUBLIC_URL}/mcp/{conn['api_key']}" if conn.get("api_key") else None,
            "api_key": conn.get("api_key")}


@app.get("/api/overview", dependencies=[Depends(admin)])
def overview():
    sites = []
    for s in db.list_sites():
        conn = db.connection_for_domain(s["domain"])
        ops = s["spec"]["operations"]
        sites.append({"domain": s["domain"], "title": s["title"], "operations": len(ops),
                      "verified": sum(o["status"] == "verified" for o in ops),
                      "connection": (connection_view(conn) or {}).get("status")})
    published = {s["domain"] for s in sites}
    running = [{"id": j["id"], "domain": site_domain(j["site_url"]), "status": j["status"]}
               for j in db.list_jobs()
               if j.get("kind", "generate") == "generate" and j["status"] in RUNNING + ("explored", "generated")
               and site_domain(j["site_url"]) not in published]
    return {"sites": sites, "running": running}


# --- Generate ------------------------------------------------------------------------------------

class NewJob(BaseModel):
    site_url: str
    hints: str | None = None


@app.post("/api/jobs", dependencies=[Depends(admin)])
async def create_job(body: NewJob):
    url = body.site_url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    return {"id": jobs.start_job(url, body.hints)}


@app.get("/api/jobs", dependencies=[Depends(admin)])
def list_jobs():
    return [{k: v for k, v in j.items() if k not in ("cdp", "vnc", "view_token")} for j in db.list_jobs()]


PHASES = ["login", "explore", "generate", "verify", "publish"]


def phase_of(job, ops):
    s = job["status"]
    if s in ("starting",) or (s == "needs_human" and not db.request_count(job["id"])):
        return "login"
    if s in ("exploring", "needs_human", "explored"):
        return "explore"
    if s == "generating":
        return "verify" if any(o["status"] in ("verifying", "verified") for o in ops) else "generate"
    if s in ("generated", "publishing"):
        return "publish"
    if s == "published":
        return "done"
    return s


@app.get("/api/jobs/{job_id}", dependencies=[Depends(admin)])
def get_job(job_id: str, after: int = 0):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    ops = db.operations(job_id)
    with db.conn() as c:
        step_rows = c.execute("SELECT step, label, detail FROM steps WHERE job_id=? ORDER BY step", (job_id,)).fetchall()
    endpoints = []
    for r in step_rows:
        for ep in (json.loads(r["detail"] or "{}") or {}).get("new_endpoints", []):
            endpoints.append({"endpoint": ep, "label": r["label"], "step": r["step"]})
    usage = db.usage_summary(job_id)
    return {
        "job": {k: v for k, v in job.items() if k not in ("cdp", "vnc", "view_token")},
        "domain": site_domain(job["site_url"]),
        "phase": phase_of(job, ops),
        "live_url": live_url(job),
        "steps": max((r["step"] for r in step_rows), default=0),
        "requests": db.request_count(job_id),
        "endpoints": endpoints,
        "operations": [{"name": o["name"], "endpoint": o["endpoint"], "status": o["status"],
                        "attempts": o["attempts"], "side_effect": (o["spec"] or {}).get("side_effect")}
                       for o in ops],
        "cost_usd": round(sum(p["cost_usd"] or 0 for p in usage.values()), 4),
        "events": db.events(job_id, after),
    }


@app.post("/api/jobs/{job_id}/human-done", dependencies=[Depends(admin)])
def human_done(job_id: str):
    ev = jobs.human_done.get(job_id)
    if not ev:
        raise HTTPException(409, "job is not waiting for a human")
    ev.set()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/generate", dependencies=[Depends(admin)])
async def generate(job_id: str, retry_failed: bool = False):
    if not db.get_job(job_id):
        raise HTTPException(404)
    jobs.start_generation(job_id, retry_failed)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/publish", dependencies=[Depends(admin)])
async def publish(job_id: str, create_connection: bool = False):
    if not db.get_job(job_id):
        raise HTTPException(404)
    jobs.start_publish(job_id, create_connection)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/operations", dependencies=[Depends(admin)])
def list_operations(job_id: str):
    return db.operations(job_id)


@app.get("/api/jobs/{job_id}/usage", dependencies=[Depends(admin)])
def job_usage(job_id: str):
    return db.usage_summary(job_id)


@app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(admin)])
async def stop(job_id: str):
    await jobs.stop_job(job_id)
    return {"ok": True}


# --- Site page -----------------------------------------------------------------------------------

@app.get("/api/sites/{domain}", dependencies=[Depends(admin)])
def site_detail(domain: str):
    site = db.get_site(domain)
    if not site:
        raise HTTPException(404)
    ops = []
    for o in site["spec"]["operations"]:
        s = o["spec"]
        public = site["spec"]["openapi"]["paths"].get(f"/v1/{domain}/{o['name']}", {}).get("post", {})
        ops.append({"name": o["name"], "status": o["status"], "side_effect": s.get("side_effect"),
                    "summary": s.get("summary"), "description": s.get("description"), "undo": s.get("undo"),
                    "params": [{k: p.get(k) for k in ("name", "type", "required", "description")}
                               for p in s.get("params", [])],
                    "returns": s.get("returns", []),
                    "example": {p: v.get("example") for p, v in public.get("requestBody", {}).get("content", {})
                                .get("application/json", {}).get("schema", {}).get("properties", {}).items()
                                if v.get("example") is not None},
                    "code": o.get("public_code") or o["code"]})
    conn = db.connection_for_domain(domain)
    job = db.get_job(site["job_id"])
    return {"domain": domain, "title": site["title"], "published": site["published"],
            "operations": ops, "connection": connection_view(conn),
            "openapi_url": f"{PUBLIC_URL}/specs/{domain}/openapi.json",
            "download_url": f"{PUBLIC_URL}/specs/{domain}/download.zip",
            "rest_base": f"{PUBLIC_URL}/v1/{domain}",
            "generation_cost_usd": race.generation_cost(domain),
            "generated_from": job["site_url"] if job else None}


@app.post("/api/sites/{domain}/connect", dependencies=[Depends(admin)])
async def connect(domain: str):
    conn = db.connection_for_domain(domain)
    try:
        return {"job_id": jobs.start_connect(domain, conn["id"] if conn else None)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/sites/{domain}/check", dependencies=[Depends(admin)])
async def check(domain: str):
    conn = db.connection_for_domain(domain)
    if not conn:
        raise HTTPException(404, "not connected")
    return {"status": await gateway.check_connection(conn, PUBLIC_URL)}


# --- Reconnect links (no site password; the one-time token is the credential) ---------------------

def valid_token(token):
    t = db.get_reconnect_token(token)
    if not t or t["used"] or t["expires"] < time.time():
        raise HTTPException(410, "this link has expired")
    return t


@app.get("/api/r/{token}")
async def reconnect_status(token: str):
    t = valid_token(token)
    if not t["job_id"]:
        job_id = jobs.start_connect(t["domain"], t["connection_id"])
        db.update_reconnect_token(token, job_id=job_id)
        t["job_id"] = job_id
    job = db.get_job(t["job_id"])
    if job["status"] == "connected":
        db.update_reconnect_token(token, used=1)
    return {"domain": t["domain"], "status": job["status"], "live_url": live_url(job)}


@app.post("/api/r/{token}/done")
def reconnect_done(token: str):
    t = valid_token(token)
    ev = jobs.human_done.get(t["job_id"] or "")
    if ev:
        ev.set()
    return {"ok": True}


# --- Race ----------------------------------------------------------------------------------------

class RaceRequest(BaseModel):
    domain: str
    task: str


@app.get("/api/race/presets/{domain}", dependencies=[Depends(admin)])
def race_presets(domain: str):
    return {"tasks": race.presets(domain), "frontier": FRONTIER_MODEL if ANTHROPIC_API_KEY else None,
            "open": BROWSE_MODEL}


@app.post("/api/race", dependencies=[Depends(admin)])
async def start_race(body: RaceRequest):
    conn = db.connection_for_domain(body.domain)
    if not conn or conn["status"] != "active":
        raise HTTPException(409, "connect this site first")
    return {"race_id": race.start_race(conn, body.task, PUBLIC_URL, jobs.tasks)}


@app.get("/api/race/{race_id}", dependencies=[Depends(admin)])
def race_status(race_id: str):
    job = db.get_job(race_id)
    if not job or job.get("kind") != "race":
        raise HTTPException(404)
    contestants, result = {}, None
    for e in db.events(race_id):
        d = e["data"] or {}
        if e["kind"] == "race_result":
            result = d
            continue
        name = d.get("contestant")
        if not name:
            continue
        c = contestants.setdefault(name, {"steps": []})
        if e["kind"] == "race_sandbox":
            c["live_url"] = live_url(db.get_job(d["job_id"]))
        elif e["kind"] == "race_step":
            c["steps"].append({"step": d.get("step"), "action": d.get("action"), "thought": d.get("thought")})
    usage = db.usage_summary(race_id)
    for name, c in contestants.items():
        u = usage.get(name, {})
        c["model_tokens"] = (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)
        c["cost_usd"] = round(u.get("cost_usd") or 0, 6)
    return {"task": job["hints"], "status": job["status"], "detail": job["status_detail"],
            "started": job["created"], "contestants": contestants, "result": result}


# --- Public: docs, download, REST gateway --------------------------------------------------------

@app.get("/specs")
def public_specs():
    return [{"domain": s["domain"], "title": s["title"], "operations": len(s["spec"]["operations"]),
             "openapi": f"{PUBLIC_URL}/specs/{s['domain']}/openapi.json"} for s in db.list_sites()]


@app.get("/specs/{domain}/openapi.json")
def public_openapi(domain: str):
    site = db.get_site(domain)
    if not site:
        raise HTTPException(404)
    return site["spec"]["openapi"]


@app.get("/specs/{domain}/download.zip")
def public_download(domain: str):
    """The published (redacted) operations, runnable with runtime.py and your own session cookies."""
    from .publisher import RUNTIME_SRC
    site = db.get_site(domain)
    if not site:
        raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{domain}/openapi.json", json.dumps(site["spec"]["openapi"], indent=2))
        z.writestr(f"{domain}/ops/_site.py", site["spec"]["site_module"])
        z.writestr(f"{domain}/ops/runtime.py", RUNTIME_SRC.read_text())
        for o in site["spec"]["operations"]:
            z.writestr(f"{domain}/ops/{o['module']}.py", o.get("public_code") or o["code"])
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{domain}-skeleton-key.zip"'})


@app.post("/v1/{domain}/{operation}")
async def call_operation(domain: str, operation: str, request: Request):
    auth = request.headers.get("authorization", "")
    try:
        conn = gateway.connection_for_key(auth.removeprefix("Bearer ").strip())
        if conn["domain"] != domain:
            raise gateway.GatewayError("unauthorized", f"this API key is for {conn['domain']}", 403)
        try:
            params = await request.json() if await request.body() else {}
        except ValueError:
            raise gateway.GatewayError("bad_request", "body must be a JSON object")
        output, meta = await gateway.execute(conn, operation, params or {}, PUBLIC_URL)
        return JSONResponse(output, headers={"X-SK-Seconds": str(meta["seconds"])})
    except gateway.GatewayError as e:
        return JSONResponse({"error": e.code, "message": e.message, **e.extra}, status_code=e.status)
