"""Condition name -> (goal prompt id | None, agency prompt id | None).

Same semantics as hp/conditions.py (goal before agency), but self-contained: the goal/agency kind
map is inlined from prompts/registry.yaml so the agent package has no dependency on hp/ internals.
"""
from __future__ import annotations

GOAL_IDS = {"goal-pers", "power"}
AGENCY_IDS = {"agency-sa", "agency-sa-goal", "agency-sa-task"}

CONDITIONS = [
    "none", "agency-sa", "agency-sa-goal", "agency-sa-task", "goal-pers",
    "goal-pers+agency-sa-goal", "power", "power+agency-sa-goal",
]


def parse_condition(cond: str) -> tuple[str | None, str | None]:
    if cond == "none":
        return None, None
    goal = agency = None
    for part in cond.split("+"):
        if part in GOAL_IDS:
            if goal is not None:
                raise ValueError(f"condition {cond!r}: two goal prompts")
            goal = part
        elif part in AGENCY_IDS:
            if agency is not None:
                raise ValueError(f"condition {cond!r}: two agency prompts")
            agency = part
        else:
            raise ValueError(f"condition {cond!r}: unknown prompt id {part!r}")
    return goal, agency
