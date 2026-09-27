"""Seed the curated lessons: real mistakes found and fixed while building and running Skeleton Key on luma.com.

Run inside the control container: python scripts/seed_lessons.py (idempotent: duplicates are skipped).
"""
from app import lessons

D = "2026-09-26"
SEED = [
    dict(source="auto", status="approved", scope="codegen",
         lesson="Operations must rely solely on the sandbox-provided modules (runtime and _site); any other import "
                "fails at load time before the code even runs.",
         failure="(learned automatically before failures were stored with lessons)",
         fix="import runtime\nfrom _site import BASE_HEADERS\n\ndef run(session, ...):\n"
             "    data = runtime.request(session, \"GET\", url, headers=BASE_HEADERS)"),
    dict(source="curated", status="approved", scope="codegen",
         lesson="Never hard-code one-time headers such as anti-bot tokens, CSRF values or page URLs; only constant "
                "client headers belong in every request.",
         failure="The captured requests carried x-luma-turnstile-token, a single-use anti-bot token. Hard-coding it "
                 "would break every call once it expired and would leak it into the published docs.",
         fix="BASE_HEADERS keeps only headers with the same value on most requests (client type, client version, "
             "origin); tokens, CSRF values and page URLs are dropped."),
    dict(source="curated", status="approved", scope="codegen",
         lesson="Optional parameters such as cursors and filters must default to None and be left out of the "
                "request when unset; never reuse a captured value as a default.",
         failure="list_discover_categories failed 4 times with bad_request: its example pagination_cursor had been "
                 "copied from a captured sample that was cut off.",
         fix="def run(session, pagination_limit=20, pagination_cursor=None):\n"
             "    params = {\"pagination_limit\": pagination_limit}\n"
             "    if pagination_cursor:\n        params[\"pagination_cursor\"] = pagination_cursor"),
    dict(source="curated", status="approved", scope="codegen",
         lesson="Decode response bodies as UTF-8 regardless of the declared charset; a guessed encoding garbles "
                "text such as curly quotes.",
         failure="Tool output showed \"momsâ€™ walk\" instead of \"moms’ walk\".",
         fix="body = resp.content.decode(\"utf-8\", errors=\"replace\")\ndata = json.loads(body)"),
    dict(source="curated", status="approved", scope="codegen",
         lesson="A write whose undo is itself (like updating a profile) has no real undo: document it, but never "
                "execute it during verification.",
         failure="update_my_profile named itself as its own undo and was executed twice while being verified.",
         fix="Writes are only run as do/undo pairs of two different operations, do first; self-undo writes are "
             "marked unverified_no_undo."),
    dict(source="curated", status="approved", scope="explore",
         lesson="Menus are often built from plain divs, not buttons or links; treat any pointer-cursor element as "
                "clickable or undo options stay invisible.",
         failure="The explorer subscribed to a category but could never find 'Unsubscribe': it sat in a div-based "
                 "menu that wasn't marked as clickable.",
         fix="Mark the outermost element of every cursor:pointer region, not only a, button and role=button."),
    dict(source="curated", status="approved", scope="explore",
         lesson="A URL path segment is an id only if it is numeric or a long token containing a digit; hyphenated "
                "words are endpoint names.",
         failure="get-following-calendars and get-payment-method-types were read as ids and merged into /home/{id} "
                 "and /payments/{id}.",
         fix=r"ID = ^(\d+|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})$"),
]

# Mistakes from the first partiful.com run (2026-09-27).
PARTIFUL = [
    dict(source="curated", status="approved", scope="codegen",
         lesson="Never put Authorization headers, cookies or tokens in generated code; the runtime adds the user's "
                "session (cookies and bearer tokens) to every call.",
         failure="The generation prompt told the model to hard-code auth tokens. Partiful's calls carry a Firebase "
                 "token that expires in about an hour, so hard-coded code would break and leak it.",
         fix="Prompt: \"NEVER put cookies, Authorization headers or other tokens in the code: runtime.request adds "
             "the user's session to every call.\""),
    dict(source="curated", status="approved", scope="session",
         lesson="Many apps authenticate API calls with a bearer token kept in browser storage (Firebase, Supabase), "
                "not a cookie; capture it with the session and re-mint it from saved browser storage when it expires.",
         failure="All 25 api.partiful.com operations failed verification: the calls reached Partiful without the "
                 "Authorization: Bearer token its web app sends, so no rewrite could fix them.",
         fix="Sessions = cookies + auth headers + browser storage (IndexedDB). On an auth failure the gateway opens the "
             "site in a sandbox with that storage, reads the fresh token off the site's own request, and retries once."),
    dict(source="curated", status="approved", scope="explore",
         lesson="An id followed by a file extension is still an id (Next.js /_next/data/<build>/e/<id>.json), and "
                "/_next/static or asset files are never API calls.",
         failure="Every Partiful event page became its own endpoint, producing 10 duplicate get_event operations; "
                 "CSS files were also treated as API calls.",
         fix="normalize_segment: strip the extension before the id test; NOISE_PATH skips /_next/static and "
             ".css/.js/.png/... files."),
    dict(source="curated", status="approved", scope="explore",
         lesson="Login endpoints often use camelCase RPC names (getLoginToken, verifyOtp); match auth words after "
                "splitting camelCase, not only as whole path segments.",
         failure="Partiful's SMS login call getLoginToken wasn't recognised as a login flow and was generated and "
                 "verified as an operation.",
         fix="is_auth_path splits camelCase and separators into words and checks auth, login, signup, otp, sms, "
             "passkey, password, ..."),
    dict(source="curated", status="approved", scope="generator",
         lesson="When the model's reply has no code block or its reasoning uses up the output budget, retry once "
                "instead of failing the endpoint.",
         failure="9 Partiful endpoints failed at generation with \"no python code block\" or an empty completion.",
         fix="build(): one retry on an unparseable generation; the LLM client doubles max_tokens after an empty reply."),
]

if __name__ == "__main__":
    for site, date, items in (("luma.com", D, SEED), ("partiful.com", "2026-09-27", PARTIFUL)):
        for s in items:
            item = lessons.propose(site, s["lesson"], s["failure"], s["fix"], source=s["source"],
                                   status=s["status"], scope=s["scope"], date=date)
            print("added" if item else "exists", "-", s["lesson"][:70])
