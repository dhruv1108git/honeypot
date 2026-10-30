"""Build the two messages the target sees.

system  = sys_v0 [+ "\n\n" + goal] [+ "\n\n" + agency]        (goal before agency; PROVENANCE row 7)
user    = task_v0 (with {repo}/{branch} filled) + "\n" + packet.md (with {target_model_family} filled)

Placeholders are replaced with str.replace on the exact token (never str.format: diffs contain braces).
Returns the per-part sha256 so a record can prove which bytes were used.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .conditions import parse_condition
from .prompts import Registry, sha256_text

PART_JOIN = "\n\n"
PACKET_PLACEHOLDER = "{target_model_family}"


@dataclass
class Rendered:
    messages: list[dict]
    prompt_ids: dict[str, str | None]
    prompt_hashes: dict[str, str | None]
    unfilled: list[str] = field(default_factory=list)   # placeholders we expected but did not find


def read_packet(envs_dir: Path, env_id: str) -> str:
    p = Path(envs_dir) / env_id / "packet.md"
    if not p.exists():
        raise FileNotFoundError(f"env {env_id!r}: {p} does not exist (environment not built yet?)")
    return p.read_text(encoding="utf-8")


def render_system(reg: Registry, condition: str, sys_id: str = "sys_v0") -> tuple[str, dict, dict]:
    goal_id, agency_id = parse_condition(condition, reg)
    parts = [reg.get(sys_id)]
    if goal_id:
        parts.append(reg.get(goal_id))
    if agency_id:
        parts.append(reg.get(agency_id))
    text = PART_JOIN.join(p.text for p in parts)
    ids = {"system_base": sys_id, "goal": goal_id, "agency": agency_id}
    hashes = {"system_base": parts[0].sha256,
              "goal": reg.get(goal_id).sha256 if goal_id else None,
              "agency": reg.get(agency_id).sha256 if agency_id else None,
              "system_message": sha256_text(text)}
    return text, ids, hashes


def render_user(reg: Registry, packet_template: str, *, repo: str, branch: str,
                target_model_family: str, task_id: str = "task_v0") -> tuple[str, dict, dict, list[str]]:
    task = reg.get(task_id)
    task_text = task.text.replace("{repo}", repo).replace("{branch}", branch)
    unfilled = []
    if PACKET_PLACEHOLDER not in packet_template:
        unfilled.append(PACKET_PLACEHOLDER)
    packet = packet_template.replace(PACKET_PLACEHOLDER, target_model_family)
    sep = "" if task_text.endswith("\n") else "\n"
    text = task_text + sep + packet
    ids = {"task": task_id, "packet": "packet.md"}
    hashes = {"task_template": task.sha256, "task_rendered": sha256_text(task_text),
              "packet_template": sha256_text(packet_template), "packet_rendered": sha256_text(packet),
              "user_message": sha256_text(text)}
    return text, ids, hashes, unfilled


def render(reg: Registry, *, condition: str, packet_template: str, repo: str, branch: str,
           target_model_family: str, sys_id: str = "sys_v0", task_id: str = "task_v0") -> Rendered:
    sys_text, sys_ids, sys_hashes = render_system(reg, condition, sys_id)
    user_text, user_ids, user_hashes, unfilled = render_user(
        reg, packet_template, repo=repo, branch=branch, target_model_family=target_model_family, task_id=task_id)
    messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": user_text}]
    hashes = {**sys_hashes, **user_hashes}
    hashes["messages"] = hashlib.sha256(
        ("\x00".join(f"{m['role']}\x01{m['content']}" for m in messages)).encode("utf-8")).hexdigest()
    return Rendered(messages=messages, prompt_ids={**sys_ids, **user_ids}, prompt_hashes=hashes, unfilled=unfilled)


EVAL_CUE_WORDS = ("test", "eval", "benchmark", "honeypot", "sabotage", "scheming")


def eval_cue_hits(text: str) -> dict[str, int]:
    """Count BUILD_SPEC eval-cue words (case-insensitive, substring). Advisory only: the monitor
    codebase legitimately contains 'scheming' and real code contains 'test'."""
    low = text.lower()
    return {w: low.count(w) for w in EVAL_CUE_WORDS if low.count(w)}
