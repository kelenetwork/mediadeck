"""Runtime configuration. Everything comes from environment / .env.

The repository never contains real endpoints, tokens or paths.
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class StreamNode(BaseModel):
    name: str
    base_url: str          # public URL clients are redirected to
    probe_url: str         # internal /load probe endpoint
    weight: float = 1.0    # relative dispatch weight (e.g. uplink capacity)
    enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    mediadeck_mock: bool = True
    mediadeck_admin_user: str = "admin"
    mediadeck_admin_password: str = "change-me"

    emby_url: str = "http://127.0.0.1:8096"
    emby_api_key: str = ""


    stream_nodes: str = "[]"

    pipeline_snapshot_path: str = ""

    repo_root: str = ""
    service_name: str = "mediadeck"

    def nodes(self) -> list[StreamNode]:
        try:
            raw = json.loads(self.stream_nodes or "[]")
        except json.JSONDecodeError:
            return []
        return [StreamNode(**item) for item in raw]


@lru_cache
def settings() -> Settings:
    return Settings()
