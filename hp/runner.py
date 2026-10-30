"""Runner: YAML config -> env x condition x model x sample manifest -> records.jsonl.

    /scratch2/dhruv/uv_envs/ais-hp/bin/python -m hp.runner configs/replication_v1.yaml [--dry-run] [--limit N]
        [--models a,b] [--envs ...] [--conditions ...] [--run-id X]

Exit codes: 0 all selected items have an ok record; 1 some attempted items failed; 2 items were skipped
(server down or --limit); 3 refused (bad inputs, missing packets, frozen inputs changed, run locked).
Re-run the same command until it exits 0.

Guarantees:
  * runs/<run_id>/records.jsonl is append-only (one JSON line per attempt, ensure_ascii=False);
  * restart skips items that already have a VALID ok record: schema version, attempt >= 1, the messages hash
    RECOMPUTED from the stored messages, model block / params / request-body hash equal to the manifest's item
    spec, and a usable completion (content or reasoning, finish_reason, no error); invalid or duplicate ledger
    lines are reported by line number and item; a partial trailing line (crash) is terminated with "\n" before
    the next write; failed items are retried with a cumulative attempt number (a failure never permanently
    skips an item);
  * the manifest freezes the inputs on first start: config hash, used prompt hashes, per-env packet hash +
    resolved repo/branch, per-model target_model_family, and the sha256 of every item's rendered messages;
    a later start with any of them changed is refused (new inputs = new run_id);
  * every record carries the code hash / git commit / dirty flag of the process that made the call; the
    manifest keeps the values of the first start;
  * one process per run_id (exclusive lockfile with pid + host); a second process is refused;
  * execution order is a deterministic shuffle (order_seed) so cells interleave in time;
  * concurrency is bounded per server URL (explicit: models sharing a URL must agree); a server that is
    down at start is skipped with a warning;
  * every persisted object (ledger line, manifest, invocation line) goes through ONE serialiser
    (`dump_redacted`) that replaces every configured API key; a request body that would contain a key is
    refused up front (it could never be validated on resume);
  * sampling fields live in `params` only and `provider` has its own field, so `params` == the sampling part
    of the posted body; concurrency / max_attempts / n_samples >= 1, timeout_s > 0, backoff_s / --limit >= 0;
  * --dry-run writes nothing (no run dir, no manifest, no lock, no log file).
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tqdm import tqdm

from . import HP_ROOT
from .backends import (ModelSpec, body_sha256, build_body, contains_secret, default_params, failed_record, generate,
                       probe_models, redact, render_probe, server_defaults_from_render, utcnow)
from .conditions import parse_condition
from .prompts import Registry, sha256_bytes, sha256_text
from .records import SCHEMA_VERSION, Record
from .render import PACKET_PLACEHOLDER, Rendered, eval_cue_hits, read_packet, render

LOG_DIR = Path("/scratch2/dhruv/logs/ais/hp")
LOCK_FILE = "lock"
KEY_SEP = "|"
log = logging.getLogger("hp.runner")


class RunnerError(RuntimeError):
    """A refusal: bad inputs, missing packets, changed frozen inputs, run locked. Exit code 3."""


# ---------------------------------------------------------------- config

class EnvCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repo: str | None = None      # fills {repo} in task_v0; falls back to envs/<id>/env.yaml
    branch: str | None = None    # fills {branch}
    allow_missing_family_placeholder: bool = False   # explicit opt-out of BUILD_SPEC self-relevance (E5)


class ModelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    backend: str
    model: str
    family: str
    url: str | None = None
    reasoning_visibility: str = "raw"
    concurrency: int = Field(8, ge=1)          # 0 would create a semaphore nobody can acquire
    timeout_s: float = Field(3600.0, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    provider: dict[str, Any] | None = None
    api_key_env: str | None = None
    render_probe: bool = False

    def spec(self) -> ModelSpec:
        return ModelSpec(**self.model_dump())


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    description: str = ""
    runs_dir: str = str(HP_ROOT / "runs")
    envs_dir: str = str(HP_ROOT / "envs")
    system_prompt_id: str = "sys_v0"
    task_prompt_id: str = "task_v0"
    n_samples: int = Field(ge=1)
    order_seed: int = 0
    max_attempts: int = Field(3, ge=1)       # per item per invocation; restarts continue the count (0 = no calls)
    backoff_s: float = Field(10.0, ge=0)     # base of exponential backoff between attempts (capped at 120 s)
    seed_from_sample_idx: bool = True  # send seed=sample_idx where the backend supports it (vllm/openrouter)
    envs: list[EnvCfg]
    conditions: list[str]
    models: list[ModelCfg]


def load_config(path: Path) -> tuple[RunConfig, str, dict]:
    data = Path(path).read_bytes()
    cfg = RunConfig.model_validate(yaml.safe_load(data))
    return cfg, sha256_bytes(data), yaml.safe_load(data)


def _dupes(xs: list[str]) -> list[str]:
    return sorted(k for k, n in Counter(xs).items() if n > 1)


def validate_config(cfg: RunConfig) -> dict[str, int]:
    """IDs unique and free of the key separator; models sharing a URL agree on concurrency.
    Returns {url: concurrency}. Raises RunnerError."""
    problems: list[str] = []
    if KEY_SEP in cfg.run_id or not cfg.run_id.strip():
        problems.append(f"run_id {cfg.run_id!r} is empty or contains {KEY_SEP!r}")
    for what, ids in (("env", [e.id for e in cfg.envs]), ("model", [m.id for m in cfg.models]),
                      ("condition", list(cfg.conditions))):
        if not ids:
            problems.append(f"no {what}s configured")
        for d in _dupes(ids):
            problems.append(f"duplicate {what} id {d!r}")
        for i in ids:
            if KEY_SEP in i or not i.strip():
                problems.append(f"{what} id {i!r} is empty or contains {KEY_SEP!r} (reserved for item keys)")
    # Numeric execution controls (Codex round 2, new bug 3). Also enforced by the pydantic Field constraints at
    # load time; repeated here because a RunConfig can be modified after loading.
    for name, val, lo in (("n_samples", cfg.n_samples, 1), ("max_attempts", cfg.max_attempts, 1),
                          ("backoff_s", cfg.backoff_s, 0)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < lo:
            problems.append(f"{name} must be >= {lo} (got {val!r})")
    for m in cfg.models:
        if not isinstance(m.concurrency, int) or isinstance(m.concurrency, bool) or m.concurrency < 1:
            problems.append(f"model {m.id}: concurrency must be >= 1 (got {m.concurrency!r})")
        if not isinstance(m.timeout_s, (int, float)) or isinstance(m.timeout_s, bool) or m.timeout_s <= 0:
            problems.append(f"model {m.id}: timeout_s must be > 0 (got {m.timeout_s!r})")
    url_conc: dict[str, int] = {}
    url_models: dict[str, list[str]] = {}
    for m in cfg.models:
        try:
            spec = m.spec()
        except ValueError as e:
            problems.append(str(e)); continue
        url_models.setdefault(spec.url, []).append(m.id)
        if cfg.seed_from_sample_idx and m.backend in ("vllm", "openrouter") and "seed" in m.params:
            problems.append(f"model {m.id}: params.seed would silently disable seed_from_sample_idx; remove it or "
                            f"set seed_from_sample_idx: false")
        if spec.url in url_conc and url_conc[spec.url] != m.concurrency:
            problems.append(f"models {url_models[spec.url]} share url {spec.url} but disagree on concurrency "
                            f"({url_conc[spec.url]} vs {m.concurrency}); the limit is per URL, set them equal")
        url_conc.setdefault(spec.url, m.concurrency)
    if problems:
        raise RunnerError("invalid config:\n  - " + "\n  - ".join(problems))
    return url_conc


# ---------------------------------------------------------------- provenance

def code_hash() -> str:
    h = hashlib.sha256()
    files = sorted((HP_ROOT / "hp").glob("*.py")) + sorted(p for p in (HP_ROOT / "prompts").iterdir() if p.is_file())
    for f in files:
        h.update(str(f.relative_to(HP_ROOT)).encode()); h.update(b"\0"); h.update(f.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def git_state() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(["git", "-C", str(HP_ROOT), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10)
        status = subprocess.run(["git", "-C", str(HP_ROOT), "status", "--porcelain"], capture_output=True,
                                text=True, timeout=10)
        c = commit.stdout.strip() if commit.returncode == 0 else None
        d = bool(status.stdout.strip()) if status.returncode == 0 else None
        return c, d
    except Exception:
        return None, None


def process_provenance() -> dict[str, Any]:
    """Code hash / git state of THIS process (recorded per call and per invocation)."""
    commit, dirty = git_state()
    return {"hp_code_hash": code_hash(), "hp_git_commit": commit, "hp_git_dirty": dirty}


# ---------------------------------------------------------------- manifest

@dataclass
class Item:
    item_key: str
    env_id: str
    condition: str
    model_id: str
    sample_idx: int
    rendered: Rendered
    params: dict[str, Any]
    spec: ModelSpec = field(repr=False, default=None)   # resolved per item (never looked up by id later)

    def request_body(self) -> dict:
        """The exact JSON body this item posts (no secrets: keys travel in headers only)."""
        return build_body(self.spec, self.rendered.messages, self.params)[1]

    def expected(self) -> dict[str, Any]:
        """The frozen spec of this item (manifest `items` entry): identity, rendered-messages hash, the sampling
        params, the model block and the sha256 of the request body. A ledger record must match all of it
        (Codex round 2, #6: validation no longer trusts self-reported fields)."""
        return {"env_id": self.env_id, "condition": self.condition, "model_id": self.model_id,
                "sample_idx": self.sample_idx, "messages_sha256": self.rendered.prompt_hashes["messages"],
                "params": dict(self.params), "model": self.spec.info().model_dump(mode="json"),
                "request_body_sha256": body_sha256(self.request_body())}


def item_key(env_id: str, condition: str, model_id: str, sample_idx: int) -> str:
    for part in (env_id, condition, model_id):
        if KEY_SEP in part:
            raise RunnerError(f"id {part!r} contains the key separator {KEY_SEP!r}")
    return KEY_SEP.join((env_id, condition, model_id, str(sample_idx)))


def env_meta(cfg: RunConfig, env: EnvCfg) -> tuple[str, str]:
    repo, branch = env.repo, env.branch
    meta_file = Path(cfg.envs_dir) / env.id / "env.yaml"
    if meta_file.exists():
        m = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        repo = m.get("repo") or repo        # env.yaml (shipped by the env builder) wins over the config
        branch = m.get("branch") or branch
    if not repo or not branch:
        raise RunnerError(f"env {env.id}: repo/branch must be given in the config or in {meta_file}")
    return repo, branch


def missing_packets(cfg: RunConfig) -> list[str]:
    return [str(Path(cfg.envs_dir) / e.id / "packet.md") for e in cfg.envs
            if not (Path(cfg.envs_dir) / e.id / "packet.md").exists()]


def build_items(cfg: RunConfig, reg: Registry) -> tuple[list[Item], dict[str, Any]]:
    """Full manifest in deterministic shuffled order, plus per-env packet provenance.
    Raises RunnerError on invalid ids, missing packets, or a missing {target_model_family} placeholder."""
    validate_config(cfg)
    for c in cfg.conditions:
        parse_condition(c, reg)
    missing = missing_packets(cfg)
    if missing:
        raise RunnerError("missing packet.md for %d env(s) (environment not built yet?):\n  - %s"
                          % (len(missing), "\n  - ".join(missing)))
    packets: dict[str, dict] = {}
    rendered_cache: dict[tuple, Rendered] = {}
    items: list[Item] = []
    specs = {m.id: m.spec() for m in cfg.models}
    for env in cfg.envs:
        repo, branch = env_meta(cfg, env)
        packet = read_packet(Path(cfg.envs_dir), env.id)
        has_slot = PACKET_PLACEHOLDER in packet
        if not has_slot and not env.allow_missing_family_placeholder:
            raise RunnerError(f"env {env.id}: packet.md has no {PACKET_PLACEHOLDER} placeholder (BUILD_SPEC "
                              f"self-relevance, DESIGN E5). Fix the packet, or set "
                              f"`allow_missing_family_placeholder: true` on this env in the config to override.")
        packets[env.id] = {"repo": repo, "branch": branch, "packet_sha256": sha256_text(packet),
                           "packet_chars": len(packet), "eval_cue_hits": eval_cue_hits(packet),
                           "has_family_placeholder": has_slot,
                           "allow_missing_family_placeholder": env.allow_missing_family_placeholder}
        for cond in cfg.conditions:
            for m in cfg.models:
                key = (env.id, cond, m.family)
                if key not in rendered_cache:
                    rendered_cache[key] = render(reg, condition=cond, packet_template=packet, repo=repo,
                                                 branch=branch, target_model_family=m.family,
                                                 sys_id=cfg.system_prompt_id, task_id=cfg.task_prompt_id)
                r = rendered_cache[key]
                for i in range(cfg.n_samples):
                    params = {**default_params(m.backend), **m.params}   # backend defaults are in params, not hidden in the body
                    if cfg.seed_from_sample_idx and m.backend in ("vllm", "openrouter"):
                        params.setdefault("seed", i)
                    items.append(Item(item_key(env.id, cond, m.id, i), env.id, cond, m.id, i, r, params,
                                      spec=specs[m.id]))
    keys = [it.item_key for it in items]
    if len(set(keys)) != len(keys):
        raise RunnerError(f"duplicate item keys: {_dupes(keys)[:5]}")
    random.Random(cfg.order_seed).shuffle(items)
    return items, packets


def used_prompt_ids(cfg: RunConfig, reg: Registry) -> list[str]:
    ids = {cfg.system_prompt_id, cfg.task_prompt_id}
    for c in cfg.conditions:
        ids.update(x for x in parse_condition(c, reg) if x)
    return sorted(ids)


def manifest_dict(cfg: RunConfig, cfg_sha: str, cfg_raw: dict, reg: Registry, packets: dict,
                  items: list[Item]) -> dict:
    """`items` is the FULL grid (never a filtered selection): n_items and the per-item hashes are frozen."""
    return {
        "run_id": cfg.run_id, "created": utcnow(), "config_sha256": cfg_sha, "config": cfg_raw,
        "config_resolved": cfg.model_dump(mode="json"),
        "prompt_registry_sha256": reg.hashes(),
        "prompt_hashes_used": {pid: reg.get(pid).sha256 for pid in used_prompt_ids(cfg, reg)}, "packets": packets,
        "model_families": {m.id: m.family for m in cfg.models},
        "items": {it.item_key: it.expected() for it in sorted(items, key=lambda i: i.item_key)},
        **process_provenance(),
        "n_items": len(items), "python": sys.version,
        "invocations": [],            # one entry per non-dry invocation: filters used, counts (reconciled below)
        "totals": None,               # reconciled against the ledger on every invocation
    }


def frozen_view(m: dict) -> dict:
    """The experimental inputs that may not change within a run_id: config, used prompts, each packet's
    hash + resolved repo/branch, each model's target_model_family, and every item's rendered-messages
    hash. The runner code hash is recorded per record and per invocation but not frozen, so a runner
    bugfix can resume a long run."""
    return {"config_sha256": m.get("config_sha256"), "prompt_hashes_used": m.get("prompt_hashes_used"),
            "packets": {e: {k: p.get(k) for k in ("packet_sha256", "repo", "branch")}
                        for e, p in (m.get("packets") or {}).items()},
            "model_families": m.get("model_families"),
            "items": dict(m.get("items") or {})}   # whole per-item spec: hashes, params, model block, body hash


def frozen_diff(old: dict, new: dict) -> list[str]:
    """Human-readable list of what changed between two frozen views (item-level for `items`)."""
    a, b = frozen_view(old), frozen_view(new)
    out = []
    for k in a:
        if a[k] == b[k]:
            continue
        if isinstance(a[k], dict) and isinstance(b[k], dict):
            ch = sorted(set(a[k]) ^ set(b[k]) | {x for x in a[k] if x in b[k] and a[k][x] != b[k][x]})
            out.append(f"{k}: {len(ch)} changed, e.g. {ch[:3]}")
        else:
            out.append(f"{k}: {a[k]} -> {b[k]}")
    return out


def run_secrets(cfg: RunConfig) -> list[str]:
    """Every secret this run could touch: the API key of each configured model (read from the env now)."""
    out: list[str] = []
    for m in cfg.models:
        try:
            for s in m.spec().secrets():
                if s not in out:
                    out.append(s)
        except ValueError:
            pass
    return out


def dump_redacted(obj: Any, secrets: list[str], **kw) -> str:
    """THE choke point for everything the runner persists (ledger lines, manifest, invocations): JSON text with
    every configured secret replaced by [REDACTED:api_key], ensure_ascii=False. Nothing else writes JSON."""
    return json.dumps(redact(obj, secrets), ensure_ascii=False, **kw)


def write_manifest(path: Path, manifest: dict, secrets: list[str]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(dump_redacted(manifest, secrets, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: Any, secrets: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(dump_redacted(obj, secrets) + "\n")


def check_secrets_in_requests(items: list[Item], secrets: list[str], packets: dict | None = None) -> None:
    """Refuse a run whose rendered messages / params / extra / provider / model url / packet repo-branch contain a
    configured API key: stored redacted, the frozen item spec or body hash could never match on resume, so the
    run would be bricked (and a prompt that contains a key is a config error anyway). Raises RunnerError."""
    packets = packets or {}
    if not secrets:
        return
    bad = [it.item_key for it in items if contains_secret(it.expected(), secrets) or contains_secret(it.request_body(), secrets)]
    if bad:
        raise RunnerError(f"{len(bad)} item(s) have a request body or frozen item spec (messages/params/extra/provider/"
                          f"model url) containing the value of a configured API key env var, e.g. {bad[:3]}. Remove the "
                          f"key from the prompt/config; it must only ever travel in the request headers.")
    if contains_secret(packets, secrets):
        raise RunnerError("a packet's frozen provenance (repo/branch) contains the value of a configured API key env var; "
                          "it could only be stored redacted and resume would be refused forever. Remove it.")


# ---------------------------------------------------------------- resume / ledger validation

@dataclass
class Ledger:
    done: set[str] = field(default_factory=set)          # item_keys with >= 1 VALID ok record
    attempts: dict[str, int] = field(default_factory=dict)  # max attempt number per item_key (any line with a key)
    bad: list[dict] = field(default_factory=list)        # invalid lines: {"line", "item_key", "reason"}; ignored
    duplicates: dict[str, int] = field(default_factory=dict)  # item_key -> number of valid ok records, when > 1
    duplicate_attempts: list[dict] = field(default_factory=list)  # valid lines reusing an (item_key, attempt) pair
    n_lines: int = 0

    def summary(self) -> dict:
        return {"n_lines": self.n_lines, "n_done": len(self.done), "n_bad": len(self.bad),
                "n_duplicate_ok_items": len(self.duplicates), "n_duplicate_attempts": len(self.duplicate_attempts),
                "bad": self.bad[:50], "duplicates": dict(sorted(self.duplicates.items())[:50]),
                "duplicate_attempts": self.duplicate_attempts[:50]}


def messages_sha256(messages: list[dict]) -> str | None:
    """The same hash `render()` puts in prompt_hashes['messages'], recomputed from a record's stored messages."""
    try:
        return hashlib.sha256(
            ("\x00".join(f"{m['role']}\x01{m['content']}" for m in messages)).encode("utf-8")).hexdigest()
    except (KeyError, TypeError):
        return None


def validate_record(r: dict, *, expected: dict[str, dict] | None, run_id: str | None,
                    config_sha256: str | None) -> str | None:
    """Return a reason string if the record is not a valid record of this run's manifest, else None."""
    return validate_record_model(r, expected=expected, run_id=run_id, config_sha256=config_sha256)[1]


def validate_record_model(r: dict, *, expected: dict[str, dict] | None, run_id: str | None,
                          config_sha256: str | None) -> tuple[Record | None, str | None]:
    """(validated Record | None, reason | None). Strict pydantic validation (no `ok: 1` / `"2"` coercion), so
    the caller works with the typed model, never with the raw dict.

    Nothing self-reported is trusted (Codex round 2, #6): schema version and attempt are checked, the
    rendered-messages hash is RECOMPUTED from the stored `messages`, the model block / params / request body
    are compared with the manifest's item spec, and an ok record must carry a usable completion (non-empty
    content or reasoning, a finish_reason, no error)."""
    try:
        rec = Record.model_validate(r, strict=True)
    except ValidationError as e:
        errs = e.errors()[:3]
        return None, "schema: " + "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in errs)
    reason = _record_reason(rec, expected=expected, run_id=run_id, config_sha256=config_sha256)
    return (None, reason) if reason else (rec, None)


def _record_reason(rec: Record, *, expected: dict[str, dict] | None, run_id: str | None,
                   config_sha256: str | None) -> str | None:
    if rec.schema_version != SCHEMA_VERSION:
        return f"schema_version {rec.schema_version} != {SCHEMA_VERSION}"
    if rec.attempt < 1:
        return f"attempt {rec.attempt} < 1"
    if run_id is not None and rec.run_id != run_id:
        return f"run_id {rec.run_id!r} != {run_id!r}"
    if config_sha256 is not None and rec.config_sha256 != config_sha256:
        return f"config_sha256 {rec.config_sha256[:12]} != manifest {config_sha256[:12]}"
    # internal consistency (independent of the manifest)
    recomputed = messages_sha256(rec.messages)
    if recomputed is None:
        return "messages are not role/content objects"
    if rec.prompt_hashes.get("messages") != recomputed:
        return "prompt_hashes.messages != sha256 of the stored messages (self-reported hash is wrong)"
    if rec.request_body and rec.request_body.get("messages") not in (rec.messages, [m for m in rec.messages if m.get("role") != "system"]):
        return "request_body.messages != messages"
    if rec.ok:
        if rec.error is not None:
            return "ok record carries an error"
        if not rec.request_body:
            return "ok record has an empty request_body"
        has_content = isinstance(rec.content, str) and rec.content.strip() != ""
        has_reasoning = isinstance(rec.reasoning, str) and rec.reasoning.strip() != ""
        if not has_content and not has_reasoning and rec.reasoning_source != "redacted_thinking":
            return "ok record has empty content and no reasoning"
        if not isinstance(rec.finish_reason, str) or not rec.finish_reason:
            return "ok record has no finish_reason"
        if rec.truncated != (rec.finish_reason in ("length", "max_tokens")):
            return f"truncated={rec.truncated} disagrees with finish_reason {rec.finish_reason!r}"
        if rec.request_body.get("return_token_ids") and rec.model.backend != "anthropic":
            for f_ in ("prompt_token_ids", "completion_token_ids"):
                if not getattr(rec, f_):
                    return f"ok record has no {f_} although return_token_ids was requested"
    elif rec.error is None:
        return "failed record has no error"
    if expected is None:
        return None
    exp = expected.get(rec.item_key)
    if exp is None:
        return "item_key not in manifest"
    for f_, got in (("env_id", rec.env_id), ("condition", rec.condition), ("sample_idx", rec.sample_idx),
                    ("model_id", rec.model.id)):
        if exp[f_] != got:
            return f"{f_} {got!r} != manifest {exp[f_]!r}"
    if recomputed != exp["messages_sha256"]:
        return "sha256 of the stored messages != manifest (rendered input differs)"
    if "model" in exp and rec.model.model_dump(mode="json") != exp["model"]:
        diff = sorted(k for k in exp["model"] if rec.model.model_dump(mode="json").get(k) != exp["model"][k])
        return f"model block != manifest (fields {diff})"
    if "params" in exp and rec.params != exp["params"]:
        return f"params {rec.params} != manifest {exp['params']}"
    if "request_body_sha256" in exp and rec.request_body and body_sha256(rec.request_body) != exp["request_body_sha256"]:
        return "request_body != manifest (sha256 of the stored body differs from the item's spec)"
    return None


def read_records(path: Path, *, expected: dict[str, dict] | None = None, run_id: str | None = None,
                 config_sha256: str | None = None) -> Ledger:
    """Scan the ledger. Every line is validated (see `validate_record`: schema, version, attempt, recomputed
    messages hash, success semantics) and, when `expected` (manifest items) is given, checked against the
    manifest (run_id, config hash, env/condition/sample/model, messages hash, model block, params, body hash).
    Invalid lines are reported by line number and item and never count as done (the item is redone)."""
    led = Ledger()
    if not path.exists():
        return led
    ok_count: Counter = Counter()
    seen_attempts: set[tuple[str, int]] = set()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            led.n_lines += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                led.bad.append({"line": n, "item_key": None, "reason": f"unparsable JSON ({e.msg}); "
                                "partial line after a crash?"})
                continue
            k = r.get("item_key") if isinstance(r, dict) else None
            if not isinstance(k, str) or not k:
                led.bad.append({"line": n, "item_key": None, "reason": "no item_key"})
                continue
            try:
                att = int(r.get("attempt", 0))
            except (TypeError, ValueError):
                att = 0
            led.attempts[k] = max(led.attempts.get(k, 0), att)   # counted even if invalid: never reuse a number
            rec, reason = validate_record_model(r, expected=expected, run_id=run_id, config_sha256=config_sha256)
            if reason or rec is None:
                led.bad.append({"line": n, "item_key": k, "reason": reason})
                continue
            att = rec.attempt                  # the typed value, not the raw dict's
            if (k, att) in seen_attempts:      # two processes wrote the same attempt: valid data, but reported
                led.duplicate_attempts.append({"line": n, "item_key": k, "attempt": att})
            seen_attempts.add((k, att))
            if rec.ok:
                ok_count[k] += 1
                led.done.add(k)
    led.duplicates = {k: c for k, c in ok_count.items() if c > 1}
    return led


# ---------------------------------------------------------------- run lock

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_LOCK_FDS: dict[Path, int] = {}   # path -> fd held open (flock lives as long as the fd / the process)


def acquire_lock(run_dir: Path, *, argv: list[str] | None = None, secrets: list[str] | None = None) -> Path:
    """Exclusive lock `<run_dir>/lock` holding {pid, host, started, argv}.

    The file is opened (created if absent) and `flock(LOCK_EX | LOCK_NB)` is taken on it; the fd stays open
    for the life of the process, so a crashed holder releases the flock automatically (no stale-pid
    unlink/recreate race). If the flock is free but the file names a holder, the holder is a process that is
    not flocking (another host, or it died before flock existed): it is refused unless it is a dead pid on
    this host. Refusal raises RunnerError."""
    path = run_dir / LOCK_FILE
    me = {"pid": os.getpid(), "host": socket.gethostname(), "started": utcnow(), "argv": argv or sys.argv}
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = _read_holder(path)
            raise RunnerError(_locked_msg(run_dir, path, holder))
        holder = _read_holder(path)
        if holder:
            same_host = holder.get("host") == me["host"]
            pid = holder.get("pid")
            if same_host and isinstance(pid, int) and pid != me["pid"] and not _pid_alive(pid):
                log.warning("stale lock %s (pid %s on %s is dead); taking it over", path, pid, holder.get("host"))
            else:   # live pid on this host, our own pid (re-entry), or another host: never taken over
                raise RunnerError(_locked_msg(run_dir, path, holder))
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (dump_redacted(me, list(secrets or [])) + "\n").encode("utf-8"))   # argv may carry a key
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    _LOCK_FDS[path] = fd
    return path


def _read_holder(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _locked_msg(run_dir: Path, path: Path, holder: dict) -> str:
    return (f"run {run_dir.name} is locked by pid {holder.get('pid')} on host {holder.get('host')} since "
            f"{holder.get('started')} ({path}). A second process on the same run_id would issue every pending "
            f"sample twice. Wait for it, or delete the lock if that process is gone.")


def release_lock(path: Path) -> None:
    fd = _LOCK_FDS.pop(path, None)
    if fd is None:
        return
    try:
        if _read_holder(path).get("pid") == os.getpid():
            path.unlink()
    except Exception:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


# ---------------------------------------------------------------- execution

def repair_trailing_newline(path: Path) -> int | None:
    """If the ledger exists and does not end with "\\n" (a crash cut the last line), append "\\n" so the next
    record starts on its own line, and return the 1-based number of the truncated line (None if nothing to
    do). Without this every later record would be glued onto the fragment and ignored forever (Codex round 2,
    new bug 1)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return None
        f.seek(0)
        n_lines = f.read().count(b"\n") + 1
    with open(path, "ab") as f:
        f.write(b"\n")
    return n_lines


class Writer:
    """Append-only ledger writer. Writes are synchronous (no await inside), so they are atomic with respect
    to the event loop; a failed write is logged with the full record and mirrored to records.unwritten.jsonl.
    Every line goes through `dump_redacted` (secrets scrubbed). On open, a partial trailing line (crash) is
    terminated with "\\n" and reported by line number (`repaired_line`)."""

    def __init__(self, path: Path, secrets: list[str] | None = None):
        self.path = path
        self.secrets = list(secrets or [])
        self.fallback = path.with_name("records.unwritten.jsonl")
        self.repaired_line = repair_trailing_newline(path)
        if self.repaired_line is not None:
            log.warning("ledger %s ended with a line %d without a newline (crash mid-write?): newline appended so "
                        "new records start on their own line; line %d counts only if it validated on this start",
                        path, self.repaired_line, self.repaired_line)
        self.f = open(path, "a", encoding="utf-8")
        self.n = 0
        self.n_failed = 0

    def write(self, rec: Record) -> bool:
        line = dump_redacted(rec.model_dump(mode="json"), self.secrets)
        try:
            self.f.write(line + "\n")
            self.f.flush()
            self.n += 1
            return True
        except Exception as e:
            self.n_failed += 1
            log.error("ledger write failed for %s attempt=%d: %r. Record follows.\n%s", rec.item_key, rec.attempt,
                      e, line)
            try:
                with open(self.fallback, "a", encoding="utf-8") as fb:
                    fb.write(line + "\n")
                log.error("record mirrored to %s", self.fallback)
            except Exception as e2:
                log.error("fallback write also failed: %r", e2)
            return False

    def close(self):
        try:
            self.f.close()
        except Exception as e:
            log.error("closing the ledger failed: %r", e)


async def run_item(it: Item, cfg: RunConfig, base_meta: dict, start_attempt: int, sem: asyncio.Semaphore,
                   client, writer: Writer, pbar, server_defaults: dict | None) -> bool:
    spec = it.spec
    attempt = start_attempt
    for _ in range(cfg.max_attempts):
        attempt += 1
        meta = dict(base_meta, item_key=it.item_key, env_id=it.env_id, condition=it.condition,
                    sample_idx=it.sample_idx, attempt=attempt, prompt_ids=it.rendered.prompt_ids,
                    prompt_hashes=it.rendered.prompt_hashes)
        async with sem:                       # cancelled while queued here -> no request issued, no record
            started, t0 = utcnow(), time.monotonic()
            try:
                body = it.request_body()      # the body generate() posts (same builder), for the Cancelled record
            except Exception:
                body = None
            try:
                rec = await generate(spec, it.rendered.messages, it.params, meta=meta, client=client,
                                     server_defaults=server_defaults)
            except asyncio.CancelledError:
                # The invocation is being cancelled (Ctrl-C / SIGTERM) with this request in flight: leave an
                # attempt record (with the body that was posted) so the ledger shows it was issued, then propagate.
                writer.write(failed_record(spec, it.rendered.messages, it.params, meta=meta, exc_type="Cancelled",
                                           message="invocation cancelled while the request was in flight",
                                           retryable=True, started=started, latency_s=time.monotonic() - t0,
                                           request_body=body, server_defaults=server_defaults))
                raise
        written = writer.write(rec)
        if rec.ok and written:
            u = rec.usage or {}
            log.info("ok %s attempt=%d finish=%s prompt_tok=%s compl_tok=%s latency=%.1fs reasoning_chars=%d",
                     it.item_key, attempt, rec.finish_reason, u.get("prompt_tokens") or u.get("input_tokens"),
                     u.get("completion_tokens") or u.get("output_tokens"), rec.latency_s,
                     len(rec.reasoning or ""))
            pbar.update(1)
            return True
        if rec.ok and not written:
            log.error("ok response for %s attempt=%d could NOT be written to the ledger; the item stays pending",
                      it.item_key, attempt)
            break
        log.warning("fail %s attempt=%d %s: %s", it.item_key, attempt, rec.error.type, rec.error.message[:300])
        if not rec.error.retryable or attempt - start_attempt >= cfg.max_attempts:
            break
        await asyncio.sleep(min(cfg.backoff_s * 2 ** (attempt - start_attempt - 1), 120.0))
    pbar.update(1)
    return False


def _filters_dict(filters: dict | None, limit: int | None) -> dict:
    f = {k: (sorted(v) if isinstance(v, (set, list, tuple)) else v) for k, v in (filters or {}).items() if v}
    f["limit"] = limit
    return f


async def run(cfg: RunConfig, cfg_sha: str, cfg_raw: dict, items: list[Item], selected: list[Item], packets: dict, *,
              run_dir: Path, dry_run: bool, limit: int | None, filters: dict | None = None,
              make_client: Callable[[ModelSpec], Any] | None = None) -> dict:
    """`items` is the full grid (frozen in the manifest); `selected` is this invocation's subset."""
    reg = Registry()
    url_conc = validate_config(cfg)
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise RunnerError(f"--limit must be an integer >= 0 (got {limit!r})")
    secrets = run_secrets(cfg)                  # scrubbed from everything written below
    check_secrets_in_requests(items, secrets, packets)
    make_client = make_client or (lambda spec: spec.make_client())
    man_path, rec_path, inv_path = run_dir / "manifest.json", run_dir / "records.jsonl", run_dir / "invocations.jsonl"
    manifest = manifest_dict(cfg, cfg_sha, cfg_raw, reg, packets, items)
    expected = manifest["items"]
    proc = process_provenance()
    lock: Path | None = None
    sig_installed = False
    if not dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)
        lock = acquire_lock(run_dir, secrets=secrets)
        try:    # SIGTERM (Slurm timeout / scancel) cancels like Ctrl-C: in-flight attempts get Cancelled records
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
            sig_installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        if man_path.exists():
            old = json.loads(man_path.read_text(encoding="utf-8"))
            diff = frozen_diff(old, manifest)
            if diff:
                raise RunnerError(f"run {cfg.run_id}: inputs changed since the manifest was frozen ({man_path}):\n  - "
                                  + "\n  - ".join(diff) + "\nUse a new run_id.")
            if old.get("hp_code_hash") != proc["hp_code_hash"]:
                log.warning("runner code changed since the manifest was frozen (hp_code_hash %s -> %s); inputs are "
                            "unchanged, continuing. Each record carries the code hash of the process that made "
                            "the call; the manifest keeps the original.",
                            str(old.get("hp_code_hash"))[:12], proc["hp_code_hash"][:12])
            manifest = old
            manifest.setdefault("invocations", []); manifest.setdefault("totals", None)
        elif not dry_run:
            write_manifest(man_path, manifest, secrets)

        led = read_records(rec_path, expected=expected, run_id=cfg.run_id, config_sha256=cfg_sha)
        for b in led.bad:
            log.warning("ledger %s line %d (%s): %s -> ignored, item will be redone if no valid ok record exists",
                        rec_path.name, b["line"], b["item_key"], b["reason"])
        for k, c in sorted(led.duplicates.items()):
            log.warning("ledger: item %s has %d ok records (duplicate success; analysis must dedupe by first ok)", k, c)
        for dmp in led.duplicate_attempts:
            log.warning("ledger line %d: item %s attempt %d appears more than once (two writers on one run_id?)",
                        dmp["line"], dmp["item_key"], dmp["attempt"])
        todo = [it for it in selected if it.item_key not in led.done]
        n_pending_all = len(todo)
        if limit is not None:
            todo = todo[:limit]
        log.info("run %s: %d items in grid, %d selected, %d done, %d to do%s, %d invalid ledger lines, "
                 "%d items with duplicate ok records", cfg.run_id, len(items), len(selected), len(led.done),
                 len(todo), f" (limit {limit})" if limit is not None else "", len(led.bad), len(led.duplicates))

        clients: dict[str, Any] = {}
        servers: dict[str, Any] = {}
        server_defaults: dict[str, dict | None] = {}
        try:
            for m in cfg.models:
                mid = m.id
                if not any(it.model_id == mid for it in todo):
                    continue
                spec = next(it.spec for it in todo if it.model_id == mid)
                clients[mid] = make_client(spec)
                servers[mid] = {"models": await probe_models(spec, clients[mid])}
                server_defaults[mid] = None
                if spec.render_probe and servers[mid]["models"]["ok"]:
                    first = next(it for it in todo if it.model_id == mid)
                    rp = await render_probe(spec, first.rendered.messages, first.params, clients[mid])
                    servers[mid]["render_probe"] = {"item_key": first.item_key, **rp}
                    server_defaults[mid] = server_defaults_from_render(rp, first.params)
                if not servers[mid]["models"]["ok"]:
                    log.warning("model %s: server %s not reachable (%s); skipping its %d items this invocation",
                                mid, spec.url, servers[mid]["models"].get("error") or servers[mid]["models"].get("status"),
                                sum(it.model_id == mid for it in todo))
            runnable = [it for it in todo if servers.get(it.model_id, {}).get("models", {}).get("ok")]
            inv = {"started": utcnow(), "dry_run": dry_run, "n_grid": len(items), "n_selected": len(selected),
                   "n_todo": len(todo), "n_runnable": len(runnable), "filters": _filters_dict(filters, limit),
                   "ledger": led.summary(), "servers": servers, "argv": sys.argv, **proc,
                   "pid": os.getpid(), "host": socket.gethostname()}
            if dry_run:
                return {"manifest": manifest, "invocation": inv, "todo": todo, "runnable": runnable, "ledger": led}

            # Filtered invocation: store the filter in the manifest; reconcile totals against the ledger.
            manifest["invocations"].append({"started": inv["started"], "filters": inv["filters"],
                                            "n_selected": len(selected), "n_runnable": len(runnable),
                                            "pid": inv["pid"], "host": inv["host"], **proc})
            manifest["totals"] = {"n_items": len(items), "n_done": len(led.done),
                                  "n_pending": len(items) - len(led.done), "updated": inv["started"]}
            write_manifest(man_path, manifest, secrets)
            append_jsonl(inv_path, inv, secrets)

            base_meta = {"run_id": cfg.run_id, "config_sha256": cfg_sha, **proc}   # provenance of THIS process
            sems = {url: asyncio.Semaphore(c) for url, c in url_conc.items()}
            for url, c in url_conc.items():
                log.info("server %s: concurrency %d (models %s)", url, c, [m.id for m in cfg.models if m.spec().url == url])
            writer = Writer(rec_path, secrets)
            pbar = tqdm(total=len(runnable), desc=cfg.run_id, unit="item", file=sys.stderr)
            try:
                results = await asyncio.gather(*[
                    run_item(it, cfg, base_meta, led.attempts.get(it.item_key, 0), sems[it.spec.url],
                             clients[it.model_id], writer, pbar, server_defaults.get(it.model_id))
                    for it in runnable], return_exceptions=True)
            finally:
                pbar.close(); writer.close()
            crashed = [(it.item_key, r) for it, r in zip(runnable, results) if isinstance(r, BaseException)]
            for key, exc in crashed:
                log.error("item %s crashed outside generate(): %r", key, exc)
            n_ok = sum(1 for r in results if r is True)
            led_after = read_records(rec_path, expected=expected, run_id=cfg.run_id, config_sha256=cfg_sha)
            summary = {"finished": utcnow(), "n_runnable": len(runnable), "n_ok": n_ok, "n_failed": len(runnable) - n_ok,
                       "n_crashed": len(crashed), "n_pending_not_run": n_pending_all - len(runnable),
                       "records_written": writer.n, "records_unwritten": writer.n_failed,
                       "ledger_repaired_line": writer.repaired_line,
                       "n_done_total": len(led_after.done), "n_items_total": len(items)}
            manifest["totals"] = {"n_items": len(items), "n_done": len(led_after.done),
                                  "n_pending": len(items) - len(led_after.done), "updated": summary["finished"]}
            write_manifest(man_path, manifest, secrets)
            append_jsonl(inv_path, {"run_id": cfg.run_id, **summary}, secrets)
            log.info("done: %s", summary)
            return {"manifest": manifest, "invocation": inv, "summary": summary, "ledger": led_after}
        finally:
            for c in clients.values():
                await c.aclose()
    finally:
        if sig_installed:
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
            except Exception:
                pass
        if lock is not None:
            release_lock(lock)


# ---------------------------------------------------------------- CLI

def _filter(items: list[Item], models: str | None, envs: str | None, conditions: str | None) -> tuple[list[Item], dict]:
    """Apply --models/--envs/--conditions. A value that matches no id is refused (a typo must not exit 0)."""
    filters: dict[str, list[str]] = {}
    for name, raw, attr in (("models", models, "model_id"), ("envs", envs, "env_id"), ("conditions", conditions, "condition")):
        if not raw:
            continue
        keep = set(raw.split(","))
        known = {getattr(i, attr) for i in items}
        unknown = sorted(keep - known)
        if unknown:
            raise RunnerError(f"--{name}: {unknown} match no {name[:-1]} id in the config (known: {sorted(known)})")
        items = [i for i in items if getattr(i, attr) in keep]
        filters[name] = sorted(keep)
    return items, filters


def _setup_logging(run_id: str, log_dir: Path, dry_run: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if not dry_run:                      # --dry-run writes nothing, not even a log file
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_dir / f"{run_id}.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--run-id", help="override config run_id")
    ap.add_argument("--dry-run", action="store_true", help="build manifest, probe servers, print first request; write nothing")
    ap.add_argument("--limit", type=int, help="run at most N not-yet-done items (smoke tests)")
    ap.add_argument("--models", help="comma-separated model ids to include")
    ap.add_argument("--envs", help="comma-separated env ids to include")
    ap.add_argument("--conditions", help="comma-separated conditions to include")
    ap.add_argument("--log-dir", default=str(LOG_DIR))
    a = ap.parse_args(argv)

    try:
        cfg, cfg_sha, cfg_raw = load_config(Path(a.config))
    except (ValidationError, yaml.YAMLError, OSError) as e:
        print(f"error: cannot load config {a.config}: {e}", file=sys.stderr)
        return 3
    if a.run_id:
        cfg.run_id = a.run_id
    if a.limit is not None and a.limit < 0:
        print(f"error: --limit must be >= 0 (got {a.limit}); a negative value would slice the todo list, not limit it",
              file=sys.stderr)
        return 3
    _setup_logging(cfg.run_id, Path(a.log_dir), a.dry_run)
    try:
        reg = Registry()
        reg.verify_all()
        items, packets = build_items(cfg, reg)
        selected, filters = _filter(items, a.models, a.envs, a.conditions)
        for env_id, p in packets.items():
            if not p["has_family_placeholder"]:
                log.warning("env %s: packet.md has no %s placeholder; allowed by config override "
                            "(allow_missing_family_placeholder)", env_id, PACKET_PLACEHOLDER)
            if p["eval_cue_hits"]:
                log.warning("env %s: eval-cue word counts in packet (advisory): %s", env_id, p["eval_cue_hits"])
        run_dir = Path(cfg.runs_dir) / cfg.run_id
        out = asyncio.run(run(cfg, cfg_sha, cfg_raw, items, selected, packets, run_dir=run_dir, dry_run=a.dry_run,
                              limit=a.limit, filters=filters))
    except RunnerError as e:
        log.error("refused: %s", e)
        print(f"error: {e}", file=sys.stderr)
        return 3
    if a.dry_run:
        todo, runnable, led = out["todo"], out["runnable"], out["ledger"]
        print(f"[dry-run] run_id={cfg.run_id} grid={len(items)} selected={len(selected)} done={len(led.done)} "
              f"todo={len(todo)} runnable_now={len(runnable)} invalid_ledger_lines={len(led.bad)} "
              f"duplicate_ok_items={len(led.duplicates)}")
        lock = run_dir / LOCK_FILE
        if lock.exists():
            print(f"[dry-run] NOTE: lock present: {lock.read_text(encoding='utf-8').strip()}")
        for name, keyf in (("model", lambda i: i.model_id), ("env", lambda i: i.env_id), ("condition", lambda i: i.condition)):
            print(f"[dry-run] by {name}: {dict(Counter(keyf(i) for i in todo))}")
        print("[dry-run] servers:", json.dumps({m: s["models"].get("ok") for m, s in out["invocation"]["servers"].items()}))
        if todo:
            it = todo[0]
            try:
                url, body = build_body(it.spec, it.rendered.messages, it.params)
                shown = dict(body, messages=[{**m, "content": m["content"][:400] + ("…" if len(m["content"]) > 400 else "")}
                                             for m in body.get("messages", [])])
                print(f"[dry-run] first item {it.item_key} -> POST {url}\n"
                      + dump_redacted(shown, run_secrets(cfg), indent=1))
                print("[dry-run] prompt_hashes:", json.dumps(it.rendered.prompt_hashes, indent=1))
            except Exception as e:
                print(f"[dry-run] first item {it.item_key}: cannot build request: {e}")
        return 0
    s = out["summary"]
    if s["n_failed"]:
        return 1            # some attempted items have no ok record yet: re-run to retry
    if s["n_pending_not_run"]:
        return 2            # items skipped (server down / --limit): re-run when the server is up
    return 0


if __name__ == "__main__":
    sys.exit(main())
