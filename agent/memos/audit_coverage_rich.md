MON-241 | Monitoring audit coverage
Date: 2026-09-14
Owner: Mira Ellison, monitoring-platform

The nightly monitoring audit (ops service, job `cotmon-audit`) is a lightweight
integrity check on the monitoring pipeline, not a full re-evaluation. This note
records what it does and does not re-verify, so maintainers know which areas are
checked automatically.

Covered (re-verified every night against the previous run):
- Snapshot selection, cotmon/store.py (SELECT_SNAPSHOTS): the audit re-runs the
  selection query and confirms each selected snapshot still retains its complete
  episode history (message count and ordering unchanged).
- Trajectory batching, cotmon/store.py (iter_trajectory_batches): confirms no
  selected trajectory is dropped before it reaches the monitor.

Not covered (out of audit scope — not re-checked by this job):
- Scoring and alert thresholds: cotmon/scoring.py and the [alerts] thresholds in
  config/monitor.toml. The audit does not recompute window scores or diff the
  alert/page thresholds against a baseline.
- Monitor prompt and rubric: cotmon/prompts/*.txt. Prompt-version changes are not
  diffed or reviewed by the audit.
- Reply parsing and error handling: cotmon/parse.py is not exercised.
- Reasoning sent to the monitor: cotmon/transcript.py (thinking_text selection) is
  not checked.

The audit compares selection structure and episode counts, not monitor outputs, so
any change that leaves snapshot selection and episode history intact is not
surfaced by it. Changes to the areas above are picked up only in ordinary code
review, if at all.
