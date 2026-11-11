"""Configuration loading for the nightly monitor job."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MonitorConfig:
    model: str
    endpoint: str
    prompt_version: str
    max_output_tokens: int = 1024
    temperature: float = 0.0
    request_timeout_s: float = 60.0


@dataclass(frozen=True)
class TranscriptConfig:
    max_transcript_chars: int = 120_000


@dataclass(frozen=True)
class AlertConfig:
    alert_score: int = 7
    page_score: int = 9


@dataclass(frozen=True)
class StoreConfig:
    path: str


@dataclass(frozen=True)
class Config:
    monitor: MonitorConfig
    transcript: TranscriptConfig
    alerts: AlertConfig
    store: StoreConfig


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(
        monitor=MonitorConfig(**raw["monitor"]),
        transcript=TranscriptConfig(**raw.get("transcript", {})),
        alerts=AlertConfig(**raw.get("alerts", {})),
        store=StoreConfig(**raw["store"]),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not cfg.monitor.model:
        raise ValueError("monitor.model must be set")
    if cfg.monitor.max_output_tokens <= 0:
        raise ValueError("monitor.max_output_tokens must be positive")
    if not 1 <= cfg.alerts.alert_score <= cfg.alerts.page_score <= 10:
        raise ValueError("expected 1 <= alerts.alert_score <= alerts.page_score <= 10")
    if cfg.transcript.max_transcript_chars < 1000:
        raise ValueError("transcript.max_transcript_chars is too small")
