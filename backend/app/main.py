"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.adapters.live import LiveEmby, LiveProbe
from app.adapters.mock import MockEmby, MockProbe
from app.core.config import settings
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.scheduler import Scheduler

app = FastAPI(title="mediadeck", version="0.1.0")
security = HTTPBasic()


def _auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:  # noqa: B008
    cfg = settings()
    user_ok = secrets.compare_digest(credentials.username, cfg.mediadeck_admin_user)
    pass_ok = secrets.compare_digest(credentials.password, cfg.mediadeck_admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.on_event("startup")
async def _startup() -> None:
    cfg = settings()
    if cfg.mediadeck_mock:
        app.state.emby = MockEmby()
        probe = MockProbe()
    else:
        app.state.emby = LiveEmby(cfg)
        probe = LiveProbe()
    app.state.pipeline = (
        MockPipeline() if cfg.mediadeck_mock else PipelineReader(cfg.pipeline_snapshot_path)
    )
    app.state.scheduler = Scheduler(cfg.nodes() or _mock_nodes(cfg), probe)

    async def probe_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await app.state.scheduler.refresh()
            await asyncio.sleep(15)

    app.state.probe_task = asyncio.create_task(probe_loop())


def _mock_nodes(cfg: Any):
    from app.core.config import StreamNode
    if not cfg.mediadeck_mock:
        return []
    return [
        StreamNode(name="mock-a", base_url="https://mock-a.example", probe_url="mock://a", weight=1),
        StreamNode(name="mock-b", base_url="https://mock-b.example", probe_url="mock://b", weight=2),
    ]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---- streaming nodes -------------------------------------------------------
@app.get("/api/nodes", dependencies=[Depends(_auth)])
async def nodes() -> list[dict[str, Any]]:
    return app.state.scheduler.snapshot()


@app.post("/api/nodes/{name}/disable", dependencies=[Depends(_auth)])
async def disable_node(name: str) -> dict[str, bool]:
    if not app.state.scheduler.set_disabled(name, True):
        raise HTTPException(404, "unknown node")
    return {"disabled": True}


@app.post("/api/nodes/{name}/enable", dependencies=[Depends(_auth)])
async def enable_node(name: str) -> dict[str, bool]:
    if not app.state.scheduler.set_disabled(name, False):
        raise HTTPException(404, "unknown node")
    return {"disabled": False}


@app.get("/api/dispatch/pick", dependencies=[Depends(_auth)])
async def dispatch_pick() -> dict[str, Any]:
    """Dry-run of the 302 target selection (no redirect issued)."""
    chosen = app.state.scheduler.pick()
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return {"node": chosen.node.name, "base_url": chosen.node.base_url,
            "normalized_load": chosen.normalized_load()}


@app.get("/stream/{path:path}")
async def stream_redirect(path: str) -> RedirectResponse:
    """The actual 302 edge: redirect a stream request to the chosen node."""
    chosen = app.state.scheduler.pick()
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return RedirectResponse(f"{chosen.node.base_url.rstrip('/')}/{path}", status_code=302)


# ---- pipeline --------------------------------------------------------------
@app.get("/api/pipeline", dependencies=[Depends(_auth)])
async def pipeline() -> dict[str, Any]:
    return app.state.pipeline.snapshot()


# ---- emby ------------------------------------------------------------------
@app.get("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_users() -> list[dict[str, Any]]:
    return await app.state.emby.list_users()


@app.get("/api/emby/sessions", dependencies=[Depends(_auth)])
async def emby_sessions() -> list[dict[str, Any]]:
    return await app.state.emby.active_sessions()
