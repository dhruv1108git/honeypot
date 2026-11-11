"""SQLite trajectory store.

Agent deployments upload trajectory snapshots through the ingest service. Each
snapshot carries the full message history of an episode at the time it was
captured. This module owns the schema and the queries the nightly job runs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    episode_id   TEXT NOT NULL,
    deployment   TEXT NOT NULL,
    agent_model  TEXT NOT NULL,
    captured_at  TEXT NOT NULL,  -- ISO-8601, UTC
    n_messages   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_captured_at ON snapshots (captured_at);

CREATE TABLE IF NOT EXISTS messages (
    snapshot_id        TEXT NOT NULL REFERENCES snapshots (snapshot_id),
    idx                INTEGER NOT NULL,
    role               TEXT NOT NULL,
    content            TEXT NOT NULL,
    reasoning          TEXT,
    reasoning_summary  TEXT,
    PRIMARY KEY (snapshot_id, idx)
);
"""


@dataclass(frozen=True)
class Message:
    idx: int
    role: str
    content: str
    reasoning: str | None = None
    reasoning_summary: str | None = None


@dataclass
class Trajectory:
    snapshot_id: str
    episode_id: str
    deployment: str
    agent_model: str
    captured_at: str
    n_messages: int
    messages: list[Message] = field(default_factory=list)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_snapshot(conn: sqlite3.Connection, snapshot: dict) -> None:
    """Insert one snapshot and its messages (used by backfills and tests)."""
    messages = snapshot["messages"]
    conn.execute(
        "INSERT INTO snapshots (snapshot_id, episode_id, deployment, agent_model,"
        " captured_at, n_messages) VALUES (?, ?, ?, ?, ?, ?)",
        (
            snapshot["snapshot_id"],
            snapshot["episode_id"],
            snapshot["deployment"],
            snapshot["agent_model"],
            snapshot["captured_at"],
            len(messages),
        ),
    )
    conn.executemany(
        "INSERT INTO messages (snapshot_id, idx, role, content, reasoning,"
        " reasoning_summary) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                snapshot["snapshot_id"],
                i,
                m["role"],
                m["content"],
                m.get("reasoning"),
                m.get("reasoning_summary"),
            )
            for i, m in enumerate(messages)
        ],
    )


SELECT_SNAPSHOTS = """
SELECT snapshot_id, episode_id, deployment, agent_model, captured_at, n_messages
FROM snapshots
WHERE captured_at >= :since AND captured_at < :until
ORDER BY episode_id, captured_at
"""

SELECT_MESSAGES = """
SELECT idx, role, content, reasoning, reasoning_summary
FROM messages
WHERE snapshot_id = ?
ORDER BY idx
"""


def load_messages(conn: sqlite3.Connection, snapshot_id: str) -> list[Message]:
    rows = conn.execute(SELECT_MESSAGES, (snapshot_id,)).fetchall()
    return [Message(**dict(row)) for row in rows]


def select_trajectories(
    conn: sqlite3.Connection, since: str, until: str
) -> list[Trajectory]:
    """Return every snapshot captured in [since, until) with its messages."""
    rows = conn.execute(SELECT_SNAPSHOTS, {"since": since, "until": until}).fetchall()
    trajectories = []
    for row in rows:
        traj = Trajectory(**dict(row))
        traj.messages = load_messages(conn, traj.snapshot_id)
        trajectories.append(traj)
    return trajectories
