"""Mock adapters: full panel functionality with zero real credentials."""
from __future__ import annotations

import random
import time
from typing import Any


class MockEmby:
    async def list_users(self) -> list[dict[str, Any]]:
        return [
            {"Id": "u1", "Name": "demo-user-1", "Policy": {"IsDisabled": False}},
            {"Id": "u2", "Name": "demo-user-2", "Policy": {"IsDisabled": True}},
        ]

    async def active_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "UserName": "demo-user-1",
                "Client": "Demo Player",
                "PlayMethod": "DirectStream",
                "BitrateMbps": round(random.uniform(3, 20), 1),
                "Item": "Demo Show S01E01",
            }
        ]


class MockProbe:
    """Simulates per-node /load probes with drifting load values."""

    def __init__(self) -> None:
        self._seed = time.monotonic()

    async def load(self, probe_url: str) -> dict[str, Any]:
        jitter = (hash(probe_url) % 100) / 10
        t = time.monotonic() - self._seed
        return {
            "ok": True,
            "active_streams": int(5 + jitter + 3 * abs(__import__("math").sin(t / 30))),
            "egress_mbps": round(50 + jitter * 20 + random.uniform(-5, 5), 1),
        }
