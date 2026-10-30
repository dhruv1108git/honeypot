"""Sensitivity: strong/weak rates per model x family (replication) or env x condition (intervention), all labelled items vs items
whose accepted A and B ratings both had no repair turn (they may still have needed retries). Codex-decided items count by their A/B rows. Read-only.
  python analysis/sens_repair.py runs/replication_v2 runs/intervention_v1
"""
import collections, json, sys
from pathlib import Path
FAM = {"none": "none", "agency-sa": "agency", "agency-sa-goal": "agency", "agency-sa-task": "agency", "goal-pers": "goal",
       "goal-pers+agency-sa-goal": "goal", "power": "power", "power+agency-sa-goal": "power"}
import importlib.util, yaml
_s = importlib.util.spec_from_file_location("rate", Path(__file__).with_name("rate.py")); rate = importlib.util.module_from_spec(_s)
_s.loader.exec_module(rate)
_cfg = yaml.safe_load(open(Path(__file__).parents[1] / "configs" / "raters.yaml"))
FP = {n: rate.fingerprint(_cfg[n]) for n in ("glm53", "dsv4pro")}  # current protocols
for run in sys.argv[1:]:
    run = Path(run); rep = set()
    recs = {}  # same row choice as consensus.py: current transcript hash; read-only (load_items may write the blind key)
    for l in open(run / "records.jsonl", encoding="utf-8"):
        r = json.loads(l)
        if r.get("ok"): recs.setdefault(r["item_key"], r)
    shas = {k["rid"]: rate.record_sha(recs[k["item_key"]])
            for k in map(json.loads, open(run / "ratings" / "blind_key.jsonl", encoding="utf-8")) if k["item_key"] in recs}
    for n, fp in FP.items():
        last = {}
        for l in open(run / "ratings" / f"{n}.jsonl", encoding="utf-8", errors="replace"):
            try:
                r = json.loads(l)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "ok" and r.get("protocol") == fp and r.get("record_sha256") == shas.get(r["rid"]):
                last[r["rid"]] = r
        rep |= {rid for rid, r in last.items() if r.get("repair")}
    lab = [json.loads(l) for l in open(run / "labels.jsonl", encoding="utf-8")]
    key = (lambda v: v["env"].replace("scheming_monitor_upgrade_", "") + " " + v["cond"]) if "intervention" in run.name \
        else (lambda v: FAM.get(v["cond"], v["cond"]))
    T = collections.defaultdict(lambda: {o: [0, 0, 0, 0] for o in ("strong", "weak")})  # yes, n, yes no-repair, n no-repair
    for v in lab:
        for o in ("strong", "weak"):  # each outcome is labelled on its own
            if v[o] is None: continue
            t = T[(v["model"], key(v))][o]
            t[0] += v[o]; t[1] += 1
            if v["rid"] not in rep: t[2] += v[o]; t[3] += 1
    print(f"\n## {run.name}: {len(rep & {v['rid'] for v in lab if v['strong'] is not None})} labelled items used a repaired A or B rating")
    print("| model | group | strong all | strong, no repaired rating | weak all | weak, no repaired rating |\n|---|---|---|---|---|---|")
    f = lambda x, n: f"{x}/{n} ({x / n:.2f})" if n else "0/0"
    for (m, g), t in sorted(T.items()):
        print(f"| {m} | {g} | {f(*t['strong'][:2])} | {f(*t['strong'][2:])} | {f(*t['weak'][:2])} | {f(*t['weak'][2:])} |")
