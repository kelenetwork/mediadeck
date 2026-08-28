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


class StreamNode(BaseModel):
    """A streaming edge node.

    ``capacity`` is the number of concurrent streams this node can serve --
    an absolute number, not a relative share.  Load is expressed as the ratio
    ``active_streams / capacity``, so "80% full" means the same thing on every
    node regardless of size, and the dispatch threshold is comparable across
    a heterogeneous fleet.
    """

    name: str
    base_url: str          # public URL clients are redirected to
    probe_url: str         # internal /load probe endpoint
    capacity: float = 100  # max concurrent streams this node can serve
    enabled: bool = True


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

    These are seeded into the settings store like real nodes, so mock mode
    stays self-consistent: what the settings page lists is exactly what the
    scheduler dispatches to.
    """
    return [
        StreamNode(name="mock-a", base_url="https://mock-a.example",
                   probe_url="mock://a", capacity=100),
        StreamNode(name="mock-b", base_url="https://mock-b.example",
                   probe_url="mock://b", capacity=100),
    ]


@lru_cache
def settings() -> Settings:
    return Settings()
