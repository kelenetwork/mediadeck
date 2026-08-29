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
            "admin": {"Id": "admin", "Name": "demo-admin",
                      "Policy": {"IsDisabled": False, "IsAdministrator": True}},
        }
        self._next = 3
        self._sessions: list[dict[str, Any]] = []
        self.stopped: list[tuple[str, str]] = []
        self.deleted_sessions: list[str] = []
        # username -> password; missing means any non-empty password is accepted
        # so existing tests that never set a password still work.
        self._passwords: dict[str, str] = {}

    async def system_info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "server_name": "demo-emby",
            "version": "4.8.0.0",
            "operating_system": "Linux",
            "id": "mock-server",
        }

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self._users.values())

    async def create_user(self, name: str) -> dict[str, Any]:
        uid = f"u{self._next}"
        self._next += 1
        user = {"Id": uid, "Name": name, "Policy": {"IsDisabled": False}}
        self._users[uid] = user
        return user

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"]["IsDisabled"] = disabled
        return True

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        self._passwords[user.get("Name") or ""] = new_password
        return True

    async def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the user dict on success, None on bad credentials.

        Never raises on a wrong password: the caller maps None to a 422 so a
        public redeem form cannot distinguish 'no such user' from 'bad password'.
        """
        if not username or not password:
            return None
        user = next((u for u in self._users.values()
                     if (u.get("Name") or "").lower() == username.lower()), None)
        if not user:
            return None
        expected = self._passwords.get(user.get("Name") or "")
        if expected is None:
            return dict(user)
        if expected != password:
            return None
        return dict(user)

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["Policy"].update(policy_patch)
        return True

    async def verify_item_access(self, item_id: str, token: str) -> bool:
        # Mirrors the live adapter: only a non-empty token is ever accepted.
        return bool((token or "").strip()) and token != "invalid-token"

    async def user_for_token(self, token: str) -> str | None:
        """Deterministic mock: "tok:<uid>" resolves to that user, anything
        else non-empty resolves to u1 so existing playback tests keep working."""
        token = (token or "").strip()
        if not token or token == "invalid-token":
            return None
        if token.startswith("tok:"):
            uid = token[4:]
            return uid if uid in self._users else None
        return "u1"

    async def item_media_paths(self, item_id: str) -> dict[str, str]:
        if item_id == "unknown":
            return {}
        return {
            f"src-{item_id}": f"/media/Movies/Demo/{item_id}.mkv",
            f"src-{item_id}-alt": f"/media/Movies/Demo/{item_id}.alt.mkv",
        }

    async def libraries(self) -> list[dict[str, Any]]:
        return [
            {"id": "lib-movies", "name": "demo-movies", "type": "movies",
             "items": 120, "locations": 2},
            {"id": "lib-series", "name": "demo-series", "type": "tvshows",
             "items": 340, "locations": 3},
        ]

    async def active_sessions_raw(self) -> list[dict[str, Any]]:
        """Full session objects, shaped like Emby's, for usage accounting.

        Injectable so tests can drive the sampler deterministically instead of
        depending on random demo data.
        """
        return list(self._sessions)

    def set_sessions(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = list(sessions)

    async def stop_session(self, session_id: str, reason: str = "") -> bool:
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.get("Id") != session_id]
        self.stopped.append((session_id, reason))
        return len(self._sessions) < before

    async def delete_session(self, session_id: str) -> bool:
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.get("Id") != session_id]
        self.deleted_sessions.append(session_id)
        return len(self._sessions) < before

    async def sessions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [s for s in self._sessions if s.get("UserId") == user_id]

    async def active_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "Id": "mock-session-1",
                "UserId": "u1",
                "UserName": "demo-user-1",
                "Client": "Demo Player",
                "PlayMethod": "DirectStream",
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
