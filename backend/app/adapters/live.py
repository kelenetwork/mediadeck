"""Live adapters.

Connection details are resolved *per call* from the runtime settings store, so
an operator can point the panel at a different Emby server from the UI and have
it take effect immediately — no restart, no shell access.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import httpx

from app.core.errors import EmbyNotConfigured, UpstreamError

ConfigProvider = Callable[[], dict[str, Any]]


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


async def probe_emby(
    url: str, api_key: str, timeout: float = 15.0, verify_ssl: bool = True
) -> dict[str, Any]:
    """Validate a candidate Emby connection without persisting it.

    Used by the settings UI "test connection" button, so the operator gets a
    concrete answer before saving credentials.
    """
    base = normalize_base_url(url)
    if not base:
        raise EmbyNotConfigured("请填写 Emby 地址")
    if not api_key.strip():
        raise EmbyNotConfigured("请填写 Emby API Key")
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl) as client:
            response = await client.get(
                f"{base}/emby/System/Info", headers={"X-Emby-Token": api_key.strip()}
            )
    except httpx.HTTPError as exc:
        raise UpstreamError(f"无法连接: {exc.__class__.__name__}") from None
    if response.status_code in (401, 403):
        raise UpstreamError("API Key 无效或权限不足")
    if response.status_code != 200:
        raise UpstreamError(f"Emby 返回 HTTP {response.status_code}")
    try:
        info = response.json()
    except ValueError:
        raise UpstreamError("返回内容不是有效 JSON，请确认地址指向 Emby 服务") from None
    return {
        "ok": True,
        "server_name": info.get("ServerName"),
        "version": info.get("Version"),
        "operating_system": info.get("OperatingSystem"),
        "id": info.get("Id"),
    }


class LiveEmby:
    """Emby adapter bound to a settings provider rather than frozen env vars."""

    def __init__(self, config_provider: ConfigProvider) -> None:
        self._config = config_provider

    # -- connection ----------------------------------------------------------
    def _conn(self) -> tuple[str, dict[str, str], float, bool]:
        cfg = self._config() or {}
        base = normalize_base_url(cfg.get("url", ""))
        api_key = (cfg.get("api_key") or "").strip()
        if not cfg.get("enabled"):
            raise EmbyNotConfigured("Emby 集成未启用，请在「系统设置」中连接 Emby")
        if not base or not api_key:
            raise EmbyNotConfigured("Emby 尚未配置，请在「系统设置」中填写地址和 API Key")
        timeout = float(cfg.get("timeout_seconds") or 15)
        verify = bool(cfg.get("verify_ssl", True))
        return base, {"X-Emby-Token": api_key}, timeout, verify

    def _client(self, timeout: float, verify: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, verify=verify)

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        if response.status_code in (401, 403):
            raise UpstreamError("Emby 拒绝了请求：API Key 无效或权限不足")
        if response.status_code >= 500:
            raise UpstreamError(f"Emby 服务异常 (HTTP {response.status_code})")
        return response

    async def system_info(self) -> dict[str, Any]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            try:
                r = self._check(await client.get(f"{base}/emby/System/Info", headers=headers))
            except httpx.HTTPError as exc:
                raise UpstreamError(f"无法连接 Emby: {exc.__class__.__name__}") from None
            r.raise_for_status()
            info = r.json()
        return {
            "ok": True,
            "server_name": info.get("ServerName"),
            "version": info.get("Version"),
            "operating_system": info.get("OperatingSystem"),
            "id": info.get("Id"),
        }

    # -- users ---------------------------------------------------------------
    async def list_users(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Users", headers=headers))
            r.raise_for_status()
            return r.json()

    async def create_user(self, name: str) -> dict[str, Any]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(f"{base}/emby/Users/New", headers=headers, json={"Name": name})
            )
            r.raise_for_status()
            return r.json()

    async def delete_user(self, user_id: str) -> bool:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(f"{base}/emby/Users/{user_id}/Delete", headers=headers)
            )
            return r.status_code in (200, 204)

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        return await self.apply_policy(user_id, {"IsDisabled": disabled})

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(
                await client.post(
                    f"{base}/emby/Users/{user_id}/Password",
                    headers=headers,
                    json={"Id": user_id, "NewPw": new_password, "ResetPassword": False},
                )
            )
            return r.status_code in (200, 204)

    async def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Validate Emby credentials for the public redeem form.

        Uses AuthenticateByName so a wrong password is just a 401, never an
        exception that would leak whether the username exists. The admin API
        key is deliberately omitted: sending it can make Emby skip the password
        check and accept any username.
        """
        if not (username or "").strip() or not password:
            return None
        base, _headers, timeout, verify = self._conn()
        client_auth = (
            'MediaBrowser Client="mediadeck", Device="redeem", '
            'DeviceId="mediadeck-redeem", Version="0.1.0"'
        )
        try:
            async with self._client(timeout, verify) as client:
                r = await client.post(
                    f"{base}/emby/Users/AuthenticateByName",
                    headers={"X-Emby-Authorization": client_auth},
                    json={"Username": username.strip(), "Pw": password},
                )
        except httpx.HTTPError:
            return None
        if r.status_code in (401, 403):
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except ValueError:
            return None
        user = data.get("User") or {}
        if not user.get("Id"):
            return None
        return user

    async def apply_policy(self, user_id: str, policy_patch: dict[str, Any]) -> bool:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Users/{user_id}", headers=headers))
            if r.status_code != 200:
                return False
            policy = r.json().get("Policy") or {}
            policy.update(policy_patch)
            pr = self._check(
                await client.post(
                    f"{base}/emby/Users/{user_id}/Policy", headers=headers, json=policy
                )
            )
            return pr.status_code in (200, 204)

    # -- playback ------------------------------------------------------------
    async def verify_item_access(self, item_id: str, token: str) -> bool:
        """Does the *caller's* own Emby credential grant access to this item?

        The panel sits on the playback path, so it must not become a way around
        Emby's own authentication: without this check anyone could guess an
        item id and be handed a signed media URL with no login at all.

        Asking Emby with the caller's token also covers per-user library
        permissions -- a user who cannot see a library gets no item back, so
        they cannot obtain a link to it.
        """
        base, _, timeout, verify = self._conn()
        token = (token or "").strip()
        if not token:
            return False
        try:
            async with self._client(timeout, verify) as client:
                r = await client.get(
                    f"{base}/emby/Items",
                    headers={"X-Emby-Token": token},
                    params={"Ids": item_id, "Limit": "1"},
                )
        except httpx.HTTPError:
            return False
        if r.status_code != 200:
            return False
        try:
            return bool((r.json() or {}).get("Items"))
        except ValueError:
            return False

    async def item_media_paths(self, item_id: str) -> dict[str, str]:
        """Map MediaSourceId -> on-disk file path for one item.

        This is what lets playback interception key affinity on the actual
        file rather than on the request URL, so every client watching the
        same title converges on the same node.
        """
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(
                f"{base}/emby/Items",
                headers=headers,
                params={"Ids": item_id, "Fields": "MediaSources,Path", "Recursive": "true"},
            ))
            r.raise_for_status()
            items = (r.json() or {}).get("Items") or []
        out: dict[str, str] = {}
        for item in items:
            for source in item.get("MediaSources") or []:
                path = source.get("Path")
                source_id = source.get("Id")
                # Remote/streamed sources have no local file to hand to a node.
                if path and source_id and not str(path).lower().startswith("http"):
                    out[str(source_id)] = str(path)
            if not out and item.get("Path"):
                out[str(item_id)] = str(item["Path"])
        return out

    # -- library -------------------------------------------------------------
    async def libraries(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(max(timeout, 30), verify) as client:
            r = self._check(
                await client.get(f"{base}/emby/Library/VirtualFolders", headers=headers)
            )
            r.raise_for_status()
            out = []
            for folder in r.json():
                item_id = folder.get("ItemId") or folder.get("Id")
                count = None
                if item_id:
                    cr = await client.get(
                        f"{base}/emby/Items",
                        headers=headers,
                        params={
                            "ParentId": item_id,
                            "Recursive": "true",
                            "IncludeItemTypes": "Movie,Series",
                            "Limit": "0",
                        },
                    )
                    if cr.status_code == 200:
                        count = cr.json().get("TotalRecordCount")
                out.append({
                    "id": str(item_id or folder.get("Name") or ""),
                    "name": folder.get("Name"),
                    "type": folder.get("CollectionType") or "mixed",
                    "items": count,
                    "locations": len(folder.get("Locations") or []),
                })
            return out

    async def active_sessions_raw(self) -> list[dict[str, Any]]:
        """Full session objects, unfiltered.

        Usage accounting needs fields the dashboard view drops (UserId,
        DeviceId, PlayState, TranscodingInfo), and device tracking needs to see
        idle sessions too, so it cannot reuse active_sessions().
        """
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Sessions", headers=headers))
            r.raise_for_status()
            return list(r.json() or [])

    async def stop_session(self, session_id: str, reason: str = "") -> bool:
        """End a playback session.

        Disabling an account does not interrupt a stream that already started,
        so a member who exhausts their quota mid-film would otherwise watch it
        to the end. The message is best-effort: not every client renders it.
        """
        if not session_id:
            return False
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            if reason:
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(
                        f"{base}/emby/Sessions/{session_id}/Message",
                        headers=headers,
                        json={"Text": reason, "Header": "mediadeck", "TimeoutMs": 8000},
                    )
            r = await client.post(
                f"{base}/emby/Sessions/{session_id}/Playing/Stop", headers=headers)
            stopped = r.status_code in (200, 204)
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(
                    f"{base}/emby/Sessions/{session_id}", headers=headers)
            return stopped

    async def delete_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = await client.delete(
                f"{base}/emby/Sessions/{session_id}", headers=headers)
            return r.status_code in (200, 204)

    async def sessions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [s for s in await self.active_sessions_raw()
                if s.get("UserId") == user_id]

    async def active_sessions(self) -> list[dict[str, Any]]:
        base, headers, timeout, verify = self._conn()
        async with self._client(timeout, verify) as client:
            r = self._check(await client.get(f"{base}/emby/Sessions", headers=headers))
            r.raise_for_status()
            out = []
            for session in r.json():
                item = session.get("NowPlayingItem")
                if not item:
                    continue
                transcoding = session.get("TranscodingInfo") or {}
                bitrate = transcoding.get("Bitrate") or item.get("Bitrate") or 0
                out.append({
                    "UserId": session.get("UserId"),
                    "UserName": session.get("UserName"),
                    "Client": session.get("Client"),
                    "DeviceName": session.get("DeviceName"),
                    "PlayMethod": (session.get("PlayState") or {}).get("PlayMethod"),
                    "BitrateMbps": round(bitrate / 1e6, 1),
                    "Item": item.get("Name"),
                    "SeriesName": item.get("SeriesName"),
                    "Paused": bool((session.get("PlayState") or {}).get("IsPaused")),
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
