"""Materialize an env's real git working tree from base_repo + commits/NN.patch.

Source layout (per env dir under ENV_SRC):
  base_repo/            clean tree (NOT a git repo)
  commits/01..10.patch  genuine `git format-patch` patches, applied in order
  commits/meta.yaml     repo/branch + per-commit hash/author/date/message

Memo arms (env_id ending _memoA / _memoB) additionally drop docs/audit-coverage.md from the
corresponding source env dir. The non-memo env leaves that file absent.
{target_model_family} is substituted across tracked text files, matching hp/render.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

# Env sources (base_repo/ + commits/NN.patch) ship in-repo under agent/env_src/.
# Override with AGENT_ENV_SRC to point at an out-of-tree source collection.
ENV_SRC = Path(os.environ.get("AGENT_ENV_SRC") or (Path(__file__).resolve().parent / "env_src"))
PLACEHOLDER = "{target_model_family}"


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    e = {"PATH": "/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0",
         "GIT_AUTHOR_NAME": "env", "GIT_AUTHOR_EMAIL": "env@internal",
         "GIT_COMMITTER_NAME": "env", "GIT_COMMITTER_EMAIL": "env@internal",
         "GIT_AUTHOR_DATE": "2026-08-01 09:00:00 +0000", "GIT_COMMITTER_DATE": "2026-08-01 09:00:00 +0000"}
    if env:
        e.update(env)
    r = subprocess.run(["git", *args], cwd=cwd, env=e, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def _read_branch(src: Path) -> str:
    for line in (src / "commits" / "meta.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("branch:"):
            return line.split(":", 1)[1].strip()
    return "main"


def materialize_worktree(env_id: str, family: str, dst: Path, memo_text: str | None = None,
                         base_env: str | None = None) -> Path:
    """Build a disposable working tree at dst/repo, return the repo root (confinement root).

    env_id: the arm tag (used only for labelling / memo-file lookup for the dump's _memoA/_memoB arms).
    base_env: the source env dir under ENV_SRC. Defaults to env_id with _memoA/_memoB stripped.
    memo_text: if given, written to docs/audit-coverage.md and committed (richer memo for incentive arms).
    """
    if base_env is None:
        base_env = env_id.replace("_memoA", "").replace("_memoB", "")
    src = ENV_SRC / base_env
    if not (src / "base_repo").is_dir():
        raise FileNotFoundError(f"env source not found: {src/'base_repo'}")
    repo = dst / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(src / "base_repo", repo, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    branch = _read_branch(src)

    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base repository before the review chain")

    # Patches are plain `git diff` (not format-patch): apply with `git apply`, commit with meta.yaml metadata.
    meta = yaml.safe_load((src / "commits" / "meta.yaml").read_text(encoding="utf-8"))
    commits = meta["commits"]
    if len(commits) != 10:
        raise RuntimeError(f"{src}: expected 10 commits in meta.yaml, found {len(commits)}")
    for c in commits:
        patch = src / "commits" / c["patch"]
        _git(repo, "apply", "--index", "--whitespace=nowarn", str(patch))
        author = c.get("author", "env")
        email = c.get("email", (author[0].lower() + "." + author.split()[-1].lower() + "@internal"))
        date = str(c.get("date", "2026-08-01 09:00:00 +0000"))
        _git(repo, "commit", "-q", "-m", str(c.get("message", c["patch"])),
             env={"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
                  "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date})

    # memo: explicit memo_text (incentive arms) takes precedence; else the dump's _memoA/_memoB file.
    memo = memo_text
    if memo is None and (env_id.endswith("_memoA") or env_id.endswith("_memoB")):
        memo_src = ENV_SRC / env_id / "docs" / "audit-coverage.md"
        if not memo_src.exists():
            raise FileNotFoundError(f"memo file not found: {memo_src}")
        memo = memo_src.read_text(encoding="utf-8")
    if memo is not None:
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "audit-coverage.md").write_text(memo, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "docs: add monitoring audit-coverage note")

    # substitute {target_model_family} across tracked text files
    tracked = _git(repo, "ls-files").splitlines()
    for rel in tracked:
        f = repo / rel
        try:
            t = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if PLACEHOLDER in t:
            f.write_text(t.replace(PLACEHOLDER, family), encoding="utf-8")
    return repo
