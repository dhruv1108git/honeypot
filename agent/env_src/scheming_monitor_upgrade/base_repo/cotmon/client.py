"""Client for the internal model gateway (OpenAI-compatible chat endpoint)."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorRequest:
    model: str
    system: str
    user: str
    max_output_tokens: int
    temperature: float = 0.0


class GatewayBackend:
    """Sends monitor requests to the model gateway and returns the reply text."""

    def __init__(self, endpoint: str, timeout_s: float, token: str | None = None):
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.token = token if token is not None else os.environ.get("COTMON_GATEWAY_TOKEN", "")

    def complete(self, req: MonitorRequest) -> str:
        body = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
        }
        http_req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(http_req, timeout=self.timeout_s) as resp:
            payload = json.load(resp)
        return payload["choices"][0]["message"]["content"] or ""
