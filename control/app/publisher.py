"""Publishes a job's verified operations as a site API: consistent names, no personal data, OpenAPI.

Steps: consistency pass (one name per concept across operations) -> re-verify renamed operations ->
redact the logged-in user's personal data from docs and examples -> OpenAPI + code to Object Storage.
"""
import ast
import asyncio
import json
import os
import pathlib
import re
import time
from collections import Counter

import boto3

from . import db, llm
from .config import CODE_MODEL
from .endpoints import is_auth_path, site_domain
from .generator import Generator, check, module_name

BUCKET = "sk-specs"
PUBLISHABLE = {"verified", "unverified_irreversible", "unverified_no_undo"}
RUNTIME_SRC = pathlib.Path(__file__).with_name("runtime_published.py")

CONSISTENCY_PROMPT = """These are the generated API operations for {site}. Make them feel like ONE consistent API for AI agents.

{ops}

Rules:
- The same concept must use the same parameter name everywhere (e.g. always `category_api_id`, never both
  `discover_category_api_id` and `category_api_id`).
- Operation names: snake_case verb_noun; keep them if already fine.
- Every operation name must be unique. Drop an operation only if another one does exactly the same thing.
Reply with ONLY JSON:
{{"rename_ops": {{"old_name": "new_name"}}, "rename_params": {{"op_name": {{"old_param": "new_param"}}}},
  "drop": ["op_name"], "notes": "<one line>"}}
Use the ORIGINAL operation names as keys. Empty objects/lists if nothing to change."""

PII_PROMPT = """Below is API documentation generated from one real person's logged-in session. Find every string that is
personal data of that person or of other private individuals: names, emails, phone numbers, usernames/handles,
user ids, profile/avatar URLs, addresses, precise locations, private event names. Public organisation or
category names are fine.

{doc}

Reply with ONLY JSON: {{"personal": ["<exact string as it appears>", ...]}}"""


def _preference(o):
    """Which of two same-named operations to keep: verified first, then the site's real API over page-data
    scrapes (Next.js /_next/data), then the one that needs fewer required parameters."""
    required = sum(1 for p in o["spec"].get("params", []) if p.get("required"))
    return (o["status"] == "verified", "/_next/data/" not in o["endpoint"], -required)


def unique_names(ops):
    """Operation names must be unique (MCP tool names, REST paths, OpenAPI operationIds). When several
    operations share a name they do the same thing, so keep the best one. Returns (ops, {kept: [dropped]})."""
    groups = {}
    for o in ops:
        groups.setdefault(o["name"], []).append(o)
    kept, merged = [], {}
    for name, group in groups.items():
        best = max(group, key=_preference)
        kept.append(best)
        if len(group) > 1:
            merged[name] = [g["endpoint"] for g in group if g is not best]
    return kept, merged


class RenameArgs(ast.NodeTransformer):
    """Renames the parameters of run() and every use of them inside it."""

    def __init__(self, mapping):
        self.mapping = mapping

    def visit_arg(self, node):
        node.arg = self.mapping.get(node.arg, node.arg)
        return node

    def visit_Name(self, node):
        node.id = self.mapping.get(node.id, node.id)
        return node


def rename_params_in_code(code, mapping):
    tree = ast.parse(code)
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name == "run":
            RenameArgs(mapping).visit(fn)
    return ast.unparse(tree) + "\n"


def placeholder_for(value):
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", value):
        return "user@example.com"
    if re.match(r"https?://", value):
        return "https://example.com/redacted"
    if re.fullmatch(r"[a-z]{2,5}-[A-Za-z0-9]{6,}", value):
        return value.split("-")[0] + "-XXXXXXXXXXXX"
    if re.fullmatch(r"\+?[\d\s().-]{7,}", value):
        return "+1 555 0100"
    return "REDACTED"


def personal_values(job_id, ops, cookies):
    """Known personal strings: the user's own profile fields and every cookie value."""
    values = {c["value"] for c in cookies if len(c.get("value", "")) >= 6}
    me = next((o for o in ops if o["name"] == "get_current_user" and o["status"] == "verified"), None)
    stack = [((me or {}).get("last_result") or [{}])[0].get("output")]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str) and len(v) >= 3 and v.lower() not in ("true", "false", "none", "null"):
            values.add(v)
    return values


def redact_value(obj, values):
    if isinstance(obj, dict):
        return {k: redact_value(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_value(v, values) for v in obj]
    if isinstance(obj, str):
        out = obj
        for v in sorted(values, key=len, reverse=True):
            if v and v in out:
                out = out.replace(v, placeholder_for(v))
        return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", out)
    return obj


def json_type(t):
    return {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
            "array": "array", "object": "object"}.get(str(t).lower(), "string")


def openapi(domain, title, ops, base_url):
    paths = {}
    for op in ops:
        s = op["spec"]
        props = {p["name"]: {"type": json_type(p.get("type")), "description": p.get("description", ""),
                             **({"example": p["example"]} if p.get("example") is not None else {})}
                 for p in s.get("params", [])}
        required = [p["name"] for p in s.get("params", []) if p.get("required")]
        returns = {r["name"]: {"type": json_type(r.get("type")), "description": r.get("description", "")}
                   for r in s.get("returns", [])}
        paths[f"/v1/{domain}/{op['name']}"] = {"post": {
            "operationId": op["name"],
            "summary": s.get("summary", ""),
            "description": s.get("description", ""),
            "requestBody": {"required": bool(required), "content": {"application/json": {"schema": {
                "type": "object", "properties": props, "required": required, "additionalProperties": False}}}},
            "responses": {
                "200": {"description": "OK", "content": {"application/json": {"schema": {
                    "type": "object", "properties": returns}}}},
                "401": {"$ref": "#/components/responses/SessionExpired"},
                "4XX": {"$ref": "#/components/responses/Error"},
            },
            "x-side-effect": s.get("side_effect"),
            "x-undo": s.get("undo"),
            "x-verified": op["status"] == "verified",
        }}
    return {
        "openapi": "3.1.0",
        "info": {"title": f"{title} (unofficial API by Skeleton Key)", "version": time.strftime("%Y.%m.%d"),
                 "description": (f"Granular operations for {domain}, generated by exploring the web app and verified "
                                 "against a live session. One operation = one user action. Authenticate with the API "
                                 "key of your connection; your session cookies never leave Skeleton Key.")},
        "servers": [{"url": base_url}],
        "security": [{"apiKey": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer"}},
            "responses": {
                "SessionExpired": {"description": "The site session expired; open reconnect_url and log in again.",
                                   "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                "Error": {"description": "Typed error", "content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/Error"}}}},
            },
            "schemas": {"Error": {"type": "object", "properties": {
                "error": {"type": "string", "enum": ["session_expired", "blocked", "not_found", "rate_limited",
                                                     "bad_request", "upstream_error", "unauthorized"]},
                "message": {"type": "string"}, "reconnect_url": {"type": "string"}}}},
        },
    }


def _s3():
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
                        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                        aws_secret_access_key=os.environ["S3_SECRET_KEY"])


def upload(domain, files):
    s3 = _s3()
    for name, body in files.items():
        ctype = "application/json" if name.endswith(".json") else "text/markdown" if name.endswith(".md") \
            else "text/x-python"
        s3.put_object(Bucket=BUCKET, Key=f"{domain}/{name}", Body=body.encode(), ContentType=ctype)


def readme(domain, title, ops, base_url):
    lines = [f"# {title}: unofficial API", "",
             f"Generated by Skeleton Key from the {domain} web app. Every operation below was verified against a "
             "live logged-in session unless marked otherwise.", "",
             "## Use it from an agent", "",
             f"- MCP: add `{base_url}/mcp/<your-api-key>` as a custom connector.",
             f"- REST: `POST {base_url}/v1/{domain}/<operation>` with `Authorization: Bearer <api-key>` and a JSON body.",
             "- Self-host: run `ops/<operation>.py` with `runtime.py` and your own session cookies.", "",
             "## Operations", "", "| operation | effect | verified | summary |", "|---|---|---|---|"]
    for op in ops:
        s = op["spec"]
        lines.append(f"| `{op['name']}` | {s.get('side_effect')} | {'yes' if op['status'] == 'verified' else 'no'} "
                     f"| {s.get('summary', '')} |")
    return "\n".join(lines) + "\n"


async def publish(job_id, base_url):
    job = db.get_job(job_id)
    domain = site_domain(job["site_url"])
    title = domain.split(".")[0].capitalize()
    gen = Generator(job_id, job["site_url"])
    # Login steps are never operations: the connection's session covers auth.
    ops = [o for o in db.operations(job_id) if o["status"] in PUBLISHABLE and o["spec"]
           and not is_auth_path(o["endpoint"].split(" ", 1)[-1])]
    for o in ops:
        o["name"] = o["spec"]["name"]
    db.add_event(job_id, "status", {"status": "publishing", "detail": f"{len(ops)} operations"})

    # 1. Consistency pass.
    listing = "\n".join(
        f"- {o['name']}({', '.join(p['name'] for p in o['spec'].get('params', []))}) -> "
        f"{', '.join(r['name'] for r in o['spec'].get('returns', []))}: {o['spec'].get('summary', '')}" for o in ops)
    try:
        text, _ = await llm.chat(CODE_MODEL, [{"role": "user", "content": CONSISTENCY_PROMPT.format(
            site=domain, ops=listing)}], max_tokens=8000)
        plan = llm.parse_json(text)
    except Exception as e:
        plan = {}
        db.add_event(job_id, "error", {"consistency": str(e)[:200]})
    drop = set(plan.get("drop") or [])
    renamed_ops = []
    for o in ops:
        pmap = (plan.get("rename_params") or {}).get(o["name"]) or {}
        new_name = (plan.get("rename_ops") or {}).get(o["name"])
        if not pmap and not new_name:
            continue
        candidate = json.loads(json.dumps(o))
        try:
            if pmap:
                candidate["code"] = rename_params_in_code(candidate["code"], pmap)
                for p in candidate["spec"].get("params", []):
                    p["name"] = pmap.get(p["name"], p["name"])
            if new_name:
                candidate["spec"]["name"] = candidate["name"] = module_name(new_name)
        except SyntaxError:
            continue
        # Renamed read operations must pass again before we accept the change.
        if candidate["spec"].get("side_effect") == "read":
            results = await gen.verify(candidate)
            if check(results[0], candidate["spec"]):
                db.add_event(job_id, "rename_reverted", {"op": o["name"], "renames": pmap, "new_name": new_name})
                continue
        o.update(candidate)
        renamed_ops.append(o["name"])
    # A dropped name shared by several operations means "one of these is redundant", not "drop them all".
    names = Counter(o["name"] for o in ops)
    ops = [o for o in ops if o["name"] not in drop or names[o["name"]] > 1]
    ops, merged = unique_names(ops)
    if merged:
        db.add_event(job_id, "duplicates_merged", {"kept_over": merged})
    for o in ops:  # keep undo references pointing at the new names
        u = o["spec"].get("undo")
        o["spec"]["undo"] = module_name((plan.get("rename_ops") or {}).get(u, u)) if u else None

    # 2. Redaction: known personal values, then a model pass for anything left.
    values = personal_values(job_id, db.operations(job_id), db.get_session(job_id) or [])
    public_ops = [{"name": o["name"], "status": o["status"], "spec": redact_value(o["spec"], values),
                   "code": o["code"]} for o in ops]
    try:
        text, _ = await llm.chat(CODE_MODEL, [{"role": "user", "content": PII_PROMPT.format(
            doc=json.dumps([o["spec"] for o in public_ops])[:60000])}], max_tokens=6000)
        extra = {s for s in llm.parse_json(text).get("personal", []) if isinstance(s, str) and len(s) >= 3}
    except Exception:
        extra = set()
    values |= extra
    leaks = []
    for o in public_ops:
        o["spec"] = redact_value(o["spec"], values)
        for v in values:
            if v in o["code"]:
                leaks.append(o["name"])
                o["code"] = o["code"].replace(v, placeholder_for(v))
    if leaks:
        db.add_event(job_id, "redacted_code", {"ops": sorted(set(leaks))})

    # 3. Artifacts.
    site_module = "BASE_HEADERS = " + json.dumps(gen.headers, indent=4) + "\n"
    for o in public_ops:
        o["module"] = module_name(o["name"])
    spec = openapi(domain, title, public_ops, base_url)
    files = {"openapi.json": json.dumps(spec, indent=2), "README.md": readme(domain, title, public_ops, base_url),
             "ops/_site.py": site_module, "ops/runtime.py": RUNTIME_SRC.read_text()}
    for o in public_ops:
        files[f"ops/{o['module']}.py"] = o["code"]
    await asyncio.to_thread(upload, domain, files)

    # The gateway runs the un-redacted code (it may need exact constants); docs are what's published.
    private = {o["name"]: o for o in ops}
    manifest_ops = [{**o, "code": private[o["name"]]["code"], "public_code": o["code"]} for o in public_ops]
    from .gateway import identity_values
    me = next((o for o in db.operations(job_id) if o["name"] == "get_current_user" and o["status"] == "verified"), None)
    identity = identity_values(((me or {}).get("last_result") or [{}])[0].get("output"))
    db.save_site(domain, job_id, title, {"operations": manifest_ops, "site_module": site_module,
                                         "login_url": job["site_url"], "openapi": spec, "identity": identity})
    summary = {"domain": domain, "operations": len(public_ops), "renamed": renamed_ops, "dropped": sorted(drop),
               "redacted_values": len(values), "files": sorted(files)}
    db.add_event(job_id, "published", summary)
    return summary
