"""Settings service — the configuration surface of the product.

Everything an Emby operator is expected to change lives here and is editable
from the panel UI: which Emby server to drive, which streaming nodes exist,
and how playback requests are dispatched across them.  Changes are persisted
and applied to the running process immediately; no restart, no shell.

Secrets (API keys) are never returned to the browser in cleartext.  The API
exposes a masked preview plus a ``configured`` flag, and accepts the sentinel
``KEEP`` value on save to mean "leave the stored secret untouched" — so an
operator can edit the URL without re-typing the key.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.config import Settings, StreamNode
from app.core.errors import ConfigError
from app.core.store import SettingsStore

SECRET_UNCHANGED = "__KEEP__"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_NODES = 64


def mask_secret(value: str) -> str:
    """Show enough to recognise a key, never enough to use it."""
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def _require_http_url(value: str, field: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        raise ConfigError(f"{field} 不能为空")
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"{field} 必须以 http:// 或 https:// 开头")
    return value


class SettingsService:
    def __init__(self, store: SettingsStore, scheduler: Any = None) -> None:
        self._store = store
        self._scheduler = scheduler

    def bind_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    # -- bootstrap -----------------------------------------------------------
    def bootstrap_from_env(self, cfg: Settings) -> bool:
        """Seed the store from .env on first run only.

        Keeps existing env-configured deployments working across the upgrade,
        while all later edits go through the UI.  Returns True if seeded.
        """
        if self._store.loaded_from_disk:
            return False
        api_key = (cfg.emby_api_key or "").strip()
        url = (cfg.emby_url or "").strip().rstrip("/")
        self._store.set_section("emby", {
            "enabled": bool(api_key and url),
            "url": url,
            "api_key": api_key,
            "verify_ssl": True,
            "timeout_seconds": 15,
        }, persist=False)
        self._store.set_section("dispatch", {
            "policy": "affinity",
            "load_threshold": 0.8,
        }, persist=False)
        self._store.set("nodes", [n.model_dump() for n in cfg.nodes()], persist=False)
        self._store.save()
        return True

    # -- emby ----------------------------------------------------------------
    def emby_config(self) -> dict[str, Any]:
        """Raw config for internal adapter use (includes the secret)."""
        section = self._store.section("emby")
        return {
            "enabled": bool(section.get("enabled")),
            "url": section.get("url") or "",
            "api_key": section.get("api_key") or "",
            "verify_ssl": bool(section.get("verify_ssl", True)),
            "timeout_seconds": section.get("timeout_seconds") or 15,
        }

    def emby_public(self) -> dict[str, Any]:
        """Safe-to-render config for the settings UI."""
        cfg = self.emby_config()
        return {
            "enabled": cfg["enabled"],
            "url": cfg["url"],
            "api_key_masked": mask_secret(cfg["api_key"]),
            "api_key_set": bool(cfg["api_key"]),
            "verify_ssl": cfg["verify_ssl"],
            "timeout_seconds": cfg["timeout_seconds"],
        }

    def save_emby(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.emby_config()
        url = _require_http_url(payload.get("url", current["url"]), "Emby 地址")

        api_key = payload.get("api_key", SECRET_UNCHANGED)
        if api_key == SECRET_UNCHANGED or api_key is None:
            api_key = current["api_key"]
        api_key = str(api_key).strip()

        enabled = bool(payload.get("enabled", current["enabled"]))
        if enabled and not api_key:
            raise ConfigError("启用 Emby 集成前必须填写 API Key")

        try:
            timeout = float(payload.get("timeout_seconds", current["timeout_seconds"]))
        except (TypeError, ValueError):
            raise ConfigError("超时时间必须是数字") from None
        if not 1 <= timeout <= 120:
            raise ConfigError("超时时间必须在 1–120 秒之间")

        self._store.set_section("emby", {
            "enabled": enabled,
            "url": url,
            "api_key": api_key,
            "verify_ssl": bool(payload.get("verify_ssl", current["verify_ssl"])),
            "timeout_seconds": timeout,
        })
        return self.emby_public()

    def resolve_probe_target(self, payload: dict[str, Any]) -> tuple[str, str, float, bool]:
        """Resolve a 'test connection' request against stored + submitted values."""
        current = self.emby_config()
        url = _require_http_url(payload.get("url") or current["url"], "Emby 地址")
        api_key = payload.get("api_key", SECRET_UNCHANGED)
        if api_key == SECRET_UNCHANGED or api_key is None or api_key == "":
            api_key = current["api_key"]
        timeout = float(payload.get("timeout_seconds") or current["timeout_seconds"])
        verify = bool(payload.get("verify_ssl", current["verify_ssl"]))
        return url, str(api_key).strip(), timeout, verify

    # -- dispatch policy -----------------------------------------------------
    def dispatch_config(self) -> dict[str, Any]:
        section = self._store.section("dispatch")
        policy = section.get("policy")
        if policy not in ("affinity", "least-load"):
            policy = "affinity"
        try:
            threshold = float(section.get("load_threshold", 0.8))
        except (TypeError, ValueError):
            threshold = 0.8
        return {"policy": policy, "load_threshold": threshold}

    def save_dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.dispatch_config()
        policy = payload.get("policy", current["policy"])
        if policy not in ("affinity", "least-load"):
            raise ConfigError("调度策略必须是 affinity 或 least-load")
        try:
            threshold = float(payload.get("load_threshold", current["load_threshold"]))
        except (TypeError, ValueError):
            raise ConfigError("负载阈值必须是数字") from None
        if not 0 < threshold <= 100:
            raise ConfigError("负载阈值必须大于 0")
        self._store.set_section("dispatch", {"policy": policy, "load_threshold": threshold})
        if self._scheduler:
            self._scheduler.set_policy(policy, threshold)
        return self.dispatch_config()

    # -- nodes ---------------------------------------------------------------
    def nodes(self) -> list[StreamNode]:
        raw = self._store.get("nodes", []) or []
        out: list[StreamNode] = []
        for item in raw:
            try:
                out.append(StreamNode(**item))
            except (TypeError, ValueError):
                continue
        return out

    def nodes_public(self) -> list[dict[str, Any]]:
        return [n.model_dump() for n in self.nodes()]

    def _persist_nodes(self, nodes: list[StreamNode]) -> None:
        self._store.set("nodes", [n.model_dump() for n in nodes])
        if self._scheduler:
            self._scheduler.reconfigure(nodes)

    @staticmethod
    def _validate_node(payload: dict[str, Any], existing: StreamNode | None = None) -> StreamNode:
        base = existing.model_dump() if existing else {}
        name = str(payload.get("name", base.get("name", ""))).strip()
        if not NAME_RE.match(name):
            raise ConfigError("节点名称只能包含字母、数字、点、下划线和连字符（1–40 字符）")
        base_url = _require_http_url(payload.get("base_url", base.get("base_url", "")), "节点地址")
        probe_url = _require_http_url(payload.get("probe_url", base.get("probe_url", "")), "探针地址")
        try:
            capacity = float(payload.get("capacity", base.get("capacity", 100)))
        except (TypeError, ValueError):
            raise ConfigError("并发容量必须是数字") from None
        if not 1 <= capacity <= 100000:
            raise ConfigError("并发容量必须在 1–100000 之间")
        return StreamNode(
            name=name,
            base_url=base_url,
            probe_url=probe_url,
            capacity=capacity,
            enabled=bool(payload.get("enabled", base.get("enabled", True))),
        )

    def add_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self.nodes()
        if len(nodes) >= MAX_NODES:
            raise ConfigError(f"节点数量已达上限（{MAX_NODES}）")
        node = self._validate_node(payload)
        if any(n.name == node.name for n in nodes):
            raise ConfigError(f"节点名称已存在: {node.name}")
        nodes.append(node)
        self._persist_nodes(nodes)
        return node.model_dump()

    def update_node(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        updated = self._validate_node(payload, existing=nodes[index])
        if updated.name != name and any(n.name == updated.name for n in nodes):
            raise ConfigError(f"节点名称已存在: {updated.name}")
        nodes[index] = updated
        self._persist_nodes(nodes)
        return updated.model_dump()

    def delete_node(self, name: str) -> bool:
        nodes = self.nodes()
        remaining = [n for n in nodes if n.name != name]
        if len(remaining) == len(nodes):
            raise KeyError(name)
        self._persist_nodes(remaining)
        return True
