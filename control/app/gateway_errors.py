"""Recognising auth failures in an operation's result (shared by the gateway and the generator's verifier)."""
import re

AUTH_FAILURE = re.compile(r"unauthenticated|unauthori[sz]ed|invalid[ _-]?token|expired[ _-]?token|jwt|id token", re.I)


def looks_unauthenticated(result):
    """True when a call failed because its session/token was rejected (not because the request was wrong)."""
    if result.get("error_code") == "session_expired":
        return True
    body = (result.get("http") or {}).get("body", "") + (result.get("error") or "")
    return result.get("error_code") == "bad_request" and bool(AUTH_FAILURE.search(body))
