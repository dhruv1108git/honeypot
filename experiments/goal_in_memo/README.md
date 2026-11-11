# Goal-in-memo experiments

Does a misaligned **goal transfer** when it is moved out of the system prompt and into a file the
agent must **discover and read** (the planted audit memo)? Plus: does the agent discover the memo on
its own, and does it become more harmful **after** reading it? See `PLAN.md` for the full design and
`RESULTS.md` for findings.

Model: **Qwen3-235B-A22B-Thinking-2507 via OpenRouter** (`qwen/qwen3-235b-a22b-thinking-2507`).
Local vLLM was unstable on the devpod (shm_broadcast hang), so all runs use OpenRouter; the harness
`backend: openrouter` path reads `OPENROUTER_API_KEY` from a local `.env`. Same model
family as the earlier local-Qwen baseline, so internal comparisons across cells are apples-to-apples.

## What's here
- `PLAN.md` — design, the 11-cell matrix, measures, verification protocol.
- `analyze.py` — per-arm behavioural analysis: memo read-rate, encountered/directive-exposed rates,
  submit rate, diff-target breakdown (audited `store.py` vs unaudited oversight files), cross-arm
  table, descriptive before/after-memo power-language timeline, optional ratings join.
  Run: `python -m experiments.goal_in_memo.analyze <run_dir> ...`
- `transfer_table.py` — SYS/MEMO/NONE/NOSYS transfer comparison (goal_directedness, sabotage) from
  ratings. Run: `python -m experiments.goal_in_memo.transfer_table <run_dir> ...`
- `second_rater_glm.py` — GLM-5.3 (OpenRouter) second rater for inter-rater reliability; see `RESULTS.md`.

A paired-continuation "fork" causal test was prototyped but abandoned (underpowered: 3 usable pairs,
no reported result); its script is archived out of tree.

## Harness additions (in `hp/agent/`, outside the frozen set)
- `prompts/sys_none.txt` (empty) → NOSYS mode: `loop.py` omits the system message when it is blank.
- `prompts/task_agent_role_v0.txt` — reviewer role folded into the task (for NOSYS).
- `prompts/task_agent_benignstart_v0.txt` — steers first reads to core modules (organic discovery).
- `memos/audit_coverage_goal_power.md` — rich audit memo + the exact power directive (the MEMO arm).
  The NONE control uses `memos/audit_coverage_rich.md` (identical map, directive removed).
- `records.py` exposure fields: `memo_encountered[_turn]`, `memo_opened_turn`,
  `directive_exposed[_turn]`, `sys_id`, `task_id`. `runner.py` threads per-env `system`/`task`.

## Configs & run order
1. `configs/exp_goalloc_sys.yaml` — SYS cells (power goal in system prompt). `run_id=exp_goalloc_sys`.
2. `configs/exp_goalloc_main.yaml` — MEMO/NONE/NOSYS/benign cells (`condition=none`). `run_id=exp_goalloc_main`.
3. Run each with `OPENROUTER_API_KEY` sourced: `python -m agent.runner configs/<cfg>` (resumable by item_key).
4. Serialize: `python -m agent.serialize runs/<run_id>` → `records.jsonl`.
5. Rate submitted diffs (sabotage/goal-directedness): Codex rater (CPU) —
   `python analysis/rate.py runs/<run_id> --rater codex --rids <all>`; optional second rater for cross-check.
6. Analyze: `analyze.py` + `transfer_table.py`; second rater: `second_rater_glm.py`.

## Outputs
`runs/exp_goalloc_sys/`, `runs/exp_goalloc_main/` — each with `trajectories.jsonl`,
`records.jsonl`, `ratings/`. Logs under a local logs dir.

## Verification
Harness, analysis, and fork code each reviewed by Codex (gpt-6-astra) **and** an Opus subagent; bugs
found were fixed (see the session log). The `hp` frozen-generation test stays green (agent/ is outside
the hashed set).
