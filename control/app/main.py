import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import db, gateway, jobs, liveview, mcp_server, race
from .config import PUBLIC_URL

@asynccontextmanager
async def lifespan(_app):
    db.init()
    # Jobs don't survive a restart (their asyncio tasks are gone); mark them so the UI is honest.
    for j in db.list_jobs():
        if j["status"] in ("starting", "needs_human", "exploring", "generating", "publishing", "racing"):
            db.update_job(j["id"], status="interrupted", status_detail="control plane restarted")
    yield


app = FastAPI(title="Skeleton Key", lifespan=lifespan)
security = HTTPBasic()
ADMIN_PASSWORD = os.environ["SK_ADMIN_PASSWORD"]


def admin(creds: HTTPBasicCredentials = Depends(security)):
    if not secrets.compare_digest(creds.password, ADMIN_PASSWORD):
        raise HTTPException(401, "bad password", headers={"WWW-Authenticate": "Basic"})


app.include_router(liveview.router)
app.include_router(mcp_server.router)


class NewJob(BaseModel):
    site_url: str
    hints: str | None = None


@app.post("/api/jobs", dependencies=[Depends(admin)])
async def create_job(body: NewJob):
    return {"id": jobs.start_job(body.site_url, body.hints)}


@app.get("/api/jobs", dependencies=[Depends(admin)])
def list_jobs():
    return [{k: v for k, v in j.items() if k not in ("cdp", "vnc")} for j in db.list_jobs()]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(admin)])
def get_job(job_id: str, after: int = 0):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404)
    live = (f"/live/{job_id}/{job['view_token']}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
            f"&path=websockify") if job["view_token"] and job["vnc"] else None  # noVNC resolves path relative to the page
    return {"job": {k: v for k, v in job.items() if k not in ("cdp", "vnc", "view_token")},
            "live_url": live, "requests": db.request_count(job_id), "events": db.events(job_id, after)}


@app.post("/api/jobs/{job_id}/human-done", dependencies=[Depends(admin)])
def human_done(job_id: str):
    ev = jobs.human_done.get(job_id)
    if not ev:
        raise HTTPException(409, "job is not waiting for a human")
    ev.set()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/generate", dependencies=[Depends(admin)])
async def generate(job_id: str, retry_failed: bool = False):  # async: create_task needs the running loop
    if not db.get_job(job_id):
        raise HTTPException(404)
    jobs.start_generation(job_id, retry_failed)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/operations", dependencies=[Depends(admin)])
def list_operations(job_id: str):
    return db.operations(job_id)


@app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(admin)])
async def stop(job_id: str):
    await jobs.stop_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/publish", dependencies=[Depends(admin)])
async def publish(job_id: str, create_connection: bool = False):
    if not db.get_job(job_id):
        raise HTTPException(404)
    jobs.start_publish(job_id, create_connection)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/usage", dependencies=[Depends(admin)])
def job_usage(job_id: str):
    return db.usage_summary(job_id)


# --- Catalog, connections, race (operator API) -------------------------------------------------

@app.get("/api/sites", dependencies=[Depends(admin)])
def sites():
    return [{"domain": s["domain"], "title": s["title"], "job_id": s["job_id"], "published": s["published"],
             "operations": [{"name": o["name"], "status": o["status"], "side_effect": o["spec"].get("side_effect"),
                             "summary": o["spec"].get("summary")} for o in s["spec"]["operations"]],
             "generation_cost_usd": race.generation_cost(s["domain"])} for s in db.list_sites()]


@app.get("/api/connections", dependencies=[Depends(admin)])
def connections():
    return db.list_connections()


@app.post("/api/connect/{domain}", dependencies=[Depends(admin)])
async def connect(domain: str, connection: str | None = None):
    try:
        return {"job_id": jobs.start_connect(domain, connection)}
    except ValueError as e:
        raise HTTPException(404, str(e))


class RaceRequest(BaseModel):
    connection_id: str
    task: str
    operation: str
    params: dict = {}


@app.post("/api/race", dependencies=[Depends(admin)])
async def start_race(body: RaceRequest):
    conn = db.get_connection(body.connection_id)
    if not conn:
        raise HTTPException(404, "no such connection")
    return {"race_id": race.start_race(conn, body.task, body.operation, body.params, PUBLIC_URL, jobs.tasks)}


# --- Public: docs, REST gateway, MCP ------------------------------------------------------------

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


@app.get("/connect/{domain}", dependencies=[Depends(admin)])
async def reconnect(domain: str, connection: str | None = None):
    """Target of reconnect_url: starts a login-only job; the operator page shows its live view."""
    try:
        job_id = jobs.start_connect(domain, connection)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse(f"/?job={job_id}")


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
        return JSONResponse(output, headers={"X-SK-Seconds": str(meta["seconds"]), "X-SK-Model-Tokens": "0"})
    except gateway.GatewayError as e:
        return JSONResponse({"error": e.code, "message": e.message, **e.extra}, status_code=e.status)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
def index():
    return DEV_PAGE


# Temporary operator page for testing the pipeline; the real UI replaces it.
DEV_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Skeleton Key (dev)</title>
<style>body{font:14px system-ui;margin:16px;background:#111;color:#eee}input,button{font:inherit;padding:6px}
#wrap{display:flex;gap:16px}iframe{width:1000px;height:660px;border:1px solid #444;background:#000}
#log{width:420px;height:660px;overflow:auto;font:12px monospace;white-space:pre-wrap;background:#1b1b1b;padding:8px}
#status{font-weight:bold;margin:8px 0}.human{color:#ffb020}</style></head><body>
<form id=f><input id=url size=40 value="https://luma.com/signin"> <input id=hints size=50 placeholder="hints (optional)">
<button>Start job</button></form>
<div id=status></div><button id=done>Done (I finished)</button> <button id=stop>Stop job</button>
<div id=wrap><iframe id=live></iframe><div id=log></div></div>
<script>
let job=new URLSearchParams(location.search).get('job'), after=0, liveSet=false;
f.onsubmit=async e=>{e.preventDefault();const btn=f.querySelector('button');if(btn.disabled)return;btn.disabled=true;
 try{const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({site_url:url.value,hints:hints.value||null})});job=(await r.json()).id;after=0;liveSet=false;log.textContent='';}
 finally{setTimeout(()=>btn.disabled=false,5000)}};
done.onclick=()=>job&&fetch(`/api/jobs/${job}/human-done`,{method:'POST'});
stop.onclick=()=>job&&fetch(`/api/jobs/${job}/stop`,{method:'POST'});
setInterval(async()=>{if(!job)return;const d=await (await fetch(`/api/jobs/${job}?after=${after}`)).json();
 status.textContent=`job ${job}: ${d.job.status} — ${d.job.status_detail||''} | ${d.requests} requests`;
 status.className=d.job.status==='needs_human'?'human':'';
 if(d.live_url&&!liveSet){live.src=d.live_url;liveSet=true}
 for(const ev of d.events){after=ev.id;log.textContent+=`${new Date(ev.ts*1000).toLocaleTimeString()} ${ev.kind} ${JSON.stringify(ev.data)}\\n`;log.scrollTop=1e9}
},1500);
</script></body></html>"""
