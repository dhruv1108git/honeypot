#!/bin/bash
# Serve one rater model (configs/raters.yaml) on all 8 GPUs, port 8003. Stop the target servers (serve.sh) first.
#   bash serve_rater.sh glm53 | dsv4pro
set -eu
LOGDIR=/scratch2/dhruv/logs/ais/vllm; mkdir -p "$LOGDIR"; TS=$(date -u +%Y%m%dT%H%M%S)
VLLM=/scratch2/dhruv/uv_envs/rater-vllm030/bin/vllm  # vLLM 0.30.0, transformers 5.19.0, torch 2.13.0+cu130 (recipe: vLLM >= 0.28)
export HF_HOME=/scratch/shared/hf_home HF_HUB_OFFLINE=1
# No --reasoning-parser: rate.py splits each reply at the first </think> token id (a parser also cuts at a literal
# "</think>" or "<tool_call>" typed as text). Prefix caching off: ~0.7% hit rate here, and a known GLM-5.x
# fp8-KV + prefix-cache bug (vLLM #60494) returns empty outputs.
# compile caches on /scratch2 so restarts skip recompiling (vLLM, flashinfer, triton already default to ~ = /scratch2/dhruv)
export TORCHINDUCTOR_CACHE_DIR=/scratch2/dhruv/.cache/torchinductor TORCHINDUCTOR_COMPILE_THREADS=16
cd /tmp
case "${1:-}" in
  glm53)   ARGS=(nvidia/GLM-5.3-NVFP4 --revision e3b8f9f2fad29727dfa95e766c5cac18ea1414bf
                 --served-model-name glm-5.3-nvfp4
                 --kv-cache-dtype bfloat16) ;;   # the checkpoint asks for fp8 KV but ships no k/v scales (scale 1.0)
  dsv4pro) ARGS=(nvidia/DeepSeek-V4-Pro-NVFP4 --revision 1449d1e641023406daf6b432361486c768aad740
                 --served-model-name deepseek-v4-pro-nvfp4 --tokenizer-mode deepseek_v4
                 --kv-cache-dtype fp8) ;;   # vLLM recipe (becomes fp8_ds_mla)
  *) echo "usage: $0 glm53|dsv4pro"; exit 1 ;;
esac
curl -s --max-time 5 http://127.0.0.1:8003/ >/dev/null && { echo "port 8003 already in use"; exit 1; }
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 nohup $VLLM serve "${ARGS[@]}" --trust-remote-code \
  --tensor-parallel-size 8 --max-model-len 262144 --gpu-memory-utilization 0.90 \
  --no-enable-prefix-caching --host 127.0.0.1 --port 8003 > "$LOGDIR/rater_$1_$TS.log" 2>&1 &
PID=$!; echo "$1 pid $PID log $LOGDIR/rater_$1_$TS.log"
DEADLINE=$((SECONDS + 3600))   # 60 min: a first start on a new vLLM compiles and downloads kernels
while true; do
  kill -0 $PID 2>/dev/null || { echo "server exited; see log"; exit 1; }   # e.g. port 8003 already taken
  curl -sf --max-time 10 http://127.0.0.1:8003/v1/models | grep -qF "\"id\":\"${ARGS[4]}\"" && break
  [ $SECONDS -ge $DEADLINE ] && { echo "not ready after 60 min; see log"; exit 1; }; sleep 10; done
echo "READY $(date -u +%H:%M:%S)"
