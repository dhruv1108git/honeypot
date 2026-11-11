"""Batch runner for the agentic pilot. Materialize -> run_episode -> append trajectory. Resumable by item_key.

Usage:
  python -m agent.runner configs/exp_goalloc_main.yaml [--limit N]
Then serialize to the rater-view and rate as usual:
  python -m agent.serialize runs/exp_goalloc_main
  python analysis/rate.py runs/exp_goalloc_main --rater glm53   (etc.)
  python analysis/consensus.py runs/exp_goalloc_main > runs/exp_goalloc_main/consensus_summary.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hp.backends import ModelSpec  # noqa: E402

from .loop import run_episode
from .materialize import materialize_worktree

MEMO_DIR = Path(__file__).resolve().parent / "memos"


def _memo_text(name: str | None) -> str | None:
    """Resolve an arm's `memo` field: None, or a file name under agent/memos/ (without extension ok)."""
    if not name:
        return None
    p = MEMO_DIR / name
    if not p.exists() and not name.endswith(".md"):
        p = MEMO_DIR / f"{name}.md"
    return p.read_text(encoding="utf-8")


def build_specs(models: list[dict]) -> dict[str, ModelSpec]:
    out = {}
    for m in models:
        out[m["id"]] = ModelSpec(
            id=m["id"], backend=m["backend"], model=m["model"], family=m["family"], url=m.get("url"),
            reasoning_visibility=m.get("reasoning_visibility", "raw"), concurrency=m.get("concurrency", 8),
            timeout_s=m.get("timeout_s", 3600.0), params=m.get("params", {}), extra={})  # no return_token_ids per turn
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

    runs_dir = Path(cfg["runs_dir"])
    if not runs_dir.is_absolute():  # resolve relative to the repo root so the runner is cwd-independent
        runs_dir = Path(__file__).resolve().parent.parent / runs_dir
    run_dir = runs_dir / cfg["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    traj_path = run_dir / "trajectories.jsonl"
    done = set()
    if traj_path.exists():
        for line in traj_path.open():
            try:
                t = json.loads(line)
                if t.get("finish") != "error":   # retry persistent-error episodes on resume
                    done.add(t["item_key"])
            except Exception:
                pass

    specs = build_specs(cfg["models"])
    # build grid
    items = []
    for env in cfg["envs"]:
        for cond in cfg["conditions"]:
            for mid, spec in specs.items():
                for s in range(cfg["n_samples"]):
                    key = f"{env['id']}|{cond}|{mid}|{s}"
                    if key in done:
                        continue
                    items.append((env, cond, spec, s))
    if args.limit:
        items = items[: args.limit]
    print(f"{len(items)} episodes to run ({len(done)} already done)")

    sems = {spec.url: asyncio.Semaphore(spec.concurrency) for spec in specs.values()}
    lock = asyncio.Lock()
    # Disposable per-episode working trees. Defaults to $TMPDIR; override with AGENT_WORKTREE_DIR.
    _wt_base = os.environ.get("AGENT_WORKTREE_DIR") or None
    work_root = Path(tempfile.mkdtemp(prefix="agent_worktrees_", dir=_wt_base))
    client = httpx.AsyncClient(timeout=None)

    async def one(env, cond, spec, s):
        edir = work_root / f"{env['id']}_{cond}_{spec.id}_{s}"
        edir.mkdir(parents=True, exist_ok=True)
        try:
            memo = _memo_text(env.get("memo"))
            repo_root = await asyncio.to_thread(
                materialize_worktree, env["id"], spec.family, edir, memo, env.get("base"))
            async with sems[spec.url]:
                traj = await run_episode(
                    spec=spec, condition=cond, env_id=env["id"], arm=env.get("arm", "none"), sample_idx=s,
                    repo_root=repo_root, repo=env["repo"], branch=env["branch"], params=spec.params,
                    client=client, max_turns=cfg.get("max_turns", 12), max_tool_calls=cfg.get("max_tool_calls", 10),
                    incentive=env.get("incentive", "none"),
                    sys_id=env.get("system", "sys_v0"), task_id=env.get("task", "task_agent_v0"))
            traj.run_id = cfg["run_id"]
            async with lock:
                with traj_path.open("a", encoding="utf-8") as f:
                    f.write(traj.model_dump_json() + "\n")
            print(f"  {traj.item_key}  finish={traj.finish} turns={len(traj.turns)} "
                  f"opened={len(traj.files_opened)} diff_chars={len(traj.final_diff)}")
        except Exception as e:   # never let one episode abort the batch / race the shared client+work_root cleanup
            print(f"  FAILED {env['id']}|{cond}|{spec.id}|{s}: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(edir, ignore_errors=True)

    try:
        await asyncio.gather(*(one(*it) for it in items), return_exceptions=True)
    finally:
        await client.aclose()
        shutil.rmtree(work_root, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
