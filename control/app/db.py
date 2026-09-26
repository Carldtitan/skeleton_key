"""SQLite store for jobs, their event log, exploration steps and captured traffic."""
import json
import sqlite3
import time

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    site_url TEXT NOT NULL,
    hints TEXT,
    status TEXT NOT NULL,
    status_detail TEXT,
    worker TEXT,
    sandbox_id TEXT,
    cdp TEXT,
    vnc TEXT,
    view_token TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    job_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    action TEXT,
    label TEXT,
    is_write INTEGER,
    url_before TEXT,
    url_after TEXT,
    detail TEXT,
    PRIMARY KEY (job_id, step)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    step INTEGER,
    ts REAL,
    method TEXT,
    url TEXT,
    resource_type TEXT,
    req_headers TEXT,
    req_body TEXT,
    status INTEGER,
    resp_headers TEXT,
    resp_body TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    job_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    name TEXT,
    status TEXT,
    spec TEXT,
    code TEXT,
    attempts INTEGER DEFAULT 0,
    last_result TEXT,
    history TEXT,
    PRIMARY KEY (job_id, endpoint)
);
CREATE TABLE IF NOT EXISTS sessions (
    job_id TEXT PRIMARY KEY,
    cookies TEXT,
    captured REAL
);
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def create_job(job_id, site_url, hints, view_token):
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO jobs (id, site_url, hints, status, view_token, created, updated) VALUES (?,?,?,?,?,?,?)",
            (job_id, site_url, hints, "starting", view_token, now, now),
        )


def update_job(job_id, **fields):
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id):
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC")]


def add_event(job_id, kind, data=None):
    with conn() as c:
        c.execute("INSERT INTO events (job_id, ts, kind, data) VALUES (?,?,?,?)",
                  (job_id, time.time(), kind, json.dumps(data) if data is not None else None))


def events(job_id, after_id=0):
    with conn() as c:
        rows = c.execute("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id", (job_id, after_id)).fetchall()
    return [dict(r) | {"data": json.loads(r["data"]) if r["data"] else None} for r in rows]


def save_step(job_id, step, **fields):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO steps (job_id, step, action, label, is_write, url_before, url_after, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, step, fields.get("action"), fields.get("label"), int(bool(fields.get("is_write"))),
             fields.get("url_before"), fields.get("url_after"), json.dumps(fields.get("detail"))),
        )


def add_request(job_id, step, **r):
    with conn() as c:
        c.execute(
            "INSERT INTO requests (job_id, step, ts, method, url, resource_type, req_headers, req_body, status, "
            "resp_headers, resp_body) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, step, time.time(), r["method"], r["url"], r["resource_type"], json.dumps(r["req_headers"]),
             r["req_body"], r["status"], json.dumps(r["resp_headers"]), r["resp_body"]),
        )


def requests_for(job_id, step=None):
    q, args = "SELECT * FROM requests WHERE job_id=?", [job_id]
    if step is not None:
        q, args = q + " AND step=?", args + [step]
    with conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args)]


def request_count(job_id):
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM requests WHERE job_id=?", (job_id,)).fetchone()[0]


def upsert_operation(job_id, endpoint, **fields):
    for k in ("spec", "last_result", "history"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], default=str)
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO operations (job_id, endpoint) VALUES (?, ?)", (job_id, endpoint))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE operations SET {cols} WHERE job_id=? AND endpoint=?", (*fields.values(), job_id, endpoint))


def operations(job_id):
    with conn() as c:
        rows = c.execute("SELECT * FROM operations WHERE job_id=? ORDER BY name", (job_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("spec", "last_result", "history"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out


def get_session(job_id):
    with conn() as c:
        row = c.execute("SELECT cookies FROM sessions WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(row["cookies"]) if row else None


def save_session(job_id, cookies):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions (job_id, cookies, captured) VALUES (?,?,?)",
                  (job_id, json.dumps(cookies), time.time()))
