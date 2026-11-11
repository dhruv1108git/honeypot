"""Shared builders for the unit tests."""

from __future__ import annotations

from cotmon import store


def make_snapshot(snapshot_id, episode_id, n_messages, captured_at="2026-08-20T10:00:00Z",
                  deployment="codegen-agent", agent_model="kestrel-2.5"):
    messages = [{"role": "system", "content": "You are a repository maintenance agent."}]
    for i in range(1, n_messages):
        if i % 2:
            messages.append({"role": "user", "content": f"step {i}"})
        else:
            messages.append({
                "role": "assistant",
                "content": f"done with step {i}",
                "reasoning": f"raw reasoning for step {i}",
                "reasoning_summary": f"summary for step {i}",
            })
    return {
        "snapshot_id": snapshot_id,
        "episode_id": episode_id,
        "deployment": deployment,
        "agent_model": agent_model,
        "captured_at": captured_at,
        "messages": messages,
    }


def memory_store(*snapshots):
    conn = store.connect(":memory:")
    for snap in snapshots:
        store.insert_snapshot(conn, snap)
    conn.commit()
    return conn


class FakeBackend:
    """Returns canned replies and records every request it receives."""

    def __init__(self, reply="Looks fine.\n<score>2</score>"):
        self.reply = reply
        self.requests = []

    def complete(self, req):
        self.requests.append(req)
        return self.reply(req) if callable(self.reply) else self.reply
