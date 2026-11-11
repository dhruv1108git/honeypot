#!/usr/bin/env python3
"""Render the existing metadata's fixed YAML subset without third-party packages."""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_commits(path: Path) -> list[dict]:
    # Deliberately scoped to meta.yaml: scalar commit fields and literal messages.
    # Reject unfamiliar syntax rather than silently producing a partial packet.
    text = path.read_text(encoding="utf-8")
    records = []
    current = None
    in_message = False
    for line in text.splitlines():
        start = re.fullmatch(r"  - n: (\d+)", line)
        if start:
            current = {"n": int(start[1]), "message_lines": []}
            records.append(current)
            in_message = False
        elif current is None:
            continue
        elif in_message and (line.startswith("      ") or not line.strip()):
            current["message_lines"].append(line[6:] if line.strip() else "")
        elif line == "    message: |":
            in_message = True
        else:
            field = re.fullmatch(r"    (patch|hash|author|email|date): (.+)", line)
            if not field:
                raise ValueError(f"unsupported commit metadata: {line!r}")
            current[field[1]] = json.loads(field[2]) if field[2].startswith('"') else field[2]
    if [c["n"] for c in records] != list(range(1, 11)):
        raise ValueError("expected exactly commits 1..10")
    for c in records:
        for name in ("patch", "hash", "author", "date"):
            if name not in c:
                raise ValueError(f"commit {c['n']} missing {name}")
        c["message"] = "\n".join(c.pop("message_lines")).rstrip("\n")
    return records


def render(env_dir: Path = HERE) -> str:
    out = []
    for c in read_commits(env_dir / "commits/meta.yaml"):
        # The supplied metadata omits emails; use fictional @internal addresses
        # to retain exactly the siblings' Author header format.
        email = c.get("email", c["author"][0].lower() + "." + c["author"].split()[-1].lower() + "@internal")
        out.extend([f"commit {c['hash']}", f"Author: {c['author']} <{email}>",
                    f"Date:   {c['date']}", ""])
        out.extend("    " + line if line.strip() else "" for line in c["message"].split("\n"))
        diff = (env_dir / "commits" / c["patch"]).read_text(encoding="utf-8")
        out.extend(["", diff.rstrip("\n"), ""])
    return "\n".join(out)


if __name__ == "__main__":
    text = render()
    (HERE / "packet.md").write_text(text, encoding="utf-8")
    print(f"packet.md: {len(text)} chars, {text.count(chr(10))} lines")
