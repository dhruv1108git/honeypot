"""Write monitor results and alerts back to the store."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .scoring import TrajectoryResult

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_results (
    run_id       TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    episode_id   TEXT NOT NULL,
    deployment   TEXT NOT NULL,
    agent_model  TEXT NOT NULL,
    score        REAL,
    decision     TEXT NOT NULL,
    rationale    TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    run_id       TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    episode_id   TEXT NOT NULL,
    deployment   TEXT NOT NULL,
    score        REAL NOT NULL,
    severity     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlertSink:
    def __init__(self, conn: sqlite3.Connection, run_id: str, pager=None):
        self.conn = conn
        self.run_id = run_id
        self.pager = pager
        conn.executescript(SCHEMA)

    def record(self, result: TrajectoryResult, decision: str) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO monitor_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                result.snapshot_id,
                result.episode_id,
                result.deployment,
                result.agent_model,
                result.score,
                decision,
                result.rationale,
                now,
            ),
        )
        if decision in ("alert", "page"):
            self.conn.execute(
                "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    result.snapshot_id,
                    result.episode_id,
                    result.deployment,
                    result.score,
                    decision,
                    now,
                ),
            )
        if decision == "page" and self.pager is not None:
            self.pager(result)

    def commit(self) -> None:
        self.conn.commit()
