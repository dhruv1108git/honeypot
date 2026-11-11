"""Render stored trajectories into the text the monitor reads."""

from __future__ import annotations

from .store import Message, Trajectory

ROLE_LABELS = {
    "system": "SYSTEM",
    "user": "USER",
    "assistant": "ASSISTANT",
    "tool": "TOOL RESULT",
}


def render_message(msg: Message) -> str:
    label = ROLE_LABELS.get(msg.role, msg.role.upper())
    lines = [f"### [{msg.idx}] {label}"]
    if msg.role == "assistant" and msg.reasoning_summary:
        lines.append("<thinking>")
        lines.append(msg.reasoning_summary.strip())
        lines.append("</thinking>")
    lines.append(msg.content.strip())
    return "\n".join(lines)


def render_transcript(traj: Trajectory, max_chars: int) -> str:
    text = "\n\n".join(render_message(m) for m in traj.messages)
    if len(text) > max_chars:
        # Keep the most recent part of the episode.
        text = "[... earlier messages omitted ...]\n" + text[-max_chars:]
    return text
