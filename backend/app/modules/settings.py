"""Settings service — the configuration surface of the product.

Scope rule, learned the hard way: a setting lives with the thing it describes.

Global settings are the ones that are true for the whole deployment: which
Emby server to drive, how the front door reaches the panel, and how playback
requests are dispatched.

Everything else belongs to a *node*: which Drive identity it mounts, where its
cache is, which media roots it mirrors, and which key signs its URLs.  Two
nodes routinely differ in all four, so a global "strip this prefix" or a single
global signing key cannot express a real fleet -- and silently breaks it: with
one global prefix, a server with both ``/media`` and ``/media-gd3`` roots has
its entire second library 404 on every node.

Secrets are never returned to the browser in cleartext.  The API exposes a
masked preview plus a ``configured`` flag, and accepts the sentinel ``KEEP``
value on save to mean "leave the stored secret untouched".
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from app.core.config import NodePool, Settings, StreamNode, demo_nodes
from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.modules.signing import MAX_TTL, MIN_TTL, generate_secret

SECRET_UNCHANGED = "__KEEP__"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_NODES = 64
MAX_POOLS = 12

# Playback interception is opt-in: it changes where clients fetch bytes from,
# so it must never switch itself on during an upgrade.
PLAYBACK_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "direct_only": True,
}

# How the operator's existing Emby entrypoint reaches this panel. Needed to
# generate copy-pasteable front-door and node config instead of prose.
INTEGRATION_DEFAULTS: dict[str, Any] = {
    "panel_public_url": "",
    "emby_public_url": "",
}


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


def _abs_path(value: str, field: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value.startswith("/"):
        raise ConfigError(f"{field} 必须是绝对路径（以 / 开头）")
    return value


class SettingsService:
    def __init__(self, store: SettingsStore, scheduler: Any = None) -> None:
        self._store = store
        self._scheduler = scheduler

    def bind_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    # -- bootstrap -----------------------------------------------------------
    def bootstrap_from_env(self, cfg: Settings) -> bool:
        """Seed the store from .env on first run only."""
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
        self._store.set_section("playback", dict(PLAYBACK_DEFAULTS), persist=False)
        self._store.set_section("integration", dict(INTEGRATION_DEFAULTS), persist=False)
        seed = cfg.nodes() or (demo_nodes() if cfg.mediadeck_mock else [])
        self._store.set("nodes", [n.model_dump() for n in seed], persist=False)
        self._store.save()
        return True

    # -- emby ----------------------------------------------------------------
    def emby_config(self) -> dict[str, Any]:
        section = self._store.section("emby")
        return {
            "enabled": bool(section.get("enabled")),
            "url": section.get("url") or "",
            "api_key": section.get("api_key") or "",
            "verify_ssl": bool(section.get("verify_ssl", True)),
            "timeout_seconds": section.get("timeout_seconds") or 15,
        }

    def emby_public(self) -> dict[str, Any]:
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
            "enabled": enabled, "url": url, "api_key": api_key,
            "verify_ssl": bool(payload.get("verify_ssl", current["verify_ssl"])),
            "timeout_seconds": timeout,
        })
        return self.emby_public()

    def resolve_probe_target(self, payload: dict[str, Any]) -> tuple[str, str, float, bool]:
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

    # -- playback ------------------------------------------------------------
    def playback_config(self) -> dict[str, Any]:
        section = self._store.section("playback")
        cfg = dict(PLAYBACK_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = bool(section[key])
        return cfg

    def save_playback(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.playback_config()
        enabled = bool(payload.get("enabled", current["enabled"]))
        if enabled:
            nodes = self.nodes()
            if not nodes:
                raise ConfigError("启用播放分流前必须至少配置一个推流节点")
            if not any(n.pools for n in nodes):
                raise ConfigError("启用前请先给节点配置媒体根映射（节点详情 → 媒体根）")
        self._store.set_section("playback", {
            "enabled": enabled,
            "direct_only": bool(payload.get("direct_only", current["direct_only"])),
        })
        return self.playback_config()

    # -- integration ---------------------------------------------------------
    def integration_config(self) -> dict[str, Any]:
        section = self._store.section("integration")
        cfg = dict(INTEGRATION_DEFAULTS)
        for key in cfg:
            if key in section:
                cfg[key] = str(section[key] or "")
        return cfg

    def save_integration(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.integration_config()
        panel = str(payload.get("panel_public_url", current["panel_public_url"]) or "").strip()
        emby = str(payload.get("emby_public_url", current["emby_public_url"]) or "").strip()
        if panel:
            panel = _require_http_url(panel, "面板对外地址")
        if emby:
            emby = _require_http_url(emby, "Emby 对外地址")
        self._store.set_section("integration", {
            "panel_public_url": panel, "emby_public_url": emby,
        })
        return self.integration_config()

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

    def node(self, name: str) -> StreamNode | None:
        return next((n for n in self.nodes() if n.name == name), None)

    @staticmethod
    def node_public(node: StreamNode) -> dict[str, Any]:
        """Node view for the UI: never leaks the signing key or enroll token."""
        data = node.model_dump()
        secret = data.pop("sign_secret", "") or ""
        rclone_conf = data.pop("rclone_conf", "") or ""
        data.pop("enroll_token", None)
        data["sign_secret_set"] = bool(secret)
        data["sign_secret_masked"] = mask_secret(secret)
        # The Drive config contains OAuth tokens; only report whether it is set.
        data["rclone_conf_set"] = bool(rclone_conf.strip())
        return data

    def nodes_public(self) -> list[dict[str, Any]]:
        return [self.node_public(n) for n in self.nodes()]

    def _persist_nodes(self, nodes: list[StreamNode]) -> None:
        self._store.set("nodes", [n.model_dump() for n in nodes])
        if self._scheduler:
            self._scheduler.reconfigure(nodes)

    @staticmethod
    def _validate_pools(raw: Any, existing: list[NodePool]) -> list[NodePool]:
        if raw is None:
            return existing
        if not isinstance(raw, list):
            raise ConfigError("媒体根必须是列表")
        if len(raw) > MAX_POOLS:
            raise ConfigError(f"媒体根数量不能超过 {MAX_POOLS}")
        out: list[NodePool] = []
        seen_prefix: set[str] = set()
        seen_name: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ConfigError("媒体根格式错误")
            name = str(item.get("name") or "").strip()
            if not NAME_RE.match(name):
                raise ConfigError("媒体根名称只能包含字母、数字、点、下划线和连字符")
            if name in seen_name:
                raise ConfigError(f"媒体根名称重复: {name}")
            emby_prefix = _abs_path(str(item.get("emby_prefix") or ""), "Emby 路径前缀")
            if emby_prefix in seen_prefix:
                raise ConfigError(f"Emby 路径前缀重复: {emby_prefix}")
            url_prefix = str(item.get("url_prefix") or "").strip().rstrip("/")
            if not url_prefix.startswith("/"):
                raise ConfigError("节点 URL 前缀必须以 / 开头")
            node_path = str(item.get("node_path") or "").strip().rstrip("/")
            if node_path and not node_path.startswith("/"):
                raise ConfigError("节点本地路径必须是绝对路径")
            seen_name.add(name)
            seen_prefix.add(emby_prefix)
            out.append(NodePool(
                name=name, emby_prefix=emby_prefix, url_prefix=url_prefix,
                node_path=node_path,
                rclone_remote=str(item.get("rclone_remote") or "").strip(),
            ))
        return out

    def _validate_node(self, payload: dict[str, Any],
                       existing: StreamNode | None = None) -> StreamNode:
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

        pools = self._validate_pools(
            payload.get("pools"),
            [NodePool(**p) for p in (base.get("pools") or [])],
        )

        secret = payload.get("sign_secret", SECRET_UNCHANGED)
        if secret == SECRET_UNCHANGED or secret is None:
            secret = base.get("sign_secret", "")
        secret = str(secret).strip()

        try:
            ttl = int(payload.get("sign_ttl_seconds", base.get("sign_ttl_seconds", 21600)))
        except (TypeError, ValueError):
            raise ConfigError("链接有效期必须是数字") from None
        if not MIN_TTL <= ttl <= MAX_TTL:
            raise ConfigError(f"链接有效期必须在 {MIN_TTL}–{MAX_TTL} 秒之间")

        return StreamNode(
            name=name, base_url=base_url, probe_url=probe_url, capacity=capacity,
            enabled=bool(payload.get("enabled", base.get("enabled", True))),
            pools=pools,
            sign_secret=secret,
            sign_ttl_seconds=ttl,
            sign_arg_digest=str(payload.get(
                "sign_arg_digest", base.get("sign_arg_digest", "md5")) or "md5").strip(),
            sign_arg_expires=str(payload.get(
                "sign_arg_expires", base.get("sign_arg_expires", "expires")) or "expires").strip(),
            cache_dir=str(payload.get(
                "cache_dir", base.get("cache_dir", "/var/cache/mediadeck"))).strip(),
            cache_size=str(payload.get("cache_size", base.get("cache_size", "500G"))).strip(),
            # Drive identity travels with the node: without it the installer
            # would still need `rclone config` run by hand on the target,
            # which is exactly the manual step one-command enrollment removes.
            rclone_conf=str(payload.get(
                "rclone_conf", base.get("rclone_conf", "")) or ""),
            enroll_token=str(base.get("enroll_token", "")),
        )

    def add_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self.nodes()
        if len(nodes) >= MAX_NODES:
            raise ConfigError(f"节点数量已达上限（{MAX_NODES}）")
        node = self._validate_node(payload)
        if any(n.name == node.name for n in nodes):
            raise ConfigError(f"节点名称已存在: {node.name}")
        # A node with no signing key would hand out permanent public links;
        # generate one up front so the installer can bake it in.
        if not node.sign_secret:
            node.sign_secret = generate_secret()
        node.enroll_token = secrets.token_urlsafe(24)
        nodes.append(node)
        self._persist_nodes(nodes)
        return self.node_public(node)

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
        return self.node_public(updated)

    def delete_node(self, name: str) -> bool:
        nodes = self.nodes()
        remaining = [n for n in nodes if n.name != name]
        if len(remaining) == len(nodes):
            raise KeyError(name)
        self._persist_nodes(remaining)
        return True

    def rotate_node_secret(self, name: str) -> dict[str, Any]:
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        nodes[index].sign_secret = generate_secret()
        self._persist_nodes(nodes)
        return self.node_public(nodes[index])

    # -- enrollment ----------------------------------------------------------
    def node_by_enroll_token(self, token: str) -> StreamNode | None:
        token = (token or "").strip()
        if not token:
            return None
        for node in self.nodes():
            if node.enroll_token and secrets.compare_digest(node.enroll_token, token):
                return node
        return None

    def node_enroll_token(self, name: str) -> str:
        """Return (creating if needed) the one-shot install token for a node."""
        nodes = self.nodes()
        index = next((i for i, n in enumerate(nodes) if n.name == name), None)
        if index is None:
            raise KeyError(name)
        if not nodes[index].enroll_token:
            nodes[index].enroll_token = secrets.token_urlsafe(24)
            self._persist_nodes(nodes)
        return nodes[index].enroll_token
