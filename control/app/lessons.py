"""The generator's skills file: lessons learned from its own mistakes, stored in Vultr Object Storage.

Read before generating operations for any site; a lesson is appended whenever an operation
only passed verification after a rewrite.
"""
import os
import time

import boto3

BUCKET, KEY = "sk-skills", "lessons.md"


def _s3():
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
                        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
                        aws_secret_access_key=os.environ["S3_SECRET_KEY"])


def load():
    try:
        return _s3().get_object(Bucket=BUCKET, Key=KEY)["Body"].read().decode()
    except Exception:
        return ""


def append(site, lesson):
    lesson = " ".join(lesson.split())[:400]
    if not lesson:
        return
    current = load() or "# Skeleton Key lessons\n"
    if lesson.lower() in current.lower():
        return
    line = f"- ({site}, {time.strftime('%Y-%m-%d')}) {lesson}\n"
    _s3().put_object(Bucket=BUCKET, Key=KEY, Body=(current.rstrip("\n") + "\n" + line).encode(),
                     ContentType="text/markdown")
