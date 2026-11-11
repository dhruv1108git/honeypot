"""One `generate()` over three backends: vllm (OpenAI-compatible, local), openrouter, anthropic.

All backends go through httpx directly (no vendor SDK), so the JSON body we record is byte-for-byte
the body we posted, and there are no hidden SDK retries: every attempt is one Record.
`generate()` never raises on model/transport errors; it returns a Record with `ok=False` and `error`.

Keys are read from env vars (OPENROUTER_API_KEY, ANTHROPIC_API_KEY) at call time, only put in
headers, and scrubbed from EVERY stored payload: error text, error bodies, successful response bodies,
`/models` and render-probe bodies. They are never logged.

A 200 whose body is not a usable completion (no choices, no message, empty content AND no reasoning,
no finish_reason, missing/empty prompt or completion token ids when `return_token_ids` was requested,
or a body that cannot be parsed / does not fit the Record schema) is a failed, RETRYABLE attempt
(`error.type == "MalformedResponse"`), never an ok record. The only non-retryable failures are config
problems found before the request is sent (bad params, missing key).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .records import ErrorInfo, ModelInfo, Record

BACKENDS = ("vllm", "openrouter", "anthropic")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "vllm": "VLLM_API_KEY"}
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Anthropic Messages API accepts only these sampling fields; anything else is an explicit error,
# never silently dropped (Codex review, "Backend contracts").
ANTHROPIC_PARAMS = {"max_tokens", "temperature", "top_p", "top_k", "thinking", "stop_sequences", "metadata"}

# Body fields that `params`/`extra` may never set: the runner owns them (Codex review, probable bugs).
# `provider` is the OpenRouter pin block: it has its own ModelSpec field (Codex round 2, reserved fields).
RESERVED_BODY_FIELDS = {"model", "messages", "system", "stream", "n", "provider", "models", "route"}

# Sampling fields: allowed in `params` only, never in `extra`, so the record's `params` is exactly the
# sampling part of the posted body (Codex round 2: `extra: {temperature: 0.7}` was sent with `params: {}`).
SAMPLING_FIELDS = {
    "temperature", "top_p", "top_k", "min_p", "max_tokens", "max_completion_tokens", "seed",
    "reasoning_effort", "reasoning", "thinking", "stop", "stop_sequences", "presence_penalty",
    "frequency_penalty", "repetition_penalty", "length_penalty", "logit_bias", "logprobs", "top_logprobs",
    "best_of", "use_beam_search", "ignore_eos", "min_tokens", "response_format", "guided_json",
    "guided_regex", "guided_choice", "guided_grammar", "structured_outputs", "truncate_prompt_tokens",
    "include_stop_str_in_output", "top_a", "stop_token_ids", "bad_words", "allowed_token_ids",
}

# Backend defaults the runner puts into `params` at manifest-build time (never injected into the body behind
# the record's back), so `params` is exactly the sampling part of the posted body for every backend.
BACKEND_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {"openrouter": {"reasoning": {"enabled": True}},
                                                     "anthropic": {"max_tokens": 32768}, "vllm": {}}


def default_params(backend: str) -> dict[str, Any]:
    return json.loads(json.dumps(BACKEND_DEFAULT_PARAMS.get(backend, {})))

# vLLM SamplingParams fields that are engine bookkeeping, not sampling knobs (dropped from effective_params).
VLLM_INTERNAL_SAMPLING_FIELDS = {"output_kind", "skip_clone", "skip_reading_prefix_cache"}

REDACTED = "[REDACTED:api_key]"
MIN_SECRET_LEN = 8   # a shorter "key" is not a secret; scrubbing it would corrupt every payload


class MalformedResponse(ValueError):
    """HTTP 200 whose body is not a usable completion. Retryable."""


def check_param_fields(params: dict[str, Any], extra: dict[str, Any]) -> None:
    """`params` and `extra` may not set runner-owned body fields, may not overlap, and `extra` may not carry
    sampling fields (else the record's `params` would disagree with the posted body). Raises ValueError."""
    reserved = (set(params) | set(extra)) & RESERVED_BODY_FIELDS
    if reserved:
        raise ValueError(f"params/extra may not set {sorted(reserved)} (runner-owned body fields; the OpenRouter "
                         f"pin block goes in the model's `provider` field)")
    sampling_in_extra = set(extra) & SAMPLING_FIELDS
    if sampling_in_extra:
        raise ValueError(f"extra may not set sampling fields {sorted(sampling_in_extra)}; they belong in params "
                         f"(the record's params must equal the sampling part of the request body)")
    overlap = set(params) & set(extra)
    if overlap:
        raise ValueError(f"params and extra both set {sorted(overlap)}; a sampling field belongs in params only")


@dataclass
class ModelSpec:
    id: str
    backend: str
    model: str
    family: str
    url: str | None = None
    reasoning_visibility: str = "raw"
    concurrency: int = 8
    timeout_s: float = 3600.0
    params: dict[str, Any] = field(default_factory=dict)   # sampling fields sent as-is
    extra: dict[str, Any] = field(default_factory=dict)    # non-sampling body fields (e.g. return_token_ids)
    provider: dict[str, Any] | None = None                 # OpenRouter provider pin block
    api_key_env: str | None = None
    render_probe: bool = False

    def __post_init__(self):
        if self.backend not in BACKENDS:
            raise ValueError(f"unknown backend {self.backend!r}; expected one of {BACKENDS}")
        if self.url is None:
            self.url = {"openrouter": OPENROUTER_URL, "anthropic": ANTHROPIC_URL}.get(self.backend)
        if not self.url:
            raise ValueError(f"model {self.id}: backend {self.backend} needs a url")
        self.url = self.url.rstrip("/")
        if self.api_key_env is None:
            self.api_key_env = DEFAULT_KEY_ENV[self.backend]
        if not isinstance(self.concurrency, int) or isinstance(self.concurrency, bool) or self.concurrency < 1:
            raise ValueError(f"model {self.id}: concurrency must be an integer >= 1 (got {self.concurrency!r})")
        if not isinstance(self.timeout_s, (int, float)) or isinstance(self.timeout_s, bool) or self.timeout_s <= 0:
            raise ValueError(f"model {self.id}: timeout_s must be > 0 (got {self.timeout_s!r})")
        try:
            check_param_fields(self.params, self.extra)
        except ValueError as e:
            raise ValueError(f"model {self.id}: {e}") from None

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None

    def secrets(self) -> list[str]:
        return [s for s in (self.api_key(),) if s and len(s) >= MIN_SECRET_LEN]

    def wants_token_ids(self) -> bool:
        return bool(self.extra.get("return_token_ids"))

    def info(self) -> ModelInfo:
        return ModelInfo(id=self.id, backend=self.backend, url=self.url, model=self.model, family=self.family,
                         reasoning_visibility=self.reasoning_visibility, provider=self.provider)

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=30.0, read=self.timeout_s, write=120.0, pool=None)

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout(),
                                 limits=httpx.Limits(max_connections=self.concurrency + 2,
                                                     max_keepalive_connections=self.concurrency))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, REDACTED)
    return text


def _scrub_obj(obj: Any, secrets: list[str]) -> Any:
    """Deep-scrub strings in a JSON-like object (dict keys included)."""
    if not secrets:
        return obj
    if isinstance(obj, str):
        return _scrub(obj, secrets)
    if isinstance(obj, (list, tuple)):
        return [_scrub_obj(x, secrets) for x in obj]
    if isinstance(obj, dict):
        return {_scrub(k, secrets) if isinstance(k, str) else k: _scrub_obj(v, secrets) for k, v in obj.items()}
    return obj


def redact(obj: Any, secrets: list[str]) -> Any:
    """Public deep-scrub of a JSON-like object. The runner's writers (`hp.runner.dump_redacted`) pass EVERY
    persisted object through this: records, manifest, invocations. Nothing reaches disk otherwise."""
    return _scrub_obj(obj, secrets)


def contains_secret(obj: Any, secrets: list[str]) -> bool:
    """True if any string in the JSON-like object contains one of the secrets."""
    return bool(secrets) and _scrub_obj(obj, secrets) != obj


def _body_of(resp: httpx.Response, secrets: list[str], text_limit: int = 20000) -> tuple[Any, bool]:
    """(scrubbed body, was_json). Any JSON value is kept as-is; otherwise the (truncated) text."""
    try:
        return _scrub_obj(resp.json(), secrets), True
    except Exception:
        return _scrub(resp.text[:text_limit], secrets), False


def effective_params(params: dict[str, Any], server_defaults: dict[str, Any] | None) -> tuple[dict, str]:
    """Requested params over the server-resolved defaults (where known). The requested values win so a
    per-item field (seed) is never masked by the probe item's value."""
    if server_defaults:
        return {**server_defaults, **params}, "requested+vllm_render_probe"
    return dict(params), "requested_only"


def server_defaults_from_render(render_result: dict[str, Any] | None, probe_params: dict[str, Any]) -> dict | None:
    """Pull vLLM's resolved SamplingParams out of a render-probe result, minus the fields the probe
    itself requested (those are not defaults) and engine-internal flags. None if unavailable."""
    if not render_result or not render_result.get("ok"):
        return None
    resp = render_result.get("response")
    sp = resp.get("sampling_params") if isinstance(resp, dict) else None
    if not isinstance(sp, dict):
        return None
    return {k: v for k, v in sp.items() if k not in probe_params and k not in VLLM_INTERNAL_SAMPLING_FIELDS}


# ---------------------------------------------------------------- request building

def build_body(spec: ModelSpec, messages: list[dict], params: dict[str, Any]) -> tuple[str, dict]:
    """Returns (endpoint_url, body): the exact JSON body for one request, built WITHOUT any secret, so it can be
    computed anywhere (manifest body hash, cancellation records, dry-run) regardless of key availability.
    Raises ValueError on a body that violates the field contract."""
    check_param_fields(params, spec.extra)
    if spec.backend in ("vllm", "openrouter"):
        body: dict[str, Any] = {"model": spec.model, "messages": messages}
        body.update(params)
        body.update(spec.extra)
        if spec.backend == "openrouter" and spec.provider:
            body["provider"] = spec.provider
        return f"{spec.url}/chat/completions", body

    # anthropic
    unsupported = set(params) - ANTHROPIC_PARAMS
    if unsupported:
        raise ValueError(f"anthropic backend does not accept params {sorted(unsupported)}; "
                         f"remove them from the model's params (nothing is dropped silently)")
    system = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    body = {"model": spec.model, "messages": rest}
    if system:
        body["system"] = "\n\n".join(system)
    body.update(params)
    body.update(spec.extra)
    return f"{spec.url}/messages", body


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def body_sha256(body: dict) -> str:
    """sha256 of the canonical JSON of a request body (manifest `request_body_sha256`; recomputed from the
    stored `request_body` on resume)."""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def build_request(spec: ModelSpec, messages: list[dict], params: dict[str, Any]) -> tuple[str, dict, dict]:
    """Returns (endpoint_url, headers, body). Headers hold the key; body never does."""
    url, body = build_body(spec, messages, params)
    headers = {"Content-Type": "application/json"}
    key = spec.api_key()
    if spec.backend in ("vllm", "openrouter"):
        if spec.backend == "openrouter" and key is None:
            raise RuntimeError(f"{spec.api_key_env} not set (needed for backend openrouter)")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return url, headers, body
    if key is None:
        raise RuntimeError(f"{spec.api_key_env} not set (needed for backend anthropic)")
    headers.update({"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
    return url, headers, body


# ---------------------------------------------------------------- response parsing

def parse_openai(raw: dict) -> dict[str, Any]:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MalformedResponse("response has no choices")
    ch = choices[0]
    if not isinstance(ch, dict):
        raise MalformedResponse("choices[0] is not an object")
    msg = ch.get("message")
    if not isinstance(msg, dict):
        raise MalformedResponse("choices[0] has no message")
    reasoning, source = msg.get("reasoning"), "reasoning"
    if reasoning is None and msg.get("reasoning_content") is not None:
        reasoning, source = msg["reasoning_content"], "reasoning_content"
    if reasoning is None and msg.get("reasoning_details"):   # OpenRouter shape
        texts = [d.get("text") or d.get("summary") for d in msg["reasoning_details"] if isinstance(d, dict)]
        texts = [t for t in texts if t]
        reasoning, source = ("\n\n".join(texts) if texts else None), "reasoning_details"
    if reasoning is None:
        source = None
    finish = ch.get("finish_reason")
    content = msg.get("content")
    if isinstance(content, list):   # content-parts shape some gateways return
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if reasoning is not None and not isinstance(reasoning, str):
        reasoning = json.dumps(reasoning, ensure_ascii=False)
    return dict(reasoning=reasoning, reasoning_source=source, content=content,
                usage=raw.get("usage"), finish_reason=finish, truncated=(finish == "length"),
                prompt_token_ids=raw.get("prompt_token_ids"), completion_token_ids=ch.get("token_ids"),
                response_model=raw.get("model"), provider_resolved=raw.get("provider"))


def parse_anthropic(raw: dict) -> dict[str, Any]:
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        raise MalformedResponse("response has no content blocks")
    blocks = [b for b in blocks if isinstance(b, dict)]
    thinking = [b.get("thinking", "") for b in blocks if b.get("type") == "thinking"]
    redacted = sum(1 for b in blocks if b.get("type") == "redacted_thinking")
    text = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    reasoning = "\n\n".join(thinking) if thinking else None
    source = "thinking_blocks" if thinking else ("redacted_thinking" if redacted else None)
    stop = raw.get("stop_reason")
    return dict(reasoning=reasoning, reasoning_source=source, content="".join(text) if text else None,
                usage=raw.get("usage"), finish_reason=stop, truncated=(stop == "max_tokens"),
                prompt_token_ids=None, completion_token_ids=None,
                response_model=raw.get("model"), provider_resolved="anthropic")


def check_usable(parsed: dict[str, Any], spec: ModelSpec) -> None:
    """Raise MalformedResponse for a structurally valid but unusable completion."""
    content = parsed.get("content")
    reasoning = parsed.get("reasoning")
    has_content = isinstance(content, str) and content.strip() != ""
    has_reasoning = isinstance(reasoning, str) and reasoning.strip() != ""
    if not has_content and not has_reasoning and parsed.get("reasoning_source") != "redacted_thinking":
        raise MalformedResponse("empty content and no reasoning")
    if not isinstance(parsed.get("finish_reason"), str) or not parsed["finish_reason"]:
        raise MalformedResponse("finish_reason missing (truncation cannot be determined)")
    if spec.wants_token_ids() and spec.backend != "anthropic":
        for f_ in ("prompt_token_ids", "completion_token_ids"):
            v = parsed.get(f_)
            if not isinstance(v, list) or not v:
                raise MalformedResponse(f"{f_} missing or empty although return_token_ids was requested")


# ---------------------------------------------------------------- generate

def failed_record(spec: ModelSpec, messages: list[dict], params: dict[str, Any], *, meta: dict,
                  exc_type: str, message: str, retryable: bool, started: str | None = None,
                  latency_s: float = 0.0, request_body: dict | None = None,
                  server_defaults: dict[str, Any] | None = None) -> Record:
    """A failed-attempt Record built outside generate() (e.g. the invocation was cancelled mid-request)."""
    secrets = spec.secrets()
    eff, eff_src = effective_params(params, server_defaults)
    return Record(**meta, timestamp=started or utcnow(), model=spec.info(), params=dict(params),
                  effective_params=eff, effective_params_source=eff_src, messages=messages,
                  request_body=request_body or {}, ok=False, latency_s=round(latency_s, 3),
                  timestamp_end=utcnow(),
                  error=ErrorInfo(type=exc_type, message=_scrub(message, secrets)[:4000], retryable=retryable))


async def generate(spec: ModelSpec, messages: list[dict], params: dict[str, Any], *, meta: dict,
                   client: httpx.AsyncClient | None = None,
                   server_defaults: dict[str, Any] | None = None) -> Record:
    """One call -> one Record. `meta` supplies the run/item fields of the Record (see runner).
    `server_defaults`: the server-resolved sampling defaults for this model (render probe), if known."""
    t0 = time.monotonic()
    started = utcnow()
    secrets = spec.secrets()
    eff, eff_src = effective_params(params, server_defaults)
    base = dict(meta, timestamp=started, model=spec.info(), params=dict(params), effective_params=eff,
                effective_params_source=eff_src, messages=messages)

    def fail(exc_type: str, message: str, status: int | None, retryable: bool, body: dict | None,
             raw: Any = None) -> Record:
        return Record(**base, request_body=body or {}, ok=False, raw_response=_scrub_obj(raw, secrets),
                      latency_s=round(time.monotonic() - t0, 3), timestamp_end=utcnow(),
                      error=ErrorInfo(type=exc_type, message=_scrub(message, secrets)[:4000],
                                      http_status=status, retryable=retryable))

    try:
        url, headers, body = build_request(spec, messages, params)
    except Exception as e:  # config problems: not retryable
        return fail(type(e).__name__, str(e), None, False, None)

    own_client = client is None
    if own_client:
        client = spec.make_client()
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        return fail("Timeout", f"{type(e).__name__}: {e}", None, True, body)
    except httpx.HTTPError as e:
        return fail("TransportError", f"{type(e).__name__}: {e}", None, True, body)
    except Exception as e:
        return fail(type(e).__name__, str(e), None, True, body)
    finally:
        if own_client:
            await client.aclose()

    status = resp.status_code
    raw, was_json = _body_of(resp, secrets)     # scrubbed; any JSON value or the text
    if status != 200:
        shown = json.dumps(raw, ensure_ascii=False) if was_json else raw
        return fail("HTTPStatus", f"HTTP {status}: {shown[:2000]}", status, status in RETRYABLE_STATUS, body, raw)
    if not was_json:
        return fail("MalformedResponse", f"non-JSON 200 response: {raw[:500]}", status, True, body, raw)
    if not isinstance(raw, dict):
        return fail("MalformedResponse", f"200 body is not a JSON object: {json.dumps(raw)[:500]}", status, True,
                    body, raw)
    if raw.get("error") and not raw.get("choices"):   # OpenRouter-style in-band upstream error
        return fail("ProviderError", json.dumps(raw["error"], ensure_ascii=False)[:2000], status, True, body, raw)
    try:
        parsed = parse_anthropic(raw) if spec.backend == "anthropic" else parse_openai(raw)
        check_usable(parsed, spec)
    except MalformedResponse as e:
        return fail("MalformedResponse", f"{e}: {json.dumps(raw, ensure_ascii=False)[:500]}", status, True, body, raw)
    except Exception as e:   # a 200 we cannot parse is a malformed response: retryable (Codex round 2, #7)
        return fail("MalformedResponse", f"parse failed, {type(e).__name__}: {e}", status, True, body, raw)
    if parsed["provider_resolved"] is None:
        parsed["provider_resolved"] = spec.backend
    try:
        return Record(**base, request_body=body, ok=True, raw_response=raw,
                      latency_s=round(time.monotonic() - t0, 3), timestamp_end=utcnow(), **parsed)
    except Exception as e:   # the 200 does not fit the Record schema (e.g. usage not an object): retryable, and
        # still a ledger line (Codex round 2, #7: a non-retryable RecordError permanently stalled the item)
        return fail("MalformedResponse", f"response does not fit the record schema, {type(e).__name__}: {e}",
                    status, True, body, raw)


def generate_sync(spec: ModelSpec, messages: list[dict], params: dict[str, Any], *, meta: dict) -> Record:
    import asyncio
    return asyncio.run(generate(spec, messages, params, meta=meta))


# ---------------------------------------------------------------- server probes

async def probe_models(spec: ModelSpec, client: httpx.AsyncClient) -> dict[str, Any]:
    """GET <url>/models. Returns {"ok": bool, "response": ..., "error": ...}; never raises; body scrubbed."""
    headers = {}
    key = spec.api_key()
    secrets = spec.secrets()
    if key and spec.backend != "anthropic":
        headers["Authorization"] = f"Bearer {key}"
    if spec.backend == "anthropic":
        headers = {"x-api-key": key or "", "anthropic-version": ANTHROPIC_VERSION}
    try:
        r = await client.get(f"{spec.url}/models", headers=headers, timeout=httpx.Timeout(20.0))
        data, _ = _body_of(r, secrets, text_limit=2000)
        return {"ok": r.status_code == 200, "status": r.status_code, "response": data, "checked_at": utcnow()}
    except Exception as e:
        return {"ok": False, "error": _scrub(f"{type(e).__name__}: {e}", secrets), "checked_at": utcnow()}


async def render_probe(spec: ModelSpec, messages: list[dict], params: dict[str, Any],
                       client: httpx.AsyncClient) -> dict[str, Any]:
    """POST <url>/chat/completions/render (vLLM >= 0.22): the exact engine prompt (incl. the Harmony
    header for gpt-oss) and the resolved SamplingParams for one request, without generating.
    Best effort; never raises; body scrubbed."""
    if spec.backend != "vllm":
        return {"ok": False, "error": "render probe only for vllm backend"}
    secrets = spec.secrets()
    try:
        _, headers, body = build_request(spec, messages, params)
        r = await client.post(f"{spec.url}/chat/completions/render", headers=headers, json=body,
                              timeout=httpx.Timeout(120.0))
        data, _ = _body_of(r, secrets, text_limit=2000)
        return {"ok": r.status_code == 200, "status": r.status_code, "response": data, "checked_at": utcnow()}
    except Exception as e:
        return {"ok": False, "error": _scrub(f"{type(e).__name__}: {e}", secrets), "checked_at": utcnow()}
