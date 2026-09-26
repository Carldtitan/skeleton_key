"""Proxies a sandbox's noVNC screen through the control VM.

Sandbox ports stay on the private VPC. The human gets /live/{job}/{token}/..., and the token
is cleared when the job's sandbox is destroyed, so the link expires with the workload.
"""
import asyncio
import secrets

import httpx
import websockets
from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response

from . import db

router = APIRouter()


def sandbox_vnc(job_id, token):
    job = db.get_job(job_id)
    if not job or not job["view_token"] or not job["vnc"] or not secrets.compare_digest(job["view_token"], token):
        raise HTTPException(404, "live view not available")
    return job["vnc"]


@router.websocket("/live/{job_id}/{token}/websockify")
async def live_ws(ws: WebSocket, job_id: str, token: str):
    try:
        vnc = sandbox_vnc(job_id, token)
    except HTTPException:
        await ws.close(code=4404)
        return
    # Echo the client's subprotocol only if it offered one; browsers reject an unrequested one.
    offered = ws.scope.get("subprotocols") or []
    proto = "binary" if "binary" in offered else None
    await ws.accept(subprotocol=proto)
    try:
        upstream_cm = websockets.connect(f"ws://{vnc}/websockify", subprotocols=[proto] if proto else None,
                                         max_size=None)
        upstream = await upstream_cm.__aenter__()
    except OSError:
        await ws.close(code=4410)  # sandbox already gone
        return
    try:
        async def client_to_upstream():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                await upstream.send(msg.get("bytes") or msg.get("text"))

        async def upstream_to_client():
            async for data in upstream:
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)

        done, pending = await asyncio.wait(
            [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        await upstream_cm.__aexit__(None, None, None)


@router.get("/live/{job_id}/{token}/{path:path}")
async def live_http(job_id: str, token: str, path: str):
    vnc = sandbox_vnc(job_id, token)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"http://{vnc}/{path}")
    except httpx.HTTPError:
        return Response("browser ended", status_code=410, media_type="text/plain")  # sandbox already gone
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/octet-stream"))
