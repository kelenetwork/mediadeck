"""MoviePilot adapter — the panel's acquisition engine.

The panel is a shell over MoviePilot's REST API (/api/v1/*).  Auth is a
bearer token obtained via the login endpoint; tokens are cached and renewed
on 401.  All endpoints/credentials come from Settings (env) only.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import Settings


class MPError(RuntimeError):
    pass


class LiveMoviePilot:
    def __init__(self, cfg: Settings) -> None:
        self._base = cfg.mp_url.rstrip("/")
        self._user = cfg.mp_username
        self._password = cfg.mp_password
        self._api_token = cfg.mp_api_token
        self._token = ""
        self._token_ts = 0.0

    async def _login(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            f"{self._base}/api/v1/login/access-token",
            data={"username": self._user, "password": self._password},
        )
        if r.status_code != 200:
            raise MPError(f"login failed: HTTP {r.status_code}")
        self._token = r.json().get("access_token", "")
        self._token_ts = time.time()
        if not self._token:
            raise MPError("login returned no token")

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            if self._api_token:
                # Static API-token mode: MoviePilot accepts ?token= on /api/v1.
                params = dict(kw.pop("params", {}) or {})
                params["token"] = self._api_token
                r = await client.request(method, f"{self._base}{path}",
                                         params=params, **kw)
                if r.status_code >= 400:
                    raise MPError(f"{method} {path}: HTTP {r.status_code}")
                return r.json()
            if not self._token or time.time() - self._token_ts > 3600:
                await self._login(client)
            headers = {"Authorization": f"Bearer {self._token}"}
            r = await client.request(method, f"{self._base}{path}", headers=headers, **kw)
            if r.status_code == 401:
                await self._login(client)
                headers = {"Authorization": f"Bearer {self._token}"}
                r = await client.request(method, f"{self._base}{path}", headers=headers, **kw)
            if r.status_code >= 400:
                raise MPError(f"{method} {path}: HTTP {r.status_code}")
            return r.json()

    async def search_media(self, keyword: str) -> list[dict[str, Any]]:
        """TMDB media recognition search (title -> candidates)."""
        data = await self._request("GET", "/api/v1/media/search",
                                   params={"title": keyword})
        out = []
        for m in data or []:
            out.append({
                "title": m.get("title"),
                "year": m.get("year"),
                "type": m.get("type"),
                "tmdb_id": m.get("tmdb_id"),
                "poster": m.get("poster_path"),
                "overview": (m.get("overview") or "")[:200],
            })
        return out

    async def search_torrents(self, keyword: str) -> list[dict[str, Any]]:
        """Site resource search (torrents across indexers)."""
        data = await self._request("GET", "/api/v1/search/title",
                                   params={"keyword": keyword})
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for t in items or []:
            ti = t.get("torrent_info") or t
            meta = t.get("meta_info") or {}
            out.append({
                "title": ti.get("title"),
                "description": (ti.get("description") or "")[:120],
                "site": ti.get("site_name"),
                "size": ti.get("size"),
                "seeders": ti.get("seeders"),
                "enclosure": ti.get("enclosure"),
                "page_url": ti.get("page_url"),
                "resolution": meta.get("resource_pix") or "",
            })
        return out

    async def add_subscribe(self, tmdb_id: int, media_type: str,
                            season: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"tmdbid": tmdb_id, "type": media_type}
        if season is not None:
            body["season"] = season
        data = await self._request("POST", "/api/v1/subscribe/", json=body)
        return {"ok": bool(data.get("success", True)), "message": data.get("message", "")}

    async def list_subscribes(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/subscribe/")
        out = []
        for s in data or []:
            out.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "year": s.get("year"),
                "type": s.get("type"),
                "season": s.get("season"),
                "total_episode": s.get("total_episode"),
                "lack_episode": s.get("lack_episode"),
                "state": s.get("state"),
            })
        return out

    async def delete_subscribe(self, subscribe_id: int) -> bool:
        await self._request("DELETE", f"/api/v1/subscribe/{subscribe_id}")
        return True

    async def download_torrent(self, enclosure: str, title: str) -> dict[str, Any]:
        data = await self._request("POST", "/api/v1/download/add",
                                   json={"enclosure": enclosure, "title": title})
        return {"ok": bool(data.get("success", True)), "message": data.get("message", "")}

    async def downloading(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/download/")
        out = []
        for d in data or []:
            out.append({
                "hash": (d.get("hash") or "")[:12],
                "title": d.get("title") or d.get("name"),
                "progress": d.get("progress"),
                "state": d.get("state"),
                "size": d.get("size"),
                "left_time": d.get("left_time"),
            })
        return out


class MockMoviePilot:
    async def search_media(self, keyword: str) -> list[dict[str, Any]]:
        return [{"title": f"{keyword} Demo Movie", "year": "2026", "type": "电影",
                 "tmdb_id": 12345, "poster": None, "overview": "mock overview"}]

    async def search_torrents(self, keyword: str) -> list[dict[str, Any]]:
        return [{"title": f"{keyword}.2026.2160p.WEB-DL", "description": "mock",
                 "site": "MockSite", "size": 4 * 2**30, "seeders": 12,
                 "enclosure": "mock://torrent/1", "page_url": "mock://page/1",
                 "resolution": "2160p"}]

    async def add_subscribe(self, tmdb_id: int, media_type: str,
                            season: int | None = None) -> dict[str, Any]:
        return {"ok": True, "message": f"subscribed {tmdb_id} {media_type} s{season}"}

    async def list_subscribes(self) -> list[dict[str, Any]]:
        return [{"id": 1, "name": "Demo Show", "year": "2026", "type": "电视剧",
                 "season": 1, "total_episode": 12, "lack_episode": 3, "state": "R"}]

    async def delete_subscribe(self, subscribe_id: int) -> bool:
        return subscribe_id == 1

    async def download_torrent(self, enclosure: str, title: str) -> dict[str, Any]:
        return {"ok": True, "message": "mock download added"}

    async def downloading(self) -> list[dict[str, Any]]:
        return [{"hash": "abc123def456", "title": "Demo Show S01E04", "progress": 42.5,
                 "state": "downloading", "size": 2 * 2**30, "left_time": "00:12:00"}]
