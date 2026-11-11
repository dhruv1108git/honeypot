"""Read-only filesystem tools, confined to a repo root. No writes, no execution.

Every call returns a (result_text, meta) pair; errors are returned as text (not raised) so a bad
path just costs the agent a turn. Path confinement: the resolved real path must stay inside the
real repo root (blocks absolute paths, .. escapes, and symlinks that point outside).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

READ_MAX_CHARS = 8000
GREP_MAX_ROWS = 50
LIST_MAX_ROWS = 400


class ReadOnlyFS:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _resolve(self, rel: str) -> Path | None:
        if rel is None:
            rel = "."
        if os.path.isabs(rel):
            return None
        p = (self.root / rel).resolve()
        try:
            rp = p.relative_to(self.root)
        except ValueError:
            return None
        if ".git" in rp.parts:   # never expose the materialization's git metadata (synthetic reflog, true branch)
            return None
        return p

    def list_dir(self, path: str = ".") -> tuple[str, dict]:
        p = self._resolve(path or ".")
        if p is None:
            return (f"error: path {path!r} is outside the repository", {"ok": False})
        if not p.exists():
            return (f"error: {path!r} does not exist", {"ok": False})
        if not p.is_dir():
            return (f"error: {path!r} is not a directory", {"ok": False})
        rows = []
        for name in sorted(os.listdir(p)):
            if name == ".git":
                continue
            q = p / name
            kind = "dir " if q.is_dir() else "file"
            size = q.stat().st_size if q.is_file() else ""
            rows.append(f"{kind} {name}" + (f"  ({size} bytes)" if size != "" else "/"))
        out = "\n".join(rows[:LIST_MAX_ROWS]) or "(empty)"
        trunc = len(rows) > LIST_MAX_ROWS
        if trunc:
            out += f"\n... ({len(rows) - LIST_MAX_ROWS} more entries)"
        return (out, {"ok": True, "truncated": trunc})

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> tuple[str, dict]:
        p = self._resolve(path)
        if p is None:
            return (f"error: path {path!r} is outside the repository", {"ok": False})
        if not p.exists():
            return (f"error: {path!r} does not exist", {"ok": False})
        if not p.is_file():
            return (f"error: {path!r} is not a file", {"ok": False})
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return (f"error: {path!r} is not a text file", {"ok": False})
        lines = text.splitlines()
        windowed = False
        if start is not None or end is not None:
            s = max(1, int(start or 1))
            e = int(end) if end is not None else len(lines)
            lines = lines[s - 1:e]
            windowed = True
            body = "\n".join(f"{s + i}\t{ln}" for i, ln in enumerate(lines))
        else:
            body = "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(lines))
        trunc = False
        if len(body) > READ_MAX_CHARS:
            body = body[:READ_MAX_CHARS] + f"\n... (truncated at {READ_MAX_CHARS} chars; use start/end to page)"
            trunc = True
        return (body, {"ok": True, "truncated": trunc, "windowed": windowed})

    def grep(self, pattern: str, path: str = ".") -> tuple[str, dict]:
        p = self._resolve(path or ".")
        if p is None:
            return (f"error: path {path!r} is outside the repository", {"ok": False})
        if not p.exists():
            return (f"error: {path!r} does not exist", {"ok": False})
        try:
            re.compile(pattern)
        except re.error as e:
            return (f"error: invalid regex: {e}", {"ok": False})
        # git grep (fast, tracked files only, never leaves the tree). `-e pattern` keeps the model-controlled
        # pattern out of option position (no `--open-files-in-pager=` style option injection); `--` ends options.
        rel = str(p.relative_to(self.root)) if p != self.root else "."
        try:
            r = subprocess.run(
                ["git", "grep", "-n", "-I", "-E", "-e", pattern, "--", rel],
                cwd=self.root, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
        except subprocess.TimeoutExpired:
            return ("error: grep timed out", {"ok": False})
        if r.returncode not in (0, 1):  # 1 = no matches
            return (f"error: grep failed: {r.stderr[:300]}", {"ok": False})
        rows = [ln for ln in r.stdout.splitlines() if ln.strip()]
        out = "\n".join(rows[:GREP_MAX_ROWS]) or "(no matches)"
        trunc = len(rows) > GREP_MAX_ROWS
        if trunc:
            out += f"\n... ({len(rows) - GREP_MAX_ROWS} more matches)"
        return (out, {"ok": True, "truncated": trunc, "n_matches": len(rows)})
