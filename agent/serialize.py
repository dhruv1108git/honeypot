"""Project agent trajectories to the single-turn rater-view so analysis/rate.py + consensus.py run unchanged.

Writes <run>/records.jsonl whose lines look like hp records to analysis.rate.parts():
  messages = [{"role":"user","content": initial task + the files the agent opened}]
  reasoning = per-turn reasoning joined
  content   = the final submission (commit message + ```diff block)
  finish_reason = "stop" (or "length" if the submit turn truncated -> dropped from rating like hp)

Usage: python -m agent.serialize runs/exp_goalloc_main
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

READ_MAX = 8000


def _final_content(traj: dict) -> str:
    for t in reversed(traj["turns"]):
        if t["action"]["tool"] == "submit":
            return t["content"] or ""
    # no explicit submit: last turn's content (may hold a diff)
    return (traj["turns"][-1]["content"] if traj["turns"] else "") or ""


def _reasoning(traj: dict) -> str:
    chunks = []
    for t in traj["turns"]:
        r = t.get("reasoning")
        if r:
            chunks.append(f"--- turn {t['index']} ---\n{r}")
    return "\n\n".join(chunks)


def _task_view(traj: dict) -> str:
    """Initial task + the contents of the files the agent chose to open (what grounds the rater)."""
    parts = [traj["initial_user"]]
    # collect EVERY successful read window, in order (a file paged over several reads keeps all its pages)
    reads = []
    for t in traj["turns"]:
        if t["action"]["tool"] == "read_file" and t["action"].get("ok") and t.get("result"):
            a = t["action"]["args"]
            p = str(a.get("path", ""))
            win = ""
            if a.get("start") is not None or a.get("end") is not None:
                win = f" (lines {a.get('start', 1)}-{a.get('end', 'end')})"
            reads.append((p + win, t["result"][:READ_MAX]))
    if reads:
        parts.append("## Files the reviewer opened")
        for label, body in reads:
            parts.append(f"### {label}\n```\n{body}\n```")
    return "\n\n".join(parts)


def rater_view(traj: dict) -> dict:
    content = _final_content(traj)
    # drop incomplete submissions from rating, matching hp (truncated finish)
    finish_reason = "length" if traj["finish"] == "truncated" else "stop"
    return {
        "run_id": traj["run_id"],
        "item_key": traj["item_key"],
        "env_id": traj["env_id"],
        "condition": traj["condition"],
        "sample_idx": traj["sample_idx"],
        "model": {"id": traj["model"]["id"], "backend": traj["model"]["backend"],
                  "url": traj["model"].get("url"), "model": traj["model"]["model"],
                  "family": traj["model"]["family"]},
        "messages": [{"role": "user", "content": _task_view(traj)}],
        "reasoning": _reasoning(traj),
        "content": content,
        "finish_reason": finish_reason,
        "ok": bool(traj["ok"]),
        "agent_finish": traj["finish"],
        "files_opened": traj["files_opened"],
        "arm": traj.get("arm", "none"),
    }


def main():
    run = Path(sys.argv[1])
    trajs = [json.loads(l) for l in (run / "trajectories.jsonl").open() if l.strip()]
    out = run / "records.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for t in trajs:
            f.write(json.dumps(rater_view(t), ensure_ascii=False) + "\n")
    print(f"wrote {len(trajs)} records -> {out}")


if __name__ == "__main__":
    main()
