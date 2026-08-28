"""Process-level configuration.

Only things that must be known *before* the panel can serve a request live
here: where to store data, whether to run in mock mode, and the panel's own
admin credentials.

Everything an operator is expected to change (Emby connection, streaming
nodes, dispatch policy) belongs in the runtime settings store instead, so it
can be edited from the UI without shell access or a restart.  The env values
below are still read once on first boot to migrate existing deployments.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class NodePool(BaseModel):
    """One media root that a node can serve.

    A real Emby server rarely has a single media root.  This stack has two
    (``/media`` from a union mount and ``/media-gd3`` from a second Drive),
    and a single global "strip this prefix" rule cannot express that: with
    ``strip_prefix=/media`` the path ``/media-gd3/x.mkv`` becomes ``-gd3/x.mkv``
    and the entire second library 404s on the node.

    So mapping is per-root and per-node: ``emby_prefix`` is the path as Emby
    reports it, ``url_prefix`` is where the node serves that same tree.
    """

    name: str                    # "main", "gd3"
    emby_prefix: str             # "/media-gd3" as Emby reports it
    url_prefix: str              # "/s/gd3" as the node serves it
    node_path: str = ""          # "/mnt/gdrive3/Media" — for generated config
    rclone_remote: str = ""      # "gdrive3:Media" — for generated mount unit


class StreamNode(BaseModel):
    """A streaming edge node and everything needed to build and drive it.

    Node-scoped settings live here, not in global settings: the Drive identity,
    cache location and signing key are properties *of a machine*, and two nodes
    routinely differ in all three.
    """

    name: str
    base_url: str          # public URL clients are redirected to
    probe_url: str         # internal /load probe endpoint
    capacity: float = 100  # max concurrent streams this node can serve
    enabled: bool = True

    # -- delivery (per node) -------------------------------------------------
    pools: list[NodePool] = []
    sign_secret: str = ""          # empty -> unsigned (public) URLs
    sign_ttl_seconds: int = 21600
    # nginx secure_link argument names. ca1's production site already uses
    # k/e; hardcoding md5/expires would 403 every request on it.
    sign_arg_digest: str = "k"
    sign_arg_expires: str = "e"

    # -- provisioning inputs (per node) --------------------------------------
    cache_dir: str = "/var/cache/mediadeck"
    cache_size: str = "500G"
    # Drive identity for this node. Held here so enrollment is genuinely
    # one-command: without it the operator would still have to SSH in and run
    # `rclone config`, which is exactly the manual step this replaces.
    rclone_conf: str = ""
    enroll_token: str = ""         # one-shot token for unattended install


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    mediadeck_mock: bool = True
    mediadeck_admin_user: str = "admin"
    mediadeck_admin_password: str = "change-me"
    mediadeck_data_dir: str = "data"

    # Bootstrap-only: seeds the settings store on first run.
    emby_url: str = "http://127.0.0.1:8096"
    emby_api_key: str = ""
    stream_nodes: str = "[]"

    pipeline_snapshot_path: str = ""
    mounts_snapshot_path: str = ""
    tasks_snapshot_path: str = ""

    repo_root: str = ""
    service_name: str = "mediadeck"

    rclone_binary: str = "rclone"
    rclone_config_path: str = ""
    mount_root: str = ""
    cache_root: str = ""
    systemd_unit_dir: str = "/etc/systemd/system"
    systemd_unit_prefix: str = "mediadeck-mount-"

    @property
    def data_dir(self) -> Path:
        return Path(self.mediadeck_data_dir).expanduser()

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def nodes(self) -> list[StreamNode]:
        """Legacy env-defined nodes, used only to seed the settings store."""
        try:
            raw = json.loads(self.stream_nodes or "[]")
        except json.JSONDecodeError:
            return []
        out = []
        for item in raw:
            try:
                out.append(StreamNode(**item))
            except (TypeError, ValueError):
                continue
        return out


def demo_nodes() -> list[StreamNode]:
    """Credential-free demo fleet used when MEDIADECK_MOCK=1.

    Seeded into the settings store like real nodes, so mock mode stays
    self-consistent: what the settings page lists is what the scheduler
    dispatches to.
    """
    pools = [NodePool(name="main", emby_prefix="/media", url_prefix="/s/main")]
    return [
        StreamNode(name="mock-a", base_url="https://mock-a.example",
                   probe_url="mock://a", capacity=100, pools=list(pools)),
        StreamNode(name="mock-b", base_url="https://mock-b.example",
                   probe_url="mock://b", capacity=100, pools=list(pools)),
    ]


@lru_cache
def settings() -> Settings:
    return Settings()
