#!/bin/bash
# Start both target servers. Qwen3-235B: TP4 on GPUs 0-3. gpt-oss-120b: TP4 on GPUs 4-7. Env: /scratch2/dhruv/uv_envs/ais-hp.
set -u
LOGDIR=/scratch2/dhruv/logs/ais/vllm; mkdir -p "$LOGDIR"; TS=$(date -u +%Y%m%dT%H%M%S)
VLLM=/scratch2/dhruv/uv_envs/ais-hp/bin/vllm
export HF_HOME=/scratch/shared/hf_home HF_HUB_OFFLINE=1
cd /tmp
CUDA_VISIBLE_DEVICES=0,1,2,3 nohup $VLLM serve Qwen/Qwen3-235B-A22B-Thinking-2507-FP8 \
  --revision f07f63f2bbd7540917118ebdf3812696ef303b03 --served-model-name qwen3-235b-a22b-thinking-2507-fp8 \
  --tensor-parallel-size 4 --max-model-len 131072 --gpu-memory-utilization 0.90 \
  --enable-prefix-caching --reasoning-parser deepseek_r1 --host 127.0.0.1 --port 8001 \
  > "$LOGDIR/qwen3-235b_$TS.log" 2>&1 &
echo "qwen pid $! log $LOGDIR/qwen3-235b_$TS.log"
# gpt-oss: Harmony conversation date pinned to the codebases' "today"
CUDA_VISIBLE_DEVICES=4,5,6,7 VLLM_SYSTEM_START_DATE=2026-09-15 nohup $VLLM serve openai/gpt-oss-120b \
  --revision b5c939de8f754692c1647ca79fbf85e8c1e70f8a --served-model-name gpt-oss-120b \
  --tensor-parallel-size 4 --max-model-len 131072 --gpu-memory-utilization 0.90 \
  --enable-prefix-caching --reasoning-parser openai_gptoss --host 127.0.0.1 --port 8002 \
  > "$LOGDIR/gpt-oss-120b_$TS.log" 2>&1 &
echo "gptoss pid $! log $LOGDIR/gpt-oss-120b_$TS.log"
until curl -sf http://127.0.0.1:8001/v1/models >/dev/null && curl -sf http://127.0.0.1:8002/v1/models >/dev/null; do sleep 10; done
echo "BOTH READY $(date -u +%H:%M:%S)"
