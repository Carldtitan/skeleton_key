"""Decide which captured requests are the app's own API, and what makes two requests the same endpoint.

One endpoint key = one future granular operation (e.g. GET api.luma.com/event/get -> get_event).
"""
import json
import re
from urllib.parse import urlparse

NOISE_PATH = re.compile(
    r"/cdn-cgi/|insights|analytics|telemetry|/ping\b|/track|/log(s|ging)?\b|metrics|beacon|/collect|"
    r"sentry|/rum\b|/vitals|heartbeat|/events?/batch|"
    r"/_next/static/|\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf)$", re.I)
# An id segment is numeric, or a long token containing a digit (uuid, evt-Ab12..., slug gtl3o3ug).
# Hyphenated words like "get-following-calendars" have no digit and stay literal.
ID_SEGMENT = re.compile(r"^(\d+|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})$")
FILE_EXT = re.compile(r"^(.*?)(\.[A-Za-z0-9]{1,5})$")
API_TYPES = {"xhr", "fetch"}
# Words that mark login/credential flows. Matched as whole words after splitting camelCase and
# separators, so "getLoginToken" and "/auth/email/start" match but "authors" does not.
AUTH_WORDS = {"auth", "login", "logout", "signin", "signins", "signout", "signup", "signups", "register", "passkey", "passkeys",
              "sudo", "2fa", "mfa", "otp", "sms", "password"}


def words(path):
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", path)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", parts) if w]


def is_auth_path(path):
    ws = words(path)
    joined = {a + b for a, b in zip(ws, ws[1:])}  # "sign in" -> "signin"
    return bool(AUTH_WORDS & (set(ws) | joined))


def site_domain(url):
    """Registrable domain, approximated as the last two labels (luma.com, partiful.com)."""
    host = urlparse(url if "//" in url else f"https://{url}").hostname or ""
    return ".".join(host.split(".")[-2:])


def is_app_api(method, url, resource_type, domain):
    u = urlparse(url)
    if not (u.hostname or "").endswith(domain):
        return False
    if NOISE_PATH.search(u.path):
        return False
    # Server-rendered apps: form posts arrive as document requests.
    return resource_type in API_TYPES or (resource_type == "document" and method != "GET")


def normalize_segment(seg):
    if ID_SEGMENT.match(seg):
        return "{id}"
    m = FILE_EXT.match(seg)  # Next.js data routes: /_next/data/<build>/e/<eventId>.json
    if m and ID_SEGMENT.match(m.group(1)):
        return "{id}" + m.group(2)
    return seg


def normalize_path(path):
    return "/".join(normalize_segment(seg) for seg in path.split("/"))


def endpoint_key(method, url, req_body=""):
    u = urlparse(url)
    key = f"{method} {u.hostname}{normalize_path(u.path)}"
    if "graphql" in u.path.lower() and req_body:
        try:
            op = json.loads(req_body).get("operationName")
            if op:
                key += f"#{op}"
        except (ValueError, AttributeError):
            pass
    return key
