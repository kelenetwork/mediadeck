"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from pathlib import Path as FilePath
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app.adapters.live import LiveEmby, LiveProbe
from app.adapters.mock import MockEmby, MockProbe
from app.adapters.mp import LiveMoviePilot, MockMoviePilot, MPError
from app.core.config import settings
from app.modules.imports import ImportManager, JobKind, MockExecutor
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.scheduler import Scheduler
from app.modules.updater import MockUpdater, Updater

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
    app.state.imports = ImportManager(MockExecutor() if cfg.mediadeck_mock else None)
    if cfg.mediadeck_mock or not cfg.repo_root:
        app.state.updater = MockUpdater()
    else:
        app.state.updater = Updater(cfg.repo_root, cfg.service_name)
    if cfg.mediadeck_mock or not cfg.mp_url:
        app.state.mp = MockMoviePilot()
    else:
        app.state.mp = LiveMoviePilot(cfg)
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


STATIC_DIR = FilePath(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/whoami", dependencies=[Depends(_auth)])
async def whoami() -> dict[str, str]:
    return {"user": settings().mediadeck_admin_user}


@app.get("/", include_in_schema=False)
async def root(_: str = Depends(_auth)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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


@app.get("/api/nodes/{name}/history", dependencies=[Depends(_auth)])
async def node_history(name: str, limit: int = 240) -> list[dict[str, Any]]:
    try:
        return app.state.scheduler.history(name, limit)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.get("/api/dispatch/log", dependencies=[Depends(_auth)])
async def dispatch_log(limit: int = 100) -> list[dict[str, Any]]:
    return app.state.scheduler.dispatch_log(limit)


@app.get("/api/dispatch/pick", dependencies=[Depends(_auth)])
async def dispatch_pick() -> dict[str, Any]:
    """Dry-run of the 302 target selection (no redirect issued)."""
    chosen = app.state.scheduler.pick(record=False)
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return {"node": chosen.node.name, "base_url": chosen.node.base_url,
            "normalized_load": chosen.normalized_load()}


@app.get("/stream/{path:path}")
async def stream_redirect(path: str) -> RedirectResponse:
    """The actual 302 edge: redirect a stream request to the chosen node."""
    chosen = app.state.scheduler.pick(context=path)
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return RedirectResponse(f"{chosen.node.base_url.rstrip('/')}/{path}", status_code=302)


# ---- pipeline --------------------------------------------------------------
@app.get("/api/pipeline", dependencies=[Depends(_auth)])
async def pipeline() -> dict[str, Any]:
    return app.state.pipeline.snapshot()


# ---- self-update -----------------------------------------------------------
@app.get("/api/update/version", dependencies=[Depends(_auth)])
async def update_version() -> dict[str, Any]:
    return app.state.updater.version()


@app.get("/api/update/check", dependencies=[Depends(_auth)])
async def update_check() -> dict[str, Any]:
    return app.state.updater.check()


@app.post("/api/update/apply", dependencies=[Depends(_auth)])
async def update_apply(target: str | None = Body(None, embed=True)) -> dict[str, Any]:
    result = app.state.updater.update(target)
    if not result.get("started"):
        raise HTTPException(409, result.get("error", "update not started"))
    return result


# ---- import lanes ----------------------------------------------------------
@app.post("/api/imports", dependencies=[Depends(_auth)])
async def imports_submit(
    kind: str = Body(...),
    source_ref: str = Body(...),
    category: str = Body(""),
) -> dict[str, Any]:
    try:
        job_kind = JobKind(kind)
    except ValueError:
        raise HTTPException(422, f"unknown kind: {kind}") from None
    try:
        job = app.state.imports.submit(job_kind, source_ref, category)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return job.to_dict()


@app.get("/api/imports", dependencies=[Depends(_auth)])
async def imports_list(state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return [j.to_dict() for j in app.state.imports.list(state, limit)]


@app.get("/api/imports/{job_id}", dependencies=[Depends(_auth)])
async def imports_get(job_id: str) -> dict[str, Any]:
    job = app.state.imports.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.to_dict()


@app.post("/api/imports/{job_id}/cancel", dependencies=[Depends(_auth)])
async def imports_cancel(job_id: str) -> dict[str, bool]:
    if not app.state.imports.cancel(job_id):
        raise HTTPException(409, "job not cancellable")
    return {"cancelled": True}


# ---- acquisition (MoviePilot) ----------------------------------------------
@app.get("/api/mp/media/search", dependencies=[Depends(_auth)])
async def mp_media_search(keyword: str) -> list[dict[str, Any]]:
    try:
        return await app.state.mp.search_media(keyword)
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


@app.get("/api/mp/torrents/search", dependencies=[Depends(_auth)])
async def mp_torrent_search(keyword: str) -> list[dict[str, Any]]:
    try:
        return await app.state.mp.search_torrents(keyword)
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


@app.get("/api/mp/subscribes", dependencies=[Depends(_auth)])
async def mp_subscribes() -> list[dict[str, Any]]:
    try:
        return await app.state.mp.list_subscribes()
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


@app.post("/api/mp/subscribes", dependencies=[Depends(_auth)])
async def mp_add_subscribe(
    tmdb_id: int = Body(...),
    media_type: str = Body(...),
    season: int | None = Body(None),
) -> dict[str, Any]:
    try:
        return await app.state.mp.add_subscribe(tmdb_id, media_type, season)
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


@app.delete("/api/mp/subscribes/{subscribe_id}", dependencies=[Depends(_auth)])
async def mp_del_subscribe(subscribe_id: int) -> dict[str, bool]:
    try:
        if not await app.state.mp.delete_subscribe(subscribe_id):
            raise HTTPException(404, "unknown subscription")
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"deleted": True}


@app.post("/api/mp/download", dependencies=[Depends(_auth)])
async def mp_download(
    enclosure: str = Body(...),
    title: str = Body(...),
) -> dict[str, Any]:
    try:
        return await app.state.mp.download_torrent(enclosure, title)
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


@app.get("/api/mp/downloading", dependencies=[Depends(_auth)])
async def mp_downloading() -> list[dict[str, Any]]:
    try:
        return await app.state.mp.downloading()
    except MPError as exc:
        raise HTTPException(502, str(exc)) from None


# ---- emby ------------------------------------------------------------------
@app.get("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_users() -> list[dict[str, Any]]:
    return await app.state.emby.list_users()


@app.get("/api/emby/sessions", dependencies=[Depends(_auth)])
async def emby_sessions() -> list[dict[str, Any]]:
    return await app.state.emby.active_sessions()


@app.post("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_create_user(name: str = Body(..., embed=True, min_length=1, max_length=60)) -> dict[str, Any]:
    return await app.state.emby.create_user(name)


@app.post("/api/emby/users/{user_id}/disable", dependencies=[Depends(_auth)])
async def emby_disable_user(user_id: str) -> dict[str, bool]:
    if not await app.state.emby.set_user_disabled(user_id, True):
        raise HTTPException(404, "unknown user")
    return {"disabled": True}


@app.post("/api/emby/users/{user_id}/enable", dependencies=[Depends(_auth)])
async def emby_enable_user(user_id: str) -> dict[str, bool]:
    if not await app.state.emby.set_user_disabled(user_id, False):
        raise HTTPException(404, "unknown user")
    return {"disabled": False}


@app.post("/api/emby/users/{user_id}/password", dependencies=[Depends(_auth)])
async def emby_set_password(user_id: str, new_password: str = Body(..., embed=True, min_length=6)) -> dict[str, bool]:
    if not await app.state.emby.set_user_password(user_id, new_password):
        raise HTTPException(404, "unknown user")
    return {"ok": True}


@app.post("/api/emby/users/{user_id}/policy", dependencies=[Depends(_auth)])
async def emby_apply_policy(user_id: str, policy: dict[str, Any] = Body(...)) -> dict[str, bool]:  # noqa: B008
    allowed = {"IsDisabled", "EnableRemoteAccess", "SimultaneousStreamLimit",
               "RemoteClientBitrateLimit", "InvalidLoginAttemptCount",
               "IsHidden", "EnableLiveTvAccess", "EnableContentDownloading"}
    patch = {k: v for k, v in policy.items() if k in allowed}
    if not patch:
        raise HTTPException(422, "no allowed policy fields in body")
    if not await app.state.emby.apply_policy(user_id, patch):
        raise HTTPException(404, "unknown user")
    return {"ok": True}
