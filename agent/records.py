"""Trajectory record schema for an agentic episode (outside hp/, so code_hash() is untouched)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str                       # list_dir | read_file | grep | submit | none
    args: dict[str, Any] = {}
    ok: bool = True
    error: str | None = None


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    reasoning: str | None = None
    content: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    action: ToolCall
    result: str | None = None       # tool result shown back to the model (truncated)
    result_bytes: int = 0
    result_truncated: bool = False


class TrajectoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    run_id: str
    item_key: str                   # "<env_id>|<condition>|<model_id>|<sample_idx>"
    env_id: str
    condition: str
    sample_idx: int
    arm: str                        # none | memoA | memoB
    model: dict[str, Any]           # {id, backend, url, model, family}
    prompt_ids: dict[str, Any] = {}
    prompt_hashes: dict[str, Any] = {}
    system_prompt: str
    initial_user: str
    turns: list[Turn] = []
    read_events: list[dict[str, Any]] = []   # [{path, turn, bytes, ok}]
    files_opened: list[str] = []             # deduped repo-relative paths read OK  (memo-study signal)
    # memo-exposure instrumentation (goal-in-memo study): distinguish encountered vs opened vs directive returned
    sys_id: str = "sys_v0"                   # which system-prompt base was used ("sys_none" = no system message)
    task_id: str = "task_agent_v0"           # which task template was used
    memo_encountered: bool = False           # docs/audit-coverage.md appeared in a list_dir result
    memo_encountered_turn: int | None = None
    memo_opened_turn: int | None = None      # first read_file on docs/audit-coverage.md
    directive_exposed: bool = False          # a read_file/grep result actually returned the embedded goal directive
    directive_exposed_turn: int | None = None
    final_diff: str = ""
    submitted: bool = False
    finish: str = ""                # submitted | max_turns | no_submit | truncated | error
    usage_total: dict[str, Any] = {}
    ok: bool = False                # produced a rateable completion (submitted or has a final diff)
    error: str | None = None
    timestamp: str
    timestamp_end: str | None = None
