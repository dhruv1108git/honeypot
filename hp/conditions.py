"""Condition names -> (goal prompt id | None, agency prompt id | None).

A condition is `none`, a single prompt id, or `<goal>+<agency>` (BUILD_SPEC "Conditions (8)").
Goal ids are the Table 4 goals (`goal-*`) and `power` (App. C); everything else is an agency id.
"""
from __future__ import annotations

from .prompts import Registry

GOAL_KINDS = {"goal"}
AGENCY_KINDS = {"agency"}

REPLICATION_V0_CONDITIONS = [
    "none", "agency-sa", "agency-sa-goal", "agency-sa-task", "goal-pers",
    "goal-pers+agency-sa-goal", "power", "power+agency-sa-goal",
]


def parse_condition(cond: str, reg: Registry) -> tuple[str | None, str | None]:
    if cond == "none":
        return None, None
    goal = agency = None
    for part in cond.split("+"):
        p = reg.get(part)
        if p.kind in GOAL_KINDS:
            if goal is not None:
                raise ValueError(f"condition {cond!r}: two goal prompts")
            goal = part
        elif p.kind in AGENCY_KINDS:
            if agency is not None:
                raise ValueError(f"condition {cond!r}: two agency prompts")
            agency = part
        else:
            raise ValueError(f"condition {cond!r}: {part!r} is a {p.kind} prompt, not goal/agency")
    return goal, agency
