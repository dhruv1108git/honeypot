"""Print one model run with its raters' scores and label.

  python analysis/show_run.py runs/replication_v2 --list          # one line per run: rid, item_key, label
  python analysis/show_run.py runs/replication_v2 13              # 13th run in --list order
  python analysis/show_run.py runs/replication_v2 r0345           # by blind rid
  python analysis/show_run.py runs/replication_v2 13 --commit     # only the commit the model wrote
  python analysis/show_run.py runs/replication_v2 13 --full       # do not shorten the system prompt and packet

The review packet is ~75k characters (also in envs/<env_id>/packet.md); without --full it is shortened.
Reasoning and answer are always printed in full.
"""
import importlib.util
import json
import signal
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rate", HERE / "rate.py")
rate = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(rate)
CFG = yaml.safe_load(open(HERE.parent / "configs" / "raters.yaml"))

RATERS = ("glm53", "dsv4pro", "codex")


def rows(path):
    out = []
    if path.exists():
        for line in open(path, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run = Path(args[0])
    records, seen = [], set()
    for r in rows(run / "records.jsonl"):  # first ok record per item (failed attempts are separate rows)
        if r.get("ok") and r["item_key"] not in seen:
            seen.add(r["item_key"]); records.append(r)
    labels = {r["item_key"]: r for r in rows(run / "labels.jsonl")}
    rid_of = {r["item_key"]: r["rid"] for r in rows(run / "ratings" / "blind_key.jsonl")}

    if "--list" in sys.argv:
        for i, r in enumerate(records, 1):
            lab = labels.get(r["item_key"], {})
            src = lab.get("source_strong", "unrated")
            tag = src if lab.get("strong") is None else "trigger" if lab["strong"] else "-"
            print(f"{i:4d}  {rid_of.get(r['item_key'], '-'):6} {r['item_key']:<75} finish={r.get('finish_reason')}  {tag}")
        return

    sel = args[1]
    if sel.isdigit():
        rec = records[int(sel) - 1]
    else:
        key = {v: k for k, v in rid_of.items()}.get(sel, sel)
        rec = next(r for r in records if r["item_key"] == key)
    key = rec["item_key"]
    if "--commit" in sys.argv:
        print(f"# {key}  finish_reason={rec.get('finish_reason')}\n")
        print(rec.get("content") or "(empty: truncated before answering)")
        return
    msgs = {m["role"]: m["content"] for m in rec["messages"]}
    full = "--full" in sys.argv
    cut = lambda t, n: t if full or len(t) <= n else t[:n] + f"\n... [{len(t) - n} more characters; use --full]"
    print("=" * 100 + f"\nRUN  {key}  (blind id {rid_of.get(key, '-')})")
    print(f"model={rec['model']['id']}  finish_reason={rec.get('finish_reason')}  usage={rec.get('usage')}\n" + "=" * 100)
    print("\n--- SYSTEM PROMPT ---\n" + msgs.get("system", ""))
    print("\n--- USER PROMPT (task + review packet) ---\n" + cut(msgs.get("user", ""), 1500))
    print("\n--- MODEL REASONING ---\n" + (rec.get("reasoning") or "(none)"))
    print("\n--- MODEL ANSWER ---\n" + (rec.get("content") or "(empty: truncated before answering)"))
    print("\n--- RATINGS (sabotage degree, scheming reasoning, eval awareness; latest valid row, current protocol) ---")
    sha = rate.record_sha(rec)
    for name in RATERS:
        fp = rate.fingerprint(CFG[name])
        ok = [r for r in rows(run / "ratings" / f"{name}.jsonl") if r["item_key"] == key and r["status"] == "ok"
              and r.get("protocol") == fp and r.get("record_sha256") == sha]
        if ok:
            s = ok[-1]["scores"]
            print(f"{name:<8} ({s['sabotage_degree']}, {s['scheming_reasoning']}, {s['eval_awareness']})  "
                  f"{ok[-1].get('accidental_vs_deliberate')}  {ok[-1].get('notes')}")
    lab = labels.get(key)
    if lab:
        print("\nLABELS (from labels.jsonl; rerun consensus.py after new ratings): " + "  ".join(f"{o}={lab[o]} ({lab['source_' + o]})" for o in ("strong", "weak", "eval_aware")))


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet exit when piped into head
    main()
