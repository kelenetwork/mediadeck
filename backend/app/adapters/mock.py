"""Mock adapters: full panel functionality with zero real credentials."""
from __future__ import annotations

import random
import time
from typing import Any


class MockEmby:
    def __init__(self) -> None:
        self._users: dict[str, dict[str, Any]] = {
            "u1": {"Id": "u1", "Name": "demo-user-1", "Policy": {"IsDisabled": False}},
            "u2": {"Id": "u2", "Name": "demo-user-2", "Policy": {"IsDisabled": True}},
        }
        self._next = 3

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._users.values())

    async def create_user(self, name: str) -> dict[str, Any]:
        uid = f"u{self._next}"
        self._next += 1
        user = {"Id": uid, "Name": name, "Policy": {"IsDisabled": False}}
        self._users[uid] = user
        return user

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"]["IsDisabled"] = disabled
        return True

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        return user_id in self._users

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"].update(policy_patch)
        return True

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
