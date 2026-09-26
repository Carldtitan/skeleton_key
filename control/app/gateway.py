"""Executes published operations for API/MCP callers.

A caller holds an API key for one connection (one user's session on one site). The key maps to
stored cookies; the operation runs in a throwaway runner container on a worker, and the cookies
never reach the caller or any LLM.
"""
import hashlib
import secrets
import time

from . import db
from .endpoints import site_domain
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
    return f"{base_url}/connect/{conn['domain']}?connection={conn['id']}"


async def execute(conn, op_name, params, base_url=""):
    """Run one operation for a connection. Returns (output, meta) or raises GatewayError."""
    domain = conn["domain"]
    site, op = find_operation(domain, op_name)
    validate_params(op, params)
    if conn["status"] == "expired":
        raise GatewayError("session_expired", "the site session expired; a human must log in again", 401,
                           reconnect_url=reconnect_url(conn, base_url))
    session = {"cookies": {c["name"]: c["value"] for c in conn["cookies"]
                           if c["domain"].lstrip(".").endswith(site_domain(domain))}}
    module = op["module"]
    files = {"_site.py": site["spec"]["site_module"], f"{module}.py": op["code"]}
    started = time.monotonic()
    res = await run_calls(files, session, [{"op": module, "params": params}])
    seconds = round(time.monotonic() - started, 3)
    result = (res.get("results") or [{}])[0]
    if result.get("ok"):
        return result.get("output"), {"seconds": seconds, "model_tokens": 0}
    code = result.get("error_code") or "upstream_error"
    if code == "session_expired":
        db.update_connection(conn["id"], status="expired")
        raise GatewayError(code, "the site session expired; a human must log in again", 401,
                           reconnect_url=reconnect_url(conn, base_url))
    status = {"not_found": 404, "rate_limited": 429, "bad_request": 400, "blocked": 403}.get(code, 502)
    raise GatewayError(code, (result.get("error") or res.get("error") or "operation failed")[:500], status)
