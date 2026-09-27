"""Turns captured traffic into granular, verified API operations.

One distinct endpoint -> one operation with its own spec and its own small script. Each script
is executed in a throwaway runner container on a worker (never in this process) against the
user's live session, and rewritten from the failure details until it passes.
"""
import asyncio
import json
import re
import time
from collections import Counter
from urllib.parse import urlparse

import httpx

from . import db, lessons, llm
from .config import CODE_MODEL, WORKER_TOKEN, WORKERS
from .endpoints import endpoint_key, is_app_api, is_auth_path, site_domain
from .gateway_errors import looks_unauthenticated

MAX_ATTEMPTS = 4
LLM_CONCURRENCY = 4
RUN_CONCURRENCY = 3
SKIP_HEADERS = {"cookie", "user-agent", "accept-encoding", "accept-language", "priority", "content-length",
                "host", "connection"}
# Login/credential flows are never turned into operations (see endpoints.is_auth_path).

GEN_PROMPT = """You turn one captured web-app request into ONE granular API operation for AI agents.
Granular means one atomic action (like `git add` or `git push`), never a multi-step workflow.

Site: {site}
Endpoint: {endpoint}
User action in the UI that triggered it: {triggers}

Captured samples (real traffic from the logged-in user):
{samples}

Lessons learned on earlier sites (follow them):
{lessons}

Write:
1. A JSON spec object:
{{"include": true|false,
  "reason": "<why this is (not) a useful operation for an agent; exclude pure UI bookkeeping, tracking, prompts>",
  "name": "<snake_case verb_noun, e.g. list_my_events, get_event, subscribe_category>",
  "summary": "<one line>",
  "description": "<for an agent: when to call it, what it returns>",
  "side_effect": "read" | "reversible_write" | "irreversible_write",
  "undo": "<snake_case name of the operation that reverses this write, or null>",
  "params": [{{"name": "...", "type": "string|integer|boolean|number|array|object", "required": true|false,
              "description": "...", "example": <value taken from the samples>}}],
  "returns": [{{"name": "...", "type": "...", "description": "..."}}]}}
Only fields a user would choose become params (ids, search text, filters, page cursor). Constants, tracking
values and client versions stay hard-coded inside the code. NEVER put cookies, Authorization headers or other
tokens in the code: runtime.request adds the user's session (cookies and auth headers) to every call.

2. Then a ```python code block:
import runtime
from _site import BASE_HEADERS
def run(session, <params with defaults for optional ones>) -> dict:
    data = runtime.request(session, "<METHOD>", "<full url>", params=..., json=..., headers=BASE_HEADERS)
    return {{...clean, small, named fields listed in "returns"...}}
runtime.request(session, method, url, params=None, json=None, data=None, headers=None, expect_json=True)
sends the call with the user's cookies and raises runtime.OperationError on HTTP errors. Return a dict whose
top-level keys are exactly the "returns" names. For lists return {{"items": [...], ...}} with each item trimmed to
the useful fields. Use only the standard library besides runtime. No prints, no network calls other than
runtime.request."""

FIX_PROMPT = """Your operation `{name}` failed verification (attempt {attempt}).

Failure: {failure}
HTTP call made: {http}

Current spec:
{spec}

Current code:
```python
{code}
```

Fix it. Reply with the full corrected JSON spec, then the full corrected ```python code block, same format."""

LESSON_PROMPT = """An operation for {site} failed verification, then passed after a rewrite.
Failure before: {failure}
Final working code:
```python
{code}
```
State ONE general lesson (one sentence) that would help generate correct operations for OTHER websites.
Reply with only the sentence."""


def shape(value, depth=0):
    """Compact structural summary of a JSON value: keys, types and short example values."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:25]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"...{len(value)} items"] if value else []
    if isinstance(value, str):
        # Mark cut strings so the model never copies a truncated value as a real example.
        return value if len(value) <= 80 else value[:60] + "…(truncated)"
    return value


def body_view(text, limit=2500):
    if not text:
        return ""
    try:
        return json.dumps(shape(json.loads(text)))[:limit]
    except ValueError:
        return text[:limit]


def collect(job_id, site):
    domain = site_domain(site)
    steps = {}
    with db.conn() as c:
        for r in c.execute("SELECT step, label, url_before FROM steps WHERE job_id=?", (job_id,)):
            steps[r["step"]] = (r["label"], r["url_before"])
    groups = {}
    for r in db.requests_for(job_id):
        if not is_app_api(r["method"], r["url"], r["resource_type"], domain):
            continue
        groups.setdefault(endpoint_key(r["method"], r["url"], r["req_body"]), []).append(r)
    out = []
    for key, reqs in groups.items():
        ok = [r for r in reqs if r["status"] and 200 <= r["status"] < 300] or reqs
        samples, seen = [], set()
        for r in ok:
            sig = (r["url"], r["req_body"])
            if sig not in seen:
                seen.add(sig)
                samples.append(r)
            if len(samples) >= 3:
                break
        triggers = sorted({steps.get(r["step"], ("page load after login", ""))[0] or "page load" for r in reqs})
        out.append({"endpoint": key, "samples": samples, "triggers": triggers[:6],
                    "first_step": min(r["step"] or 0 for r in reqs),
                    "auth": is_auth_path(urlparse(reqs[0]["url"]).path)})
    return out


VOLATILE_HEADER = re.compile(r"token|turnstile|captcha|csrf|xsrf|nonce|signature|referr|web-url|previous-path|"
                             r"request-id|trace|timestamp", re.I)


def base_headers(job_id, site):
    """Headers the site's API gets with the SAME value on most requests (client type, version, origin).

    One-time values (anti-bot tokens, CSRF, page URLs) are excluded: hard-coding them would break
    once they expire and would leak them into published docs.
    """
    domain = site_domain(site)
    values, total = {}, 0
    for r in db.requests_for(job_id):
        if not is_app_api(r["method"], r["url"], r["resource_type"], domain):
            continue
        total += 1
        for k, v in json.loads(r["req_headers"] or "{}").items():
            if k.startswith(":") or k in SKIP_HEADERS or k.startswith("sec-") or VOLATILE_HEADER.search(k):
                continue
            if k.startswith("x-") or k in ("origin", "accept"):
                values.setdefault(k, Counter())[v] += 1
    out = {}
    for k, counter in values.items():
        value, n = counter.most_common(1)[0]
        if n >= 0.5 * total:
            out[k] = value
    return out


def session_for(job_id, site):
    """Cookies plus any bearer-token headers the site's own requests carried (refreshed live before use)."""
    from .auth_tokens import latest_recorded
    domain = site_domain(site)
    cookies = db.get_session(job_id) or []
    return {"cookies": {c["name"]: c["value"] for c in cookies if c["domain"].lstrip(".").endswith(domain)},
            "headers": latest_recorded(job_id, domain)}


def site_module(headers):
    return "BASE_HEADERS = " + json.dumps(headers, indent=4) + "\n"


def parse_generation(text):
    m = re.search(r"```python\s*\n(.*?)```", text, re.S)
    if not m:
        raise ValueError("no python code block")
    code = m.group(1).strip() + "\n"
    spec = llm.parse_json(text[:m.start()])
    return spec, code


def module_name(name):
    return re.sub(r"\W", "_", name or "op").lower()


async def run_calls(files, session, calls):
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"http://{WORKERS[0]}/run", headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                              json={"files": files, "input": {"session": session, "calls": calls}})
        r.raise_for_status()
        return r.json()


def check(result, spec):
    """Deterministic verification of one call result. Returns None when it passes, else a failure message."""
    if not result.get("ok"):
        return f"{result.get('error_code')}: {result.get('error', '')[:1500]}"
    out = result.get("output")
    if result.get("truncated"):  # large output: the runner reports its type and keys instead
        if result.get("output_type") != "dict":
            return f"run() must return a dict, got {result.get('output_type')}"
        keys = result.get("output_keys") or []
    elif not isinstance(out, dict):
        return f"run() must return a dict, got {type(out).__name__}"
    else:
        keys = list(out)
    missing = [f["name"] for f in spec.get("returns", []) if f["name"] not in keys]
    if missing:
        return f"returned keys {sorted(keys)[:20]} are missing declared fields {missing}"
    if isinstance(out, dict) and out and all(v in (None, "", [], {}) for v in out.values()) \
            and has_content(result.get("http", {}).get("body", "")):
        return "all returned fields are empty although the API response contains data; the field mapping is wrong"
    return None


def has_content(body):
    """True if a JSON response holds a non-empty list somewhere near the top (i.e. real data to map)."""
    try:
        data = json.loads(body)
    except ValueError:
        return False
    stack = [(data, 0)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, list) and v:
            return True
        if isinstance(v, dict) and depth < 2:
            stack.extend((x, depth + 1) for x in v.values())
    return False


def example_params(spec):
    """Verify with required params only; optional ones (cursors, filters) use the operation's defaults."""
    return {p["name"]: p["example"] for p in spec.get("params", [])
            if p.get("required") and p.get("example") is not None}


class Generator:
    def __init__(self, job_id, site):
        self.job_id, self.site = job_id, site
        self.llm_sem, self.run_sem = asyncio.Semaphore(LLM_CONCURRENCY), asyncio.Semaphore(RUN_CONCURRENCY)
        self.headers = base_headers(job_id, site)
        self.session = session_for(job_id, site)
        self.files_common = {"_site.py": site_module(self.headers)}
        self.lessons = ""
        self.first_step = {}
        self.token_lock, self.token_at = asyncio.Lock(), 0.0

    def event(self, kind, data):
        db.add_event(self.job_id, kind, data)

    async def ask(self, prompt):
        async with self.llm_sem:
            text, _ = await llm.chat(CODE_MODEL, [{"role": "user", "content": prompt}], max_tokens=12000)
        return parse_generation(text)

    async def verify(self, op, calls_extra=None):
        mod = module_name(op["spec"]["name"])
        files = {**self.files_common, f"{mod}.py": op["code"]}
        calls = [{"op": mod, "params": example_params(op["spec"])}]
        for extra in calls_extra or []:
            files[f"{module_name(extra['spec']['name'])}.py"] = extra["code"]
            calls.append({"op": module_name(extra["spec"]["name"]), "params": example_params(extra["spec"])})
        async with self.run_sem:
            res = await run_calls(files, self.session, calls)
        results = res.get("results") or [{"ok": False, "error_code": "runner", "error": res.get("error", "")}]
        if not results[0].get("ok") and looks_unauthenticated(results[0])                 and await self.refresh_token():
            async with self.run_sem:
                res = await run_calls(files, self.session, calls)
            results = res.get("results") or results
        return results

    async def refresh_token(self):
        """Sessions go stale mid-run (Firebase tokens ~1h, Clerk cookies ~1 min); re-read cookies and auth
        headers from the job's logged-in sandbox."""
        from .auth_tokens import capture_session, site_root
        async with self.token_lock:
            if time.monotonic() - self.token_at < 20:
                return True  # another operation just refreshed it
            job = db.get_job(self.job_id)
            if not job or not job.get("cdp"):
                return False
            try:
                cookies, headers = await capture_session(job["cdp"], site_root(self.site))
            except Exception:
                return False
            domain = site_domain(self.site)
            if cookies:
                self.session["cookies"] = {c["name"]: c["value"] for c in cookies
                                           if c["domain"].lstrip(".").endswith(domain)}
            if headers:
                self.session["headers"] = headers
            self.token_at = time.monotonic()
            return bool(cookies or headers)

    async def build(self, group):
        ep = group["endpoint"]
        if group["auth"]:
            db.upsert_operation(self.job_id, ep, status="excluded",
                                spec={"include": False, "reason": "authentication flow; humans log in, agents don't"})
            return
        samples = "\n\n".join(
            f"--- sample {i + 1}\n{r['method']} {r['url']}\nrequest body: {r['req_body'][:1500] or '(none)'}\n"
            f"response {r['status']}: {body_view(r['resp_body'])}"
            for i, r in enumerate(group["samples"]))
        prompt = GEN_PROMPT.format(site=self.site, endpoint=ep, triggers="; ".join(group["triggers"]),
                                   samples=samples, lessons=self.lessons or "(none yet)")
        db.upsert_operation(self.job_id, ep, status="generating")
        spec = code = error = None
        for attempt in range(2):  # one retry when the reply has no code block or the reasoning ran out
            try:
                spec, code = await self.ask(prompt)
                break
            except Exception as e:
                error = e
        if spec is None:
            db.upsert_operation(self.job_id, ep, status="failed", last_result={"error": f"generation: {error}"})
            return
        name = module_name(spec.get("name"))
        if not spec.get("include", True):
            db.upsert_operation(self.job_id, ep, name=name, status="excluded", spec=spec, code=code)
            self.event("operation", {"endpoint": ep, "name": name, "status": "excluded", "reason": spec.get("reason")})
            return
        db.upsert_operation(self.job_id, ep, name=name, status="generated", spec=spec, code=code, attempts=0)
        self.event("operation", {"endpoint": ep, "name": name, "status": "generated",
                                 "side_effect": spec.get("side_effect")})

    async def verify_loop(self, op, pair=None):
        """Verify, and rewrite from the failure details until it passes or attempts run out."""
        ep, history, first_failure = op["endpoint"], [], None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            db.upsert_operation(self.job_id, ep, status="verifying", attempts=attempt)
            results = await self.verify(op, [pair] if pair else None)
            failure = check(results[0], op["spec"])
            if pair and results[0].get("ok") and (len(results) < 2 or not results[1].get("ok")):
                self.event("unreverted_write", {"write": op["spec"]["name"], "undo_failed": pair["spec"]["name"],
                                                "error": (results[1].get("error", "") if len(results) > 1 else "")[:300]})
                db.upsert_operation(self.job_id, ep, status="failed", last_result=results, history=history)
                return False  # never retry a write whose undo is broken
            history.append({"attempt": attempt, "failure": failure, "http": results[0].get("http")})
            if not failure:
                db.upsert_operation(self.job_id, ep, status="verified", last_result=results, history=history,
                                    spec=op["spec"], code=op["code"])
                self.event("operation", {"endpoint": ep, "name": op["spec"]["name"], "status": "verified",
                                         "attempts": attempt})
                if first_failure:
                    await self.learn(first_failure, op["code"])
                return True
            first_failure = first_failure or failure
            self.event("operation", {"endpoint": ep, "name": op["spec"]["name"], "status": "rewriting",
                                     "attempt": attempt, "failure": failure[:300]})
            if attempt == MAX_ATTEMPTS:
                break
            http = {k: v for k, v in (results[0].get("http") or {}).items() if k != "body"}
            http["body_start"] = (results[0].get("http") or {}).get("body", "")[:1200]
            try:
                spec, code = await self.ask(FIX_PROMPT.format(
                    name=op["spec"]["name"], attempt=attempt, failure=failure, http=json.dumps(http),
                    spec=json.dumps(op["spec"], indent=1), code=op["code"]))
                op["spec"], op["code"] = spec, code
            except Exception as e:
                history.append({"attempt": attempt, "failure": f"rewrite failed: {e}"})
        db.upsert_operation(self.job_id, ep, status="failed", last_result=results, history=history,
                            spec=op["spec"], code=op["code"])
        self.event("operation", {"endpoint": ep, "name": op["spec"]["name"], "status": "failed"})
        return False

    async def learn(self, failure, code):
        try:
            text, _ = await llm.chat(CODE_MODEL, [{"role": "user", "content": LESSON_PROMPT.format(
                site=self.site, failure=failure[:800], code=code[:3000])}], max_tokens=4000)
            lesson = text.strip().splitlines()[-1]
            # Proposed, not approved: a curator approves it before the generator starts using it.
            await asyncio.to_thread(lessons.propose, site_domain(self.site), lesson, failure, code)
            self.event("lesson", {"lesson": lesson[:300], "status": "proposed"})
        except Exception as e:
            self.event("error", {"lesson": str(e)[:200]})

    async def retry_failed(self):
        """Re-run the verify/rewrite loop for read operations that failed, without regenerating the rest."""
        self.lessons = await asyncio.to_thread(lessons.load)
        failed = [o for o in db.operations(self.job_id)
                  if o["status"] in ("failed", "verifying", "generated")  # also ones a restart left mid-flight
                  and o["spec"] and o["spec"].get("side_effect") == "read"]
        self.event("status", {"status": "generating", "detail": f"retrying {len(failed)} failed operations"})
        await asyncio.gather(*(self.verify_loop(o) for o in failed))
        counts = Counter(o["status"] for o in db.operations(self.job_id))
        self.event("status", {"status": "generated", "detail": dict(counts)})
        return counts

    async def run(self, fresh=False):
        """fresh=True discards earlier operations (e.g. after endpoint grouping changed) and regenerates from the
        traffic recorded during exploration; no new exploration or login is needed."""
        if fresh:
            db.clear_operations(self.job_id)
        await self.refresh_token()  # start verification with the live browser's current session
        self.lessons = await asyncio.to_thread(lessons.load)
        groups = collect(self.job_id, self.site)
        self.first_step = {g["endpoint"]: g["first_step"] for g in groups}
        self.event("status", {"status": "generating", "detail": f"{len(groups)} endpoints"})
        await asyncio.gather(*(self.build(g) for g in groups))

        ops = [o for o in db.operations(self.job_id) if o["status"] == "generated"]
        by_name = {o["spec"]["name"]: o for o in ops}
        reads = [o for o in ops if o["spec"].get("side_effect") == "read"]
        writes = [o for o in ops if o["spec"].get("side_effect") == "reversible_write"]
        irreversible = [o for o in ops if o["spec"].get("side_effect") not in ("read", "reversible_write")]

        for o in irreversible:
            db.upsert_operation(self.job_id, o["endpoint"], status="unverified_irreversible")
        await asyncio.gather(*(self.verify_loop(o) for o in reads))

        # A reversible write is only run together with its undo, so the account ends where it started.
        # The action captured first during exploration is the "do"; the later one is its "undo"
        # (running them the other way round would leave the account changed).
        done = set()
        for o in sorted(writes, key=lambda o: self.first_step.get(o["endpoint"], 0)):
            name = o["spec"]["name"]
            if name in done:
                continue
            pair = by_name.get(o["spec"].get("undo") or "")
            # "Undo = itself" (e.g. update_profile) is not a real undo: we can't know the prior value.
            if not pair or pair["spec"]["name"] in done or pair["spec"]["name"] == name:
                db.upsert_operation(self.job_id, o["endpoint"], status="unverified_no_undo")
                continue
            done |= {name, pair["spec"]["name"]}
            ok = await self.verify_loop(o, pair)
            db.upsert_operation(self.job_id, pair["endpoint"], status="verified" if ok else "failed",
                                spec=pair["spec"], code=pair["code"])
        counts = Counter(o["status"] for o in db.operations(self.job_id))
        self.event("status", {"status": "generated", "detail": dict(counts)})
        return counts
