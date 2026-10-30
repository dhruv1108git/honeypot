"""Thin per-turn model call: reuse hp.backends build_request + parse_openai over httpx.

Avoids building a full hp Record per turn (which needs heavy provenance meta). Returns a small dict
{reasoning, content, finish_reason, usage, ok, error} for one assistant turn.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, to import hp.*
from hp.backends import (ModelSpec, build_request, parse_openai, parse_anthropic,  # noqa: E402
                         check_usable, redact)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _scrub(s: str, secrets: list[str]) -> str:
    return redact(s, secrets) if isinstance(s, str) else s


async def call_once(spec: ModelSpec, messages: list[dict], params: dict[str, Any],
                    client: httpx.AsyncClient) -> dict:
    secrets = spec.secrets()
    try:
        url, headers, body = build_request(spec, messages, params)
    except Exception as e:
        return {"ok": False, "error": _scrub(f"build_request: {type(e).__name__}: {e}", secrets), "retryable": False}
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=spec.timeout_s)
    except httpx.HTTPError as e:
        return {"ok": False, "error": _scrub(f"{type(e).__name__}: {e}", secrets), "retryable": True}
    if resp.status_code != 200:
        return {"ok": False, "error": _scrub(f"HTTP {resp.status_code}: {resp.text[:500]}", secrets),
                "retryable": resp.status_code in RETRYABLE_STATUS}
    try:
        raw = resp.json()
        parsed = parse_anthropic(raw) if spec.backend == "anthropic" else parse_openai(raw)
        check_usable(parsed, spec)   # reuse hp's usability checks (finish_reason present, non-empty, etc.)
    except Exception as e:
        return {"ok": False, "error": _scrub(f"parse: {type(e).__name__}: {e}", secrets), "retryable": True}
    return {"ok": True, "reasoning": parsed.get("reasoning"), "content": parsed.get("content"),
            "finish_reason": parsed.get("finish_reason"), "truncated": bool(parsed.get("truncated")),
            "usage": parsed.get("usage")}


async def call_with_retry(spec: ModelSpec, messages: list[dict], params: dict[str, Any],
                          client: httpx.AsyncClient, max_attempts: int = 3, backoff: float = 5.0) -> dict:
    last = {"ok": False, "error": "no attempt"}
    for a in range(max_attempts):
        last = await call_once(spec, messages, params, client)
        if last["ok"] or not last.get("retryable"):
            return last
        if a < max_attempts - 1:   # don't sleep after the final attempt
            await asyncio.sleep(min(backoff * (2 ** a), 60.0))
    return last
