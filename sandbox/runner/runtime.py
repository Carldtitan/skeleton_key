"""Shared runtime for generated Skeleton Key operations.

Every operation calls request(); it sends the call like Chrome would (TLS fingerprint included),
attaches the user's session, and turns HTTP failures into typed errors an agent can act on.
"""
from curl_cffi import requests as _requests

# Details of the most recent HTTP call, used by the verifier to explain failures.
LAST = {}


class OperationError(Exception):
    """code is one of: session_expired, blocked, not_found, rate_limited, bad_request, upstream_error."""

    def __init__(self, code, message, status=None):
        super().__init__(f"{code}: {message}")
        self.code, self.status = code, status


def request(session, method, url, *, params=None, json=None, data=None, headers=None, expect_json=True):
    h = dict(session.get("headers") or {})
    h.update(headers or {})
    resp = _requests.request(method, url, params=params, json=json, data=data, headers=h,
                             cookies=session.get("cookies") or {}, impersonate="chrome", timeout=30)
    body = resp.text
    LAST.clear()
    LAST.update(method=method, url=str(resp.url), status=resp.status_code, body=body[:2000])
    s = resp.status_code
    if s in (401, 403):
        if "cf-chl" in body or "Just a moment" in body or "cf_chl" in body:
            raise OperationError("blocked", "bot protection challenged the request", s)
        raise OperationError("session_expired", "the site rejected the session; a human must log in again", s)
    if s == 404:
        raise OperationError("not_found", "resource not found", s)
    if s == 429:
        raise OperationError("rate_limited", "too many requests", s)
    if 400 <= s < 500:
        raise OperationError("bad_request", body[:300], s)
    if s >= 500:
        raise OperationError("upstream_error", body[:300], s)
    if not expect_json:
        return body
    try:
        return resp.json()
    except ValueError:
        raise OperationError("upstream_error", "expected JSON but got something else", s)
