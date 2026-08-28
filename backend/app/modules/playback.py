"""Emby playback interception — the piece that makes multi-node real.

Without this module the scheduler is decorative: it can pick a node, but no
actual Emby client ever asks it.  Emby hands clients a stream URL pointing at
the Emby host, and every byte is served from there.

The flow this implements:

    client --> GET /emby/Videos/{ItemId}/stream.mkv?...  (points at us)
      1. is this a direct play/stream request?  (transcodes must NOT move)
      2. ask Emby which file backs this item + media source
      3. use that *file path* as the affinity key -> scheduler picks a node
      4. 302 the client to that node's copy of the file

Two design rules matter more than anything else here:

**The affinity key is the media path, not the request URL.**  Two clients
playing the same movie send different session ids, api keys and container
extensions.  Keying on the URL would scatter one file across every node and
defeat the cache locality the scheduler exists to create.

**Fail open, always.**  Any uncertainty — feature off, transcode, unknown
item, no healthy node, Emby unreachable — falls back to serving from Emby
itself.  A panel bug must never turn into "playback is broken"; the worst
acceptable outcome is "playback did not get accelerated this time".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

# Path fragments that mean "Emby is transcoding this".  Transcoded output is
# produced on the Emby host and does not exist on a node, so these must always
# be served by Emby regardless of policy.
TRANSCODE_MARKERS = (
    "master.m3u8", "main.m3u8", "manifest", "hls", "live.m3u8",
    "transcod", "/segments/", ".ts", "dash", ".mpd",
)


@dataclass
class Decision:
    """Outcome of one interception, kept explicit so it can be logged/tested."""

    redirected: bool
    target: str
    reason: str
    node: str | None = None
    media_path: str | None = None


class TTLCache:
    """Tiny TTL cache for item -> path lookups.

    Every stream request would otherwise hit the Emby API; a popular title
    starting on 50 clients must not become 50 metadata calls.
    """

    def __init__(self, ttl: float = 300.0, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if not entry:
            return None
        expires, value = entry
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self._max:
            # Cheap eviction: drop whatever is already expired, else clear.
            now = time.time()
            self._data = {k: v for k, v in self._data.items() if v[0] >= now}
            if len(self._data) >= self._max:
                self._data.clear()
        self._data[key] = (time.time() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()


def is_transcode_request(path: str, query: dict[str, str]) -> bool:
    lowered = path.lower()
    if any(marker in lowered for marker in TRANSCODE_MARKERS):
        return True
    # Emby marks true direct play with Static=true; its absence on a /stream
    # request generally means the server intends to remux or transcode.
    static = (query.get("Static") or query.get("static") or "").lower()
    return static not in ("true", "1")


def map_media_path(media_path: str, strip_prefix: str, template: str) -> str:
    """Translate an Emby-side file path into the node-side relative path.

    Emby sees ``/media/Movies/CN/title.mkv``; a node may expose the same file
    under a different root.  The operator configures the prefix to strip and,
    if needed, a template for what to put in front.
    """
    path = media_path.replace("\\", "/")
    prefix = (strip_prefix or "").replace("\\", "/").rstrip("/")
    if prefix and path.startswith(prefix):
        path = path[len(prefix):]
    path = path.lstrip("/")
    if template and template != "{path}":
        path = template.replace("{path}", path)
    return path.lstrip("/")


def build_node_url(base_url: str, relative_path: str) -> str:
    encoded = quote(relative_path, safe="/")
    return f"{base_url.rstrip('/')}/{encoded}"


class PlaybackRouter:
    def __init__(self, emby: Any, scheduler: Any, config_provider: Any,
                 emby_config_provider: Any) -> None:
        self._emby = emby
        self._scheduler = scheduler
        self._config = config_provider
        self._emby_config = emby_config_provider
        self._cache = TTLCache()
        self._log: list[dict[str, Any]] = []

    # -- helpers -------------------------------------------------------------
    def _origin(self) -> str:
        return (self._emby_config() or {}).get("url", "").rstrip("/")

    def _passthrough(self, request_path: str, query: dict[str, str], reason: str) -> Decision:
        origin = self._origin()
        target = f"{origin}/{request_path.lstrip('/')}" if origin else request_path
        if query:
            target = f"{target}?{urlencode(query)}"
        return Decision(redirected=False, target=target, reason=reason)

    def _record(self, decision: Decision, item_id: str) -> None:
        self._log.append({
            "ts": time.time(),
            "item_id": item_id,
            "redirected": decision.redirected,
            "node": decision.node,
            "reason": decision.reason,
            "media_path": decision.media_path,
        })
        if len(self._log) > 500:
            del self._log[:-500]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._log[-max(1, min(limit, 500)):]

    def invalidate(self) -> None:
        self._cache.clear()

    async def _media_path(self, item_id: str, media_source_id: str | None) -> str | None:
        cache_key = f"{item_id}:{media_source_id or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached or None
        try:
            sources = await self._emby.item_media_paths(item_id)
        except Exception:  # noqa: BLE001 - fail open, never break playback
            return None
        path = None
        if media_source_id and media_source_id in sources:
            path = sources[media_source_id]
        elif sources:
            path = next(iter(sources.values()))
        # Cache negatives too, so a bad item id cannot hammer Emby.
        self._cache.set(cache_key, path or "")
        return path

    # -- main entry ----------------------------------------------------------
    async def route(self, item_id: str, request_path: str,
                    query: dict[str, str]) -> Decision:
        cfg = self._config() or {}

        if not cfg.get("enabled"):
            decision = self._passthrough(request_path, query, "disabled")
            return decision

        if cfg.get("direct_only", True) and is_transcode_request(request_path, query):
            decision = self._passthrough(request_path, query, "transcode")
            self._record(decision, item_id)
            return decision

        media_source_id = query.get("MediaSourceId") or query.get("mediaSourceId")
        media_path = await self._media_path(item_id, media_source_id)
        if not media_path:
            decision = self._passthrough(request_path, query, "unresolved-item")
            self._record(decision, item_id)
            return decision

        # Affinity key: the file, so every client of this title lands together.
        chosen = self._scheduler.pick(context=media_path)
        if not chosen:
            decision = self._passthrough(request_path, query, "no-node")
            decision.media_path = media_path
            self._record(decision, item_id)
            return decision

        relative = map_media_path(
            media_path,
            cfg.get("strip_prefix", ""),
            cfg.get("path_template", "{path}"),
        )
        if not relative:
            decision = self._passthrough(request_path, query, "empty-mapped-path")
            decision.media_path = media_path
            self._record(decision, item_id)
            return decision

        target = build_node_url(chosen.node.base_url, relative)
        decision = Decision(
            redirected=True,
            target=target,
            reason="redirected",
            node=chosen.node.name,
            media_path=media_path,
        )
        self._record(decision, item_id)
        return decision
