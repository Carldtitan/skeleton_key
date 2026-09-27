"""Where each ID parameter comes from.

An agent asked about "@anthropicai" can't call get_user_follow_counts(user_id) until it learns the user's
internal id from get_user_profile(handle). Without being told, agents guess (pass the handle as the id) and
some sites answer a wrong id with zeros instead of an error. So at publish time every required ID-like
parameter is linked to the read operation that returns it; the link goes into the parameter's description
and tool selection hands the agent the source operation too.
"""
import json
import re

from . import llm
from .config import CODE_MODEL

ID_PARAM = re.compile(r"(^|_)(id|ids|slug|handle|username|uuid)$|[a-z](Id|Ids)$", re.I)

SOURCES_PROMPT = """These are the operations of an API generated for {site}. Agents call them as tools.

{ops}

For each parameter listed under "needs", say which READ operation an agent should call first to obtain that
value, and which field of its result holds it. Only link a parameter when an agent would normally NOT already
know the value (internal ids, UUIDs, api ids). Leave it out when the user would naturally supply it (a handle,
username or slug they type or see in a URL) or when no listed operation returns it.

Reply ONLY with JSON: {{"<operation>.<parameter>": {{"from": "<read operation>", "field": "<result field>"}}, ...}}"""


def _needs(op):
    return [p for p in op["spec"].get("params", []) if p.get("required") and ID_PARAM.search(p["name"])]


async def find_sources(site, ops):
    """{op name: {param: {"from": op, "field": f}}} for required ID params, checked against the real ops."""
    reads = {o["name"] for o in ops if o["spec"].get("side_effect") == "read"}
    lines = []
    for o in ops:
        s = o["spec"]
        returns = ", ".join(r["name"] for r in s.get("returns", []) if isinstance(r, dict))
        needs = ", ".join(f"{p['name']} ({p.get('description', '')[:80]})" for p in _needs(o))
        lines.append(f"- {o['name']} [{s.get('side_effect', '?')}]: {s.get('summary', '')}\n"
                     f"  returns: {returns or '-'}" + (f"\n  needs: {needs}" if needs else ""))
    if not any(_needs(o) for o in ops):
        return {}
    text, _ = await llm.chat(CODE_MODEL, [{"role": "user", "content": SOURCES_PROMPT.format(
        site=site, ops="\n".join(lines))}], max_tokens=16000)
    raw = llm.parse_json(text) or {}
    names = {o["name"]: o for o in ops}
    found = {}
    for key, v in raw.items():
        op, _, param = str(key).partition(".")
        if not isinstance(v, dict) or op not in names or v.get("from") not in reads or v["from"] == op:
            continue
        if param in {p["name"] for p in _needs(names[op])}:
            found.setdefault(op, {})[param] = {"from": v["from"], "field": str(v.get("field") or param)}
    return found


def apply_sources(ops, found):
    """Write the links into each op's spec: param descriptions (what agents read) and spec['sources']."""
    for o in ops:
        links = found.get(o["name"])
        if not links:
            continue
        o["spec"]["sources"] = links
        for p in o["spec"].get("params", []):
            link = links.get(p["name"])
            if link:
                desc = re.sub(r"\s*Get it from `[^`]+`.*$", "", p.get("description", ""))
                p["description"] = f"{desc} Get it from `{link['from']}` (field `{link['field']}`)".strip()
    return ops


def providers(site_ops, names):
    """The source operations the given operations depend on (one level; sources are reads without ids
    more often than not)."""
    by_name = {o["name"]: o for o in site_ops}
    out = []
    for n in names:
        for link in (by_name.get(n, {}).get("spec", {}).get("sources") or {}).values():
            if link["from"] not in names and link["from"] not in out:
                out.append(link["from"])
    return out
