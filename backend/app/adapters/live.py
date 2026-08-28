"""Live adapters. All endpoints/credentials come from Settings (env)."""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings


class LiveEmby:
    def __init__(self, cfg: Settings) -> None:
        self._base = cfg.emby_url.rstrip("/")
        self._headers = {"X-Emby-Token": cfg.emby_api_key}

    async def list_users(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._base}/emby/Users", headers=self._headers)
            r.raise_for_status()
            return r.json()

    async def create_user(self, name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{self._base}/emby/Users/New",
                                  headers=self._headers, json={"Name": name})
            r.raise_for_status()
            return r.json()

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._base}/emby/Users/{user_id}", headers=self._headers)
            if r.status_code != 200:
                return False
            policy = r.json().get("Policy") or {}
            policy["IsDisabled"] = disabled
            pr = await client.post(f"{self._base}/emby/Users/{user_id}/Policy",
                                   headers=self._headers, json=policy)
            return pr.status_code in (200, 204)

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{self._base}/emby/Users/{user_id}/Password",
                headers=self._headers,
                json={"Id": user_id, "NewPw": new_password, "ResetPassword": False},
            )
            return r.status_code in (200, 204)

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._base}/emby/Users/{user_id}", headers=self._headers)
            if r.status_code != 200:
                return False
            policy = r.json().get("Policy") or {}
            policy.update(policy_patch)
            pr = await client.post(f"{self._base}/emby/Users/{user_id}/Policy",
                                   headers=self._headers, json=policy)
            return pr.status_code in (200, 204)

    async def libraries(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self._base}/emby/Library/VirtualFolders",
                                 headers=self._headers)
            r.raise_for_status()
            folders = r.json()
            out = []
            for f in folders:
                item_id = f.get("ItemId") or f.get("Id")
                count = None
                if item_id:
                    cr = await client.get(
                        f"{self._base}/emby/Items",
                        headers=self._headers,
                        params={"ParentId": item_id, "Recursive": "true",
                                "IncludeItemTypes": "Movie,Series", "Limit": "0"},
                    )
                    if cr.status_code == 200:
                        count = cr.json().get("TotalRecordCount")
                out.append({
                    "name": f.get("Name"),
                    "type": f.get("CollectionType") or "mixed",
                    "items": count,
                    "locations": len(f.get("Locations") or []),
                })
            return out

    async def active_sessions(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._base}/emby/Sessions", headers=self._headers)
            r.raise_for_status()
            out = []
            for s in r.json():
                item = s.get("NowPlayingItem")
                if not item:
                    continue
                bitrate = (s.get("TranscodingInfo") or {}).get("Bitrate") or item.get("Bitrate") or 0
                out.append({
                    "UserName": s.get("UserName"),
                    "Client": s.get("Client"),
                    "PlayMethod": (s.get("PlayState") or {}).get("PlayMethod"),
                    "BitrateMbps": round(bitrate / 1e6, 1),
                    "Item": item.get("Name"),
                })
            return out


class LiveProbe:
    async def load(self, probe_url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(probe_url)
                r.raise_for_status()
                data = r.json()
                return {
                    "ok": True,
                    "active_streams": int(data.get("active_streams", 0)),
                    "egress_mbps": float(data.get("egress_mbps", 0.0)),
                }
        except (httpx.HTTPError, ValueError, KeyError):
            return {"ok": False, "active_streams": 0, "egress_mbps": 0.0}
