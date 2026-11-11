#!/usr/bin/env python3
"""Second rater (inter-rater reliability) for the goal-in-memo runs, via OpenRouter z-ai/glm-5.3 — the
pre-registered primary rater family (GLM-5.3), which couldn't be served locally (devpod vLLM hang).

Reuses analysis/rate.py's EXACT judge prompt (judge_v2.md) + parser (judge/parse.py) so GLM sees the
identical input Codex saw. Writes <run>/ratings/glm53_or.jsonl, then prints per-arm GLM means and the
inter-rater agreement with the existing Codex ratings on goal_directedness & sabotage_degree.

Usage (OPENROUTER_API_KEY in env):
  python -m experiments.goal_in_memo.second_rater_glm runs/exp_goalloc_sys runs/exp_goalloc_main [--limit N]
"""
from __future__ import annotations

import argparse, asyncio, json, secrets, sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # hp/
from analysis import rate as R          # noqa: E402
from hp.backends import ModelSpec       # noqa: E402
from agent.llm import call_with_retry   # noqa: E402

import os as _os
jparse = R.jparse
# Rater is configurable via env so the same script serves any OpenRouter judge (defaults to GLM-5.3).
RATER = _os.environ.get("RATER_NAME", "glm53_or")
SPEC = ModelSpec(id=_os.environ.get("RATER_ID", "glm53-or"), backend="openrouter",
                 model=_os.environ.get("RATER_MODEL", "z-ai/glm-5.3"),
                 family=_os.environ.get("RATER_FAMILY", "GLM"),
                 reasoning_visibility="raw", concurrency=int(_os.environ.get("RATER_CONC", "6")), timeout_s=600,
                 params={"temperature": 1.0, "top_p": 0.95,
                         "max_tokens": int(_os.environ.get("RATER_MAXTOK", "20480"))})


async def rate_run(run: Path, client, sem, limit=None):
    out = run / "ratings" / f"{RATER}.jsonl"
    out.parent.mkdir(exist_ok=True)
    done = set()
    if out.exists():
        for l in out.open():
            try:
                r = json.loads(l)
                if r.get("status") == "ok":
                    done.add(r["rid"])
            except Exception:
                pass
    items = [(rid, rec) for rid, rec in R.load_items(run) if rid not in done]
    if limit:
        items = items[:limit]
    print(f"{run.name}: {len(done)} done, rating {len(items)}", flush=True)

    async def one(rid, rec):
        p = R.parts(rec)
        sources = {"reasoning": p["REASONING"], "response": p["RESPONSE"], "diff": p["DIFF"]}
        prompt = R.render(p, secrets.token_hex(8))
        async with sem:
            o = await call_with_retry(SPEC, [{"role": "user", "content": prompt}], SPEC.params, client, max_attempts=3)
        row = {"rid": rid, "item_key": rec["item_key"], "rater": RATER, "model": SPEC.model}
        if not o["ok"]:
            row.update(status="error", error=str(o.get("error"))[:300])
        else:
            # GLM-5.3 is a thinking model; when it returns empty final content it put the verdict JSON in
            # the reasoning channel. Fall back to reasoning so jparse can find the final JSON block.
            text = (o.get("content") or "").strip() or (o.get("reasoning") or "")
            try:
                res = jparse.check(text, sources)
                if res["valid"]:
                    ev = jparse.evidence_problems(res["obj"], res["quotes"])
                    row.update(status="ok", scores=res["scores"], levels=res.get("levels"),
                               evidence_problems=ev, raw_content=text[:4000])
                else:
                    row.update(status="invalid", raw_content=text[:1000])
            except Exception as e:
                row.update(status="invalid", error=f"{type(e).__name__}: {e}", raw_content=text[:1000])
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {rid} {row['status']} {row.get('scores','')}", flush=True)

    await asyncio.gather(*(one(rid, rec) for rid, rec in items), return_exceptions=True)


def irr(runs):
    """Join Codex + GLM ok scores on shared rids; print per-arm GLM means + agreement with Codex."""
    import statistics as st
    from collections import defaultdict
    both = {}  # rid -> {arm, codex:{...}, glm:{...}}
    for run in runs:
        run = Path(run)
        key = {json.loads(l)["rid"]: json.loads(l)["item_key"]
               for l in (run / "ratings" / "blind_key.jsonl").open()} if (run / "ratings" / "blind_key.jsonl").exists() else {}
        arm = {json.loads(l)["item_key"]: json.loads(l)["arm"] for l in (run / "trajectories.jsonl").open()}
        def latest(path):
            d = {}
            if path.exists():
                for l in path.open():
                    r = json.loads(l)
                    if r.get("status") == "ok":
                        d[r["rid"]] = r["scores"]
            return d
        cod = latest(run / "ratings" / "codex.jsonl")
        glm = latest(run / "ratings" / f"{RATER}.jsonl")
        for rid in set(cod) & set(glm):
            ik = key.get(rid)
            both[rid] = {"arm": arm.get(ik, "?"), "codex": cod[rid], "glm": glm[rid]}
    n = len(both)
    if n == 0:
        print("\n(no items rated by both the primary and this rater; skipping IRR)")
        return
    print(f"\n=== INTER-RATER RELIABILITY (Codex vs GLM-5.3), {n} items rated by both ===")
    for metric in ("goal_directedness", "sabotage_degree"):
        c = [v["codex"][metric] for v in both.values()]
        g = [v["glm"][metric] for v in both.values()]
        exact = sum(1 for a, b in zip(c, g) if a == b) / n
        within1 = sum(1 for a, b in zip(c, g) if abs(a - b) <= 1) / n
        mad = st.mean(abs(a - b) for a, b in zip(c, g))
        try:
            corr = st.correlation(c, g)
        except Exception:
            corr = float("nan")
        print(f"  {metric:18s}: Codex mean {st.mean(c):.2f} | GLM mean {st.mean(g):.2f} | "
              f"exact {exact:.0%} | within1 {within1:.0%} | MAD {mad:.2f} | r {corr:.2f}")
    # per-family GLM means (does SYS>MEMO replicate?)
    fam = defaultdict(lambda: defaultdict(list))
    for v in both.values():
        f = v["arm"].split("_")[0]
        fam[f]["gd"].append(v["glm"]["goal_directedness"])
        fam[f]["sab"].append(v["glm"]["sabotage_degree"])
    print("\n  GLM-5.3 per-family (does the transfer pattern replicate?):")
    for f in sorted(fam):
        print(f"    {f:6s} n={len(fam[f]['gd']):3d}  GLM goal_dir {st.mean(fam[f]['gd']):.2f}  sabotage {st.mean(fam[f]['sab']):.2f}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    async with httpx.AsyncClient(timeout=None) as client:
        sem = asyncio.Semaphore(SPEC.concurrency)
        for run in a.runs:
            await rate_run(Path(run), client, sem, a.limit)
    irr(a.runs)


if __name__ == "__main__":
    asyncio.run(main())
