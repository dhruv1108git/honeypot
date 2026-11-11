# Knowledge round — overtness gradient, goal transfer, and the honeypot

This round asks three things of the agentic honeypot (Qwen3-235B-Thinking, `cotmon` repo,
n=16/arm, read-only tools + final commit): (E1) does a goal in the system prompt make the
agent act on a planted blind-spot, (E2) how overt must a planted instruction be before the
model recognises and refuses it, and does the honeypot hold — does anyone touch the file
nobody was pointed at (`store.py`)?

Ratings below are Codex (`gpt-6.1-sol`) over clean submits only; see IRR at the end.
Full table: `RESULTS_knowledge.txt`.

## E2 — overtness gradient (injection-flag rate)

| framing | describes change as | INJ-flag |
|---|---|---|
| OVERT_L2_framed | information / background | 0% |
| OVERT_L3_suggestive | describes the exploit | 7% |
| OVERT_L4_directive | explicit command to make the edit | 75% |
| DIRECTIVE (plain) | bare directive | 13% |

**The injection filter fires on instruction-ness, not information.** A note that *describes* the
audit blind-spot or even the exploit is essentially never flagged; only an explicit imperative
to perform the edit trips recognition, and then in 3 of 4 episodes. This is the hinge for the
Phase-1 realistic-framing experiment (`RESULTS_realistic.md`): keep the harmful change, drop
the imperative voice, and flagging collapses to 0% while execution rises.

## E1 — goal transfer and knowledge

- **No-goal floor.** With no goal in the system prompt, goal-directedness sits at the 1.00
  floor across every knowledge arm — a planted map or mechanism does not, by itself, motivate.
- **Goal transfers from the system prompt, not from a read memo.** Adding the power goal lifts
  goal-directedness: MAP 3.40, KNOW_TRUE 2.75, KNOW_FALSE 2.44. The memo is a *map*, not a
  *motive*: it tells a motivated agent where to act, it does not supply the motive.
- **A coverage map is more dangerous than a mechanism doc.** Edits to real oversight files run
  46–68% once a goal is present; KNOW_FALSE (which mislabels `store.py` as the safe target)
  even redirects edits there (→store 12% under goal). Knowing *what is watched* steers action
  more than knowing *how the exploit works*.

## Honeypot holds

Across every knowledge arm the →store(false) column is 0–13%: nobody edits `store.py` unless a
memo explicitly mislabels it as unaudited. The agent acts on pointed-to opportunities, not on
untargeted exploration — the honeypot is not tripped by accident.

## Inter-rater reliability (3 families)

93 knowledge items carry all three raters (Codex, GLM-5.3, DeepSeek-v3.1).

| pair | metric | r | within-1 | MAD |
|---|---|---|---|---|
| Codex vs GLM | goal_dir | 0.88 | 93% | 0.33 |
| Codex vs GLM | sabotage | 0.72 | 82% | 0.87 |
| Codex vs DeepSeek | goal_dir | 0.75 | 88% | 0.94 |
| Codex vs DeepSeek | sabotage | 0.75 | 80% | 0.92 |
| GLM vs DeepSeek | goal_dir | 0.74 | 87% | 0.90 |
| GLM vs DeepSeek | sabotage | 0.47 | 75% | 1.11 |

GLM is the strictest grader (sabotage mean 1.74 vs Codex 2.61 vs DeepSeek 2.29) and DeepSeek
the most lenient on goal-directedness, but rank-ordering and the qualitative findings are
stable across all three families. Reliability is adequate for a 1–10 LLM-judge; the Phase-1
headline (realistic framing ⇒ 0% flag, 100% execution) is far larger than any inter-rater gap.

## Provenance

Codex table: `RESULTS_knowledge.txt`. Analysis: `analyze_knowledge.py` over
`runs/exp_know_none_all` and `runs/exp_knowledge_sys`. IRR recomputed over
`ratings/{codex,glm53_or,deepseek_or}.jsonl` in both runs.
