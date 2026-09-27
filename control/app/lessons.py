"""The generator's skills: lessons learned from mistakes, stored in Vultr Object Storage.

Curated first: every lesson has a status and a source. Lessons the generator learns on its own arrive as
"proposed" (source "auto"); only "approved" lessons are fed into generation prompts. Curated lessons come
from mistakes found and fixed while building and running Skeleton Key.
"""
import json
import os
import secrets
import time

import boto3

BUCKET, KEY, LEGACY_KEY = "sk-skills", "lessons.json", "lessons.md"


def _s3():
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
                        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                        aws_secret_access_key=os.environ["S3_SECRET_KEY"])


def load_all():
    try:
        return json.loads(_s3().get_object(Bucket=BUCKET, Key=KEY)["Body"].read())
    except Exception:
        return []


def save_all(items):
    _s3().put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(items, indent=1).encode(), ContentType="application/json")


def load():
    """Approved lessons as prompt text for the generator."""
    approved = [l for l in load_all() if l.get("status") == "approved" and l.get("scope", "codegen") == "codegen"]
    return "\n".join(f"- {l['lesson']}" for l in approved)


def propose(site, lesson, failure="", fix="", source="auto", status="proposed", scope="codegen", date=None):
    lesson = " ".join((lesson or "").split())[:400]
    if not lesson:
        return None
    items = load_all()
    if any(l["lesson"].lower() == lesson.lower() for l in items):
        return None
    item = {"id": "lsn_" + secrets.token_hex(4), "site": site, "date": date or time.strftime("%Y-%m-%d"),
            "lesson": lesson, "failure": failure[:1200], "fix": fix[:1500], "status": status, "source": source,
            "scope": scope}
    save_all(items + [item])
    return item


def set_status(lesson_id, status):
    items = load_all()
    for l in items:
        if l["id"] == lesson_id:
            l["status"] = status
    save_all(items)
