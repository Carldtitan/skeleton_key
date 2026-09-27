"""Vultr Serverless Inference client (OpenAI-compatible). Every agent LLM call goes through here.

Usage is metered per (job, phase) via a context variable, priced from Vultr's live model list.
"""
import asyncio
import json
import re
import time
from contextvars import ContextVar

import httpx

from . import db
from .config import INFERENCE_KEY, INFERENCE_URL

# (job_id, phase) for the code currently running; set by jobs/race so every call gets attributed.
meter: ContextVar = ContextVar("llm_meter", default=None)
_prices: dict = {}


async def prices():
    """USD per token for each model, from the public model list (cached)."""
    if not _prices:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                models = (await client.get(f"{INFERENCE_URL}/models")).json()["data"]
            for m in models:
                p = {x["type"]: float(x["cost_usd"]) for io in m.get("input_modalities", []) + m.get("output_modalities", [])
                     for x in io.get("pricing", [])}
                _prices[m["id"]] = (p.get("prompt", 0.0), p.get("completion", 0.0))
        except Exception:
            pass
    return _prices


async def cost_of(model, usage):
    p_in, p_out = (await prices()).get(model, (0.0, 0.0))
    return usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out


async def _record(model, usage, seconds):
    tag = meter.get()
    if not tag:
        return
    job_id, phase = tag
    db.add_usage(job_id, phase, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                 await cost_of(model, usage), seconds)


async def chat(model, messages, max_tokens=4000, temperature=0.2, retries=3):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    last = None
    for attempt in range(retries):
        try:
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.post(f"{INFERENCE_URL}/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {INFERENCE_KEY}"})
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(r.text[:200], request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            await _record(model, data.get("usage", {}), time.monotonic() - started)
            content = data["choices"][0]["message"].get("content") or ""
            if content.strip():
                return content, data.get("usage", {})
            last = RuntimeError("empty completion (reasoning used the whole budget?)")
            body["max_tokens"] = min(body["max_tokens"] * 2, 16000)
        except httpx.HTTPError as e:
            last = e
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"inference failed: {last}")


def _to_anthropic(messages):
    """Convert our OpenAI-style messages (system + user with image_url data URLs) to Messages API shape."""
    system, out = "", []
    for m in messages:
        if m["role"] == "system":
            system += m["content"]
            continue
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = []
        for part in content:
            if part["type"] == "text":
                blocks.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image_url":
                header, data = part["image_url"]["url"].split(",", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "data": data,
                                                           "media_type": header.split(":")[1].split(";")[0]}})
        out.append({"role": m["role"], "content": blocks})
    return system, out


def _anthropic_usage(model, u):
    """Tokens and cost including prompt-cache writes (1.25x input price) and reads (0.1x)."""
    from .config import ANTHROPIC_PRICES
    p_in, p_out = ANTHROPIC_PRICES.get(model, (0.0, 0.0))
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    prompt = u.input_tokens + written + read
    cost = u.input_tokens * p_in + written * p_in * 1.25 + read * p_in * 0.1 + u.output_tokens * p_out
    return prompt, u.output_tokens, cost, read


async def chat_anthropic(model, messages, max_tokens=16000):
    """Frontier baseline for the race only. Returns (text, usage in OpenAI field names)."""
    import anthropic

    from .config import ANTHROPIC_API_KEY
    system, msgs = _to_anthropic(messages)
    started = time.monotonic()
    async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
        response = await client.beta.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=msgs,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},  # auto-cache the longest stable prefix
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    if response.stop_reason == "refusal":
        raise RuntimeError("frontier model declined the request")
    text = "".join(b.text for b in response.content if b.type == "text")
    prompt, completion, cost, _ = _anthropic_usage(model, response.usage)
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    tag = meter.get()
    if tag:
        db.add_usage(tag[0], tag[1], model, prompt, completion, cost, time.monotonic() - started)
    return text, usage


async def chat_anthropic_tools(model, system, messages, tools, max_tokens=16000):
    """One tool-use turn on the frontier baseline. Returns the SDK response; callers append
    response.content unchanged so thinking blocks are passed back as the API expects."""
    import anthropic

    from .config import ANTHROPIC_API_KEY
    # Breakpoint on the last tool: tools + system are identical across tasks, so they are cached across runs;
    # the top-level cache_control additionally caches the growing conversation turn to turn.
    tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}] if tools else tools
    started = time.monotonic()
    async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
        response = await client.beta.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    prompt, completion, cost, cached = _anthropic_usage(model, response.usage)
    tag = meter.get()
    if tag:
        db.add_usage(tag[0], tag[1], model, prompt, completion, cost, time.monotonic() - started)
    return response


async def chat_tools(model, messages, tools, max_tokens=4000):
    """One tool-calling turn on Vultr inference. Returns the assistant message dict (may contain tool_calls)."""
    body = {"model": model, "messages": messages, "tools": tools, "max_tokens": max_tokens, "temperature": 0.2}
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(3):  # same rule as chat(): retry transient 5xx / 429 / network errors
            try:
                r = await client.post(f"{INFERENCE_URL}/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {INFERENCE_KEY}"})
                if (r.status_code >= 500 or r.status_code == 429) and attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)
    r.raise_for_status()
    data = r.json()
    await _record(model, data.get("usage", {}), time.monotonic() - started)
    return data["choices"][0]["message"]


async def rerank(query, documents, top_n, model="bge-reranker-v2-m3"):
    """Vultr's reranker: indices of the top_n documents most relevant to the query."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{INFERENCE_URL}/rerank", headers={"Authorization": f"Bearer {INFERENCE_KEY}"},
                              json={"model": model, "query": query, "documents": documents, "top_n": top_n})
    r.raise_for_status()
    return [x["index"] for x in r.json()["results"]]


def parse_json(text):
    """Extract the first JSON object from a model reply (tolerates code fences and prose)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {text[:200]}")
    return json.loads(m.group(0), strict=False)  # models often put raw newlines inside strings


async def chat_json(model, messages, **kw):
    content, usage = await chat(model, messages, **kw)
    return parse_json(content), usage
