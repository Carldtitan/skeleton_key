"""Vultr Serverless Inference client (OpenAI-compatible). Every agent LLM call goes through here."""
import asyncio
import json
import re

import httpx

from .config import INFERENCE_KEY, INFERENCE_URL


async def chat(model, messages, max_tokens=4000, temperature=0.2, retries=3):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    last = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.post(f"{INFERENCE_URL}/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {INFERENCE_KEY}"})
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(r.text[:200], request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"].get("content") or ""
            if content.strip():
                return content, data.get("usage", {})
            last = RuntimeError("empty completion (reasoning used the whole budget?)")
            body["max_tokens"] = min(body["max_tokens"] * 2, 16000)
        except httpx.HTTPError as e:
            last = e
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"inference failed: {last}")


def parse_json(text):
    """Extract the first JSON object from a model reply (tolerates code fences and prose)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {text[:200]}")
    return json.loads(m.group(0), strict=False)  # models often put raw newlines inside strings


async def chat_json(model, messages, **kw):
    content, usage = await chat(model, messages, **kw)
    return parse_json(content), usage
