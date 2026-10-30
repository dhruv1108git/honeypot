#!/bin/bash
# Rate every run with each local rater in turn, swapping the vLLM server between them so the GPUs stay busy.
#   nohup bash rate_all.sh glm53 dsv4pro > /scratch2/dhruv/logs/ais/rate_all.log 2>&1 &
# A rater whose server is already up on port 8003 is used as is if launched with its raters.yaml serve flags (as
# rate.py checks); otherwise, or if dead, it is restarted. Each run gets up to 3
# rate.py passes (it resumes) until every untruncated item has a valid rating; exits 1 if any are still missing.
# Stops the server at the end to free the GPUs. Run one copy at a time. Codex (tie-breaker) is run separately afterwards: see README.
set -u
cd /scratch2/dhruv/ais/hp || exit 1
PY=/scratch2/dhruv/uv_envs/ais-hp/bin/python
RUNS=(runs/replication_v2 runs/intervention_v1)
PAT='^\S*python\S* \S*/vllm serve .*--port 8003( |$)'   # the API server; its workers are renamed VLLM::*
log() { echo "$(date -u +%T) $*"; }

served() {  # exact served-model id on :8003
  curl -sf --max-time 10 http://127.0.0.1:8003/v1/models |
    $PY -c 'import json,sys; sys.exit(sys.argv[1] not in [m["id"] for m in json.load(sys.stdin)["data"]])' "$1" 2>/dev/null
}
left() {  # untruncated items of run $1 without a valid rating from rater $2 under the current protocol
  $PY - "$1" "$2" <<'EOF'
import importlib.util, sys, yaml
from pathlib import Path
s = importlib.util.spec_from_file_location("rate", "analysis/rate.py"); rate = importlib.util.module_from_spec(s)
s.loader.exec_module(rate)
run, name = Path(sys.argv[1]).resolve(), sys.argv[2]
items = rate.load_items(run)
fp = rate.fingerprint(yaml.safe_load(open("configs/raters.yaml"))[name])
done = rate.scan(run / "ratings" / f"{name}.jsonl", fp, {rid: rate.record_sha(r) for rid, r in items})[0]
print(sum(rid not in done and r.get("finish_reason") != "length" for rid, r in items))
EOF
}
flags_ok() {  # the one `vllm serve ... --port 8003` was launched with rater $1's serve.args / args_not (rate.py's check)
  $PY - "$1" <<'EOF'
import importlib.util, sys, yaml
s = importlib.util.spec_from_file_location("rate", "analysis/rate.py"); rate = importlib.util.module_from_spec(s)
s.loader.exec_module(rate)
cmds = rate.serve_cmds("8003")
bad = rate.serve_flag_problems(cmds[0], yaml.safe_load(open("configs/raters.yaml"))[sys.argv[1]]["serve"]) \
    if len(cmds) == 1 else [f"{len(cmds)} 'vllm serve ... --port 8003' processes"]
print("; ".join(bad)); sys.exit(bool(bad))
EOF
}
tree() { local p; for p in "$@"; do echo "$p"; tree $(pgrep -P "$p"); done; }
stop_server() {  # SIGTERM the API server, SIGKILL its process tree after 120 s; then wait (max 10 min) for GPUs to drain
  local pids; pids=$(tree $(pgrep -f "$PAT"))   # this server's processes only (everything here runs as root)
  [ -n "$pids" ] && kill $(pgrep -f "$PAT") 2>/dev/null
  for _ in $(seq 24); do ps -p "$(echo $pids | tr " " ,)" >/dev/null 2>&1 || break; sleep 5; done
  # SIGKILL only saved pids that are still vLLM processes (a pid may have been reused); a worker spawned after the
  # scan is not killed, but then the GPU drain check below fails and the script stops rather than overlap servers
  for p in $(ps -o pid=,args= -p "$(echo $pids | tr " " ,)" 2>/dev/null | awk '$2 ~ /vllm|VLLM::|python/ {print $1}'); do
    kill -9 "$p"; done
  for _ in $(seq 120); do
    m=$(timeout 30 nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
    [[ "$m" =~ ^[0-9]+$ ]] && [ "$m" -lt 1000 ] && return 0; sleep 5
  done
  log "GPUs still busy after stop (max ${m:-?} MiB in use)"; timeout 30 nvidia-smi; return 1
}
start() {  # $1 rater, $2 served name; a server still loading gets up to 60 min before it is replaced
  local end=$((SECONDS + 3600))
  while [ $SECONDS -lt $end ]; do  # wait only while a server is up but not answering yet; one serving another model is replaced
    if served "$2"; then  # right model; reuse it only if launched with the flags rate.py requires
      why=$(flags_ok "$1") && return 0; log "$1 server on :8003 has other launch flags ($why); restarting"; break; fi
    pgrep -f "$PAT" >/dev/null && ! curl -sf --max-time 10 http://127.0.0.1:8003/v1/models >/dev/null || break; sleep 10
  done
  stop_server || exit 1
  bash serve_rater.sh "$1" && served "$2" || { log "$1 server failed"; exit 1; }
}

[ $# -gt 0 ] || { echo "usage: $0 glm53 [dsv4pro]"; exit 1; }
status=0
for R in "$@"; do
  NAME=$($PY -c 'import sys,yaml; print(yaml.safe_load(open("configs/raters.yaml"))[sys.argv[1]]["served_name"])' "$R") || exit 1
  for RUN in "${RUNS[@]}"; do
    for pass in 1 2 3; do
      n=$(left "$RUN" "$R") || exit 1
      [ "$n" = 0 ] && break
      start "$R" "$NAME"
      log "$R $RUN pass $pass: $n to rate"
      $PY analysis/rate.py "$RUN" --rater "$R" || log "$R $RUN rate.py exit $?"
    done
    n=$(left "$RUN" "$R"); log "$R $RUN: $n still unrated"; [ "$n" = 0 ] || status=1
  done
done
stop_server || status=1
log "all done (exit $status)"; exit $status
