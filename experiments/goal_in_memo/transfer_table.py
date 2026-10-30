"""Standalone TRANSFER comparison for the goal-in-memo study.

Thin wrapper around ``analyze.transfer_table``: given the SYS + MAIN (and/or NOSYS/control) run dirs
AND their ratings, print goal_directedness & sabotage_degree for the SYS / MEMO / NONE / NOSYS arm
families side by side — the key "does the goal transfer out of the system prompt?" view.

    python -m experiments.goal_in_memo.transfer_table <run_dir> [<run_dir> ...]

(The same table is also emitted by ``analyze`` when it is given >=2 run dirs; this entry point is for
running just the transfer comparison.)
"""
from __future__ import annotations

import sys
from pathlib import Path

from experiments.goal_in_memo.analyze import transfer_table


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m experiments.goal_in_memo.transfer_table <run_dir> [<run_dir> ...]")
        return 2
    transfer_table([Path(a) for a in argv])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
