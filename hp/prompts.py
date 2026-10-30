"""Prompt registry: one file per prompt under prompts/, described by prompts/registry.yaml.

A prompt is loaded by id; its file bytes must hash to the registry's sha256, otherwise
we refuse to run (an edit is a new id/version, never a silent change).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import HP_ROOT

PROMPTS_DIR = HP_ROOT / "prompts"
REGISTRY_FILE = PROMPTS_DIR / "registry.yaml"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PromptHashMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    kind: str
    text: str
    sha256: str
    source: str
    meta: dict


class Registry:
    def __init__(self, registry_file: Path = REGISTRY_FILE):
        self.registry_file = Path(registry_file)
        self.dir = self.registry_file.parent
        raw = yaml.safe_load(self.registry_file.read_text(encoding="utf-8"))
        self.raw = raw
        self.entries: dict[str, dict] = {e["id"]: e for e in raw["prompts"]}
        self._cache: dict[str, Prompt] = {}

    def ids(self) -> list[str]:
        return list(self.entries)

    def get(self, pid: str) -> Prompt:
        if pid in self._cache:
            return self._cache[pid]
        if pid not in self.entries:
            raise KeyError(f"prompt id {pid!r} not in {self.registry_file}")
        e = self.entries[pid]
        data = (self.dir / e["file"]).read_bytes()
        h = sha256_bytes(data)
        if h != e["sha256"]:
            raise PromptHashMismatch(
                f"prompt {pid!r}: file sha256 {h} != registry {e['sha256']} "
                f"({self.dir / e['file']}). Edits require a new id/version."
            )
        meta = {k: v for k, v in e.items() if k not in {"id", "version", "kind", "file", "sha256", "source"}}
        p = Prompt(id=pid, version=int(e.get("version", 1)), kind=e["kind"], text=data.decode("utf-8"),
                   sha256=h, source=e["source"], meta=meta)
        self._cache[pid] = p
        return p

    def verify_all(self) -> dict[str, str]:
        """Load every prompt (raises on any mismatch); returns {id: sha256}."""
        return {pid: self.get(pid).sha256 for pid in self.entries}

    def hashes(self) -> dict[str, str]:
        return {pid: e["sha256"] for pid, e in self.entries.items()}


_default: Registry | None = None


def registry() -> Registry:
    global _default
    if _default is None:
        _default = Registry()
    return _default


def load(pid: str) -> Prompt:
    return registry().get(pid)
