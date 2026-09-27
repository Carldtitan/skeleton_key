"""Executes published operations for API/MCP callers.

A caller holds an API key for one connection (one user's session on one site). The key maps to
stored cookies; the operation runs in a throwaway runner container on a worker, and the cookies
never reach the caller or any LLM.
"""
import hashlib
import re
import secrets
import time

from . import db
from .endpoints import site_domain
from .gateway_errors import looks_unauthenticated
from .generator import run_calls


class GatewayError(Exception):
    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_connection(domain, cookies, job_id):
    """Create a connection and return (connection_id, api_key). The key is only ever shown once."""
    conn_id = "conn_" + secrets.token_hex(6)
    key = "sk_" + secrets.token_urlsafe(24)
    db.save_connection(conn_id, domain, hash_key(key), cookies, job_id)
    db.update_connection(conn_id, api_key=key)
    return conn_id, key


def connection_for_key(key):
    conn = db.connection_by_key_hash(hash_key(key or ""))
    if not conn:
        raise GatewayError("unauthorized", "unknown API key", 401)
    return conn


def find_operation(domain, op_name):
    site = db.get_site(domain)
    if not site:
        raise GatewayError("not_found", f"no published API for {domain}", 404)
    for op in site["spec"]["operations"]:
        if op["name"] == op_name:
            return site, op
    raise GatewayError("not_found", f"{domain} has no operation {op_name}", 404)


def validate_params(op, params):
    declared = {p["name"]: p for p in op["spec"].get("params", [])}
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise GatewayError("bad_request", f"unknown parameters: {unknown}; allowed: {sorted(declared)}")
    missing = [n for n, p in declared.items() if p.get("required") and n not in params]
    if missing:
        raise GatewayError("bad_request", f"missing required parameters: {missing}")


def reconnect_url(conn, base_url):
    """A one-time link that opens only this connection's login page (no site password needed)."""
    token = secrets.token_urlsafe(18)
    db.create_reconnect_token(token, conn["id"], conn["domain"])
    return f"{base_url}/#/r/{token}"


def session_of(conn):
    """Cookies for the site plus any auth headers (bearer tokens) captured with the session."""
    return {"cookies": {c["name"]: c["value"] for c in conn["cookies"]
                        if c["domain"].lstrip(".").endswith(site_domain(conn["domain"]))},
            "headers": conn.get("auth_headers") or {}}


async def _run(site, op, session, params):
    files = {"_site.py": site["spec"]["site_module"], f"{op['module']}.py": op["code"]}
    res = await run_calls(files, session, [{"op": op["module"], "params": params}])
    return res, (res.get("results") or [{}])[0]


async def refresh_auth(conn):
    """Mint fresh auth headers (bearer tokens expire) without a human, and store them on the connection."""
    from . import auth_tokens
    if not conn.get("auth_headers") and not conn.get("storage_state"):
        return False
    job = db.get_job(conn["job_id"]) if conn.get("job_id") else None
    site = db.get_site(conn["domain"])
    headers = await auth_tokens.refresh(conn, job.get("cdp") if job and job.get("view_token") else None,
                                        auth_tokens.site_root(site["spec"]["login_url"]))
    if headers:
        conn["auth_headers"] = headers
        db.update_connection(conn["id"], auth_headers=headers)
        return True
    return False


def identity_values(output, prefix=""):
    """Id-like values in a get_current_user result, keyed by their path (e.g. {"user_id": "1qIus..."})."""
    found = {}
    if isinstance(output, dict):
        for k, v in output.items():
            found.update(identity_values(v, f"{prefix}{k}."))
    elif isinstance(output, str) and len(output) >= 8 and re.search(r"\d", output)             and not re.search(r"\s|://|@", output) and not re.match(r"\d{4}-\d{2}-\d{2}", output):  # not dates
        found[prefix.rstrip(".")] = output
    return found


def site_identity(site):
    """The generating account's ids. Generated code can contain them (e.g. a userId in a request body)."""
    if site["spec"].get("identity") is not None:
        return site["spec"]["identity"]
    me = next((o for o in db.operations(site["job_id"])
               if o["name"] == "get_current_user" and o["status"] == "verified"), None)
    return identity_values(((me or {}).get("last_result") or [{}])[0].get("output"))


async def connection_identity(conn, site):
    """This connection's own ids, looked up once with get_current_user and cached on the connection."""
    if conn.get("identity") is not None:
        return conn["identity"]
    op = next((o for o in site["spec"]["operations"] if o["name"] == "get_current_user"), None)
    identity = {}
    if op:
        _, result = await _run(site, op, session_of(conn), {})
        if result.get("ok"):
            identity = identity_values(result.get("output"))
    conn["identity"] = identity
    db.update_connection(conn["id"], identity=identity)
    return identity


async def personalize(conn, site, op):
    """Swap the generating account's ids in an operation's code for this connection's ids, so an API
    generated from one account works for any account that connects."""
    theirs = site_identity(site)
    if not theirs or op["name"] == "get_current_user":
        return op
    mine = await connection_identity(conn, site)
    code = op["code"]
    for path, old in theirs.items():
        new = mine.get(path)
        if new and new != old:
            code = code.replace(old, new)
    return op if code == op["code"] else {**op, "code": code}


async def execute(conn, op_name, params, base_url=""):
    """Run one operation for a connection. Returns (output, meta) or raises GatewayError."""
    domain = conn["domain"]
    site, op = find_operation(domain, op_name)
    validate_params(op, params)
    if conn["status"] == "expired":
        raise GatewayError("session_expired", "the site session expired; a human must log in again", 401,
                           reconnect_url=reconnect_url(conn, base_url))
    op = await personalize(conn, site, op)
    started = time.monotonic()
    res, result = await _run(site, op, session_of(conn), params)
    if not result.get("ok") and looks_unauthenticated(result) and await refresh_auth(conn):
        res, result = await _run(site, op, session_of(conn), params)  # retry once with a fresh token
    seconds = round(time.monotonic() - started, 3)
    if result.get("ok"):
        return result.get("output"), {"seconds": seconds, "model_tokens": 0}
    code = result.get("error_code") or "upstream_error"
    if code == "session_expired":
        # One refused request isn't proof the login is gone (sites and bot protection refuse single calls).
        # Only expire the connection if a cheap session probe is refused too.
        probe = probe_operation(domain)
        if probe and probe != op_name and await _session_alive(conn, site, probe):
            raise GatewayError("upstream_error", "the site refused this request, but the session is still valid", 502)
        db.update_connection(conn["id"], status="expired")
        raise GatewayError(code, "the site session expired; a human must log in again", 401,
                           reconnect_url=reconnect_url(conn, base_url))
    status = {"not_found": 404, "rate_limited": 429, "bad_request": 400, "blocked": 403}.get(code, 502)
    raise GatewayError(code, (result.get("error") or res.get("error") or "operation failed")[:500], status)


async def _session_alive(conn, site, probe):
    op = next(o for o in site["spec"]["operations"] if o["name"] == probe)
    _, result = await _run(site, op, session_of(conn), {})
    return bool(result.get("ok"))


def probe_operation(domain):
    """The cheapest read to test a session: get_current_user, else any read with no required params."""
    site = db.get_site(domain)
    if not site:
        return None
    reads = [o for o in site["spec"]["operations"]
             if o["spec"].get("side_effect") == "read" and o["status"] == "verified"
             and not any(p.get("required") for p in o["spec"].get("params", []))]
    return next((o["name"] for o in reads if o["name"] == "get_current_user"), reads[0]["name"] if reads else None)


async def check_connection(conn, base_url=""):
    op = probe_operation(conn["domain"])
    if not op or conn["status"] != "active":
        return conn["status"]
    try:
        await execute(conn, op, {}, base_url)
        status = "active"
    except GatewayError as e:
        status = "expired" if e.code == "session_expired" else "active"
    db.update_connection(conn["id"], status=status, checked=time.time())
    return status


async def health_loop(interval, base_url=""):
    import asyncio
    while True:
        await asyncio.sleep(interval)
        for c in db.list_connections():
            try:
                await check_connection(db.get_connection(c["id"]), base_url)
            except Exception:
                pass
