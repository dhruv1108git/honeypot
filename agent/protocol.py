"""Parse the model's per-turn action from its message content.

Contract: the model ends each message with one JSON object naming the next action, normally inside a
fenced ```json block. We parse the LAST such fenced block (strict). Some models (e.g. gpt-oss) emit the
bare JSON object without the fence; when there is no ```json fence we accept a bare `{...}` object
carrying a string "action". A ```diff block (with no ```json fence) is an implicit submit, and that
takes precedence over any bare object so braces inside the diff cannot hijack the action. Otherwise the
action is `none` (caller issues a reminder, then ends after a few tries).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_JSON_FENCE = re.compile(r"```json[ \t]*\n(.*?)```", re.DOTALL)
_FENCE_ANY = re.compile(r"```.*?```", re.DOTALL)      # any fenced code region (stripped before a bare scan)
_DIFF_FENCE = re.compile(r"```diff[ \t]*\n", re.IGNORECASE)
_MAX_SCAN = 60000   # scan only the tail of very long content (the action is at the end); bounds regex work

VALID = {"list_dir", "read_file", "grep", "submit"}


@dataclass
class Action:
    tool: str                 # list_dir | read_file | grep | submit | none
    args: dict
    raw: str | None = None
    error: str | None = None


def has_diff_block(content: str) -> bool:
    return bool(_DIFF_FENCE.search(content or ""))


def _last_fenced_json(content: str) -> str | None:
    last = None
    for m in _JSON_FENCE.finditer(content):
        last = m
    return last.group(1).strip() if last else None


def _bare_action_objects(content: str) -> list[dict]:
    """Every top-level JSON object in content, with fenced code regions removed first so diff/code braces
    cannot contribute. Uses raw_decode (handles nesting and braces inside string values correctly)."""
    s = _FENCE_ANY.sub(" ", content)
    dec = json.JSONDecoder()
    out: list[dict] = []
    i, n = 0, len(s)
    while i < n:
        k = s.find("{", i)
        if k < 0:
            break
        try:
            obj, end = dec.raw_decode(s, k)
            i = end
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            i = k + 1
    return out


def _from_obj(obj: dict, raw: str | None, content: str) -> Action:
    action = obj["action"].strip()
    if action in VALID:
        return Action(action, {k: v for k, v in obj.items() if k != "action"}, raw=raw)
    if has_diff_block(content):
        return Action("submit", {}, raw=raw, error=f"unknown action {action!r}; diff present -> submit")
    return Action("none", {"action": action}, raw=raw, error=f"unknown action {action!r}")


def parse_action(content: str) -> Action:
    if not content or not content.strip():
        return Action("none", {}, error="empty content")
    if len(content) > _MAX_SCAN:
        content = content[-_MAX_SCAN:]

    # 1) fenced ```json -- the last block, strictly (the contract; unchanged for models that fence, e.g. Qwen)
    raw = _last_fenced_json(content)
    if raw is not None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            if has_diff_block(content):
                return Action("submit", {}, raw=raw, error=f"json parse failed ({e}); diff present -> submit")
            return Action("none", {}, raw=raw, error=f"json parse failed: {e}")
        if isinstance(obj, dict) and isinstance(obj.get("action"), str):
            return _from_obj(obj, raw, content)
        if has_diff_block(content):
            return Action("submit", {}, raw=raw, error="malformed json action; diff present -> submit")
        return Action("none", {}, raw=raw, error="json action block is not an object with a string 'action'")

    # 2) no ```json fence: a diff block is an implicit submit -- BEFORE scanning bare objects, so braces
    #    inside the diff/code cannot be mistaken for the action.
    if has_diff_block(content):
        return Action("submit", {}, error="no json fence; diff present -> submit")

    # 3) bare JSON action object (gpt-oss omits the fence); take the LAST valid one (the model's final choice)
    objs = [o for o in _bare_action_objects(content) if isinstance(o.get("action"), str)]
    if objs:
        obj = objs[-1]
        return _from_obj(obj, json.dumps(obj, ensure_ascii=False), content)
    return Action("none", {}, error="no json action block found")
