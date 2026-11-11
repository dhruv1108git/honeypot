"""Record schema: one JSON line per model call (BUILD_SPEC "Records").

Every attempt, successful or not, is a Record. `ok` is True when a response was obtained
(a `finish_reason == "length"` truncation is still a sample: ok=True, truncated=True).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2   # 2: raw_response is any JSON/text; effective_params; hp_* fields = the calling process


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                      # config name, e.g. "qwen3-235b-thinking"
    backend: str                 # vllm | openrouter | anthropic
    url: str                     # base URL the request went to
    model: str                   # served/provider model name sent in the request
    family: str                  # fills {target_model_family} in the packet
    reasoning_visibility: str    # raw | summarized | hidden
    provider: dict[str, Any] | None = None   # OpenRouter provider pin block, if any


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    run_id: str
    item_key: str                     # "<env_id>|<condition>|<model_id>|<sample_idx>"
    timestamp: str                    # UTC ISO-8601, request start
    timestamp_end: str | None = None  # UTC ISO-8601, response received / error raised
    hp_git_commit: str | None         # `git rev-parse HEAD` of hp/ in the PROCESS THAT MADE THIS CALL (None before
                                      # the first commit); the manifest keeps the values of the first start
    hp_git_dirty: bool | None
    hp_code_hash: str                 # sha256 over hp/*.py + prompts/* of the calling process
    config_sha256: str

    env_id: str
    condition: str
    sample_idx: int
    attempt: int = Field(ge=1)        # 1-based, cumulative across restarts of the same run_id

    model: ModelInfo
    params: dict[str, Any]            # every sampling field sent (temperature, top_p, top_k, min_p,
                                      # max_tokens, seed, reasoning_effort, ...)
    effective_params: dict[str, Any] | None = None   # requested params over the server's resolved defaults
                                                     # (vLLM render probe SamplingParams) where known
    effective_params_source: str | None = None       # "requested+vllm_render_probe" | "requested_only"
    messages: list[dict[str, Any]]    # the full messages sent (system, user)
    request_body: dict[str, Any]      # the exact JSON body posted (messages included again by design)
    prompt_ids: dict[str, str | None]     # part -> prompt id (sys, goal, agency, task, packet)
    prompt_hashes: dict[str, str | None]  # part -> sha256 of the text actually used, plus the whole messages

    ok: bool
    raw_response: Any = None          # the response body as received: JSON object/list/scalar, or the text
                                      # when the body was not JSON; secrets redacted
    reasoning: str | None = None
    reasoning_source: str | None = None    # which field it came from: reasoning | reasoning_content | thinking_blocks
    content: str | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    truncated: bool = False
    prompt_token_ids: list[int] | None = None   # vLLM `return_token_ids` (exact prompt incl. Harmony header)
    completion_token_ids: list[int] | None = None
    response_model: str | None = None     # what the server says it served
    provider_resolved: str | None = None  # OpenRouter `provider` field / "vllm" / "anthropic"
    latency_s: float
    error: ErrorInfo | None = None

    def to_json(self, secrets: list[str] | None = None) -> str:
        """One JSONL line. The ledger writer passes the run's secrets (hp.runner.dump_redacted is the choke
        point for everything persisted); callers that print a record elsewhere should do the same."""
        import json
        from .backends import redact
        return json.dumps(redact(self.model_dump(mode="json"), list(secrets or [])), ensure_ascii=False)
