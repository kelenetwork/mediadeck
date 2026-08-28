"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from pathlib import Path as FilePath
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app.adapters.live import LiveEmby, LiveProbe, probe_emby
from app.adapters.mock import MockEmby, MockProbe
from app.core.config import settings
from app.core.errors import ConfigError, NotConfigured, UpstreamError
from app.core.store import SettingsStore
from app.modules.imports import ImportManager, JobKind, MockExecutor
from app.modules.mounts import MockMounts, MountsReader
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.scheduler import Scheduler
from app.modules.settings import SettingsService
from app.modules.storage import MockStorage, StorageManager
from app.modules.tasks import MockTasks, TasksReader
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


def _storage_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, str(exc)) from None


@app.exception_handler(ConfigError)
async def _config_error_handler(_: Any, exc: ConfigError) -> JSONResponse:
    """Invalid operator input: answer with the message the UI should show."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(NotConfigured)
async def _not_configured_handler(_: Any, exc: NotConfigured) -> JSONResponse:
    """An integration has not been connected yet — surfaced as a setup prompt."""
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "needs_setup": True}
    )


@app.exception_handler(UpstreamError)
async def _upstream_error_handler(_: Any, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.on_event("startup")
async def _startup() -> None:
    cfg = settings()
    store = SettingsStore(cfg.settings_file)
    app.state.store = store
    app.state.settings_service = SettingsService(store)
    # First run: migrate .env values into the editable settings document so
    # existing deployments keep working, then never read them again.
    app.state.settings_service.bootstrap_from_env(cfg)

    if cfg.mediadeck_mock:
        app.state.emby = MockEmby()
        probe = MockProbe()
    else:
        app.state.emby = LiveEmby(app.state.settings_service.emby_config)
        probe = LiveProbe()
    app.state.pipeline = (
        MockPipeline() if cfg.mediadeck_mock else PipelineReader(cfg.pipeline_snapshot_path)
    )
    app.state.mounts = (
        MockMounts() if cfg.mediadeck_mock else MountsReader(cfg.mounts_snapshot_path)
    )
    app.state.storage = MockStorage() if cfg.mediadeck_mock else StorageManager(cfg)
    app.state.tasks = (
        MockTasks() if cfg.mediadeck_mock else TasksReader(cfg.tasks_snapshot_path)
    )
    app.state.imports = ImportManager(MockExecutor() if cfg.mediadeck_mock else None)
    if cfg.mediadeck_mock or not cfg.repo_root:
        app.state.updater = MockUpdater()
    else:
        app.state.updater = Updater(cfg.repo_root, cfg.service_name)

    dispatch = app.state.settings_service.dispatch_config()
    app.state.scheduler = Scheduler(
        app.state.settings_service.nodes() or _mock_nodes(cfg),
        probe,
        policy=dispatch["policy"],
        load_threshold=dispatch["load_threshold"],
    )
    # Node/policy edits in the UI reconfigure this scheduler in place.
    app.state.settings_service.bind_scheduler(app.state.scheduler)

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
        StreamNode(name="mock-a", base_url="https://mock-a.example",
                   probe_url="mock://a", capacity=20),
        StreamNode(name="mock-b", base_url="https://mock-b.example",
                   probe_url="mock://b", capacity=40),
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


# ---- settings --------------------------------------------------------------
@app.get("/api/settings", dependencies=[Depends(_auth)])
async def settings_overview() -> dict[str, Any]:
    """Everything the settings page renders, in one round trip."""
    service = app.state.settings_service
    return {
        "mock_mode": settings().mediadeck_mock,
        "emby": service.emby_public(),
        "dispatch": service.dispatch_config(),
        "nodes": service.nodes_public(),
    }


@app.get("/api/settings/emby", dependencies=[Depends(_auth)])
async def settings_emby_get() -> dict[str, Any]:
    return app.state.settings_service.emby_public()


@app.put("/api/settings/emby", dependencies=[Depends(_auth)])
async def settings_emby_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.save_emby(payload)


@app.post("/api/settings/emby/test", dependencies=[Depends(_auth)])
async def settings_emby_test(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """Validate a connection before saving it, so setup is not trial and error."""
    if settings().mediadeck_mock:
        return await app.state.emby.system_info()
    url, api_key, timeout, verify = app.state.settings_service.resolve_probe_target(payload)
    return await probe_emby(url, api_key, timeout, verify)


@app.get("/api/settings/dispatch", dependencies=[Depends(_auth)])
async def settings_dispatch_get() -> dict[str, Any]:
    return app.state.settings_service.dispatch_config()


@app.put("/api/settings/dispatch", dependencies=[Depends(_auth)])
async def settings_dispatch_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.save_dispatch(payload)


# ---- streaming nodes -------------------------------------------------------
@app.get("/api/nodes", dependencies=[Depends(_auth)])
async def nodes() -> list[dict[str, Any]]:
    return app.state.scheduler.snapshot()


@app.post("/api/nodes", dependencies=[Depends(_auth)])
async def create_node(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.add_node(payload)


@app.put("/api/nodes/{name}", dependencies=[Depends(_auth)])
async def update_node(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    try:
        return app.state.settings_service.update_node(name, payload)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.delete("/api/nodes/{name}", dependencies=[Depends(_auth)])
async def delete_node(name: str) -> dict[str, bool]:
    try:
        app.state.settings_service.delete_node(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    return {"deleted": True}


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
async def dispatch_pick(path: str = "") -> dict[str, Any]:
    """Dry-run of the 302 target selection (no redirect issued).

    Accepts a path so an operator can verify affinity: the same path must
    keep resolving to the same node while that node stays healthy.
    """
    chosen = app.state.scheduler.pick(record=False, context=path)
    if not chosen:
        raise HTTPException(503, "no available streaming node")
    return {"node": chosen.node.name, "base_url": chosen.node.base_url,
            "utilisation": round(chosen.utilisation(), 3),
            "policy": app.state.scheduler.policy}


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


@app.get("/api/mounts", dependencies=[Depends(_auth)])
async def mounts() -> dict[str, Any]:
    return app.state.mounts.snapshot()


# ---- storage (rclone remotes + systemd mounts) -----------------------------
@app.get("/api/storage/remotes", dependencies=[Depends(_auth)])
async def storage_list_remotes() -> list[dict[str, Any]]:
    return _storage_call(app.state.storage.list_remotes)


@app.post("/api/storage/remotes", dependencies=[Depends(_auth)])
async def storage_add_remote(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return _storage_call(
        app.state.storage.add_remote,
        payload.get("name") or "",
        payload.get("type") or "",
        payload.get("options") or {},
    )


@app.delete("/api/storage/remotes/{name}", dependencies=[Depends(_auth)])
async def storage_delete_remote(name: str) -> dict[str, bool]:
    return _storage_call(app.state.storage.delete_remote, name)


@app.post("/api/storage/remotes/{name}/test", dependencies=[Depends(_auth)])
async def storage_test_remote(name: str) -> dict[str, Any]:
    return _storage_call(app.state.storage.test_remote, name)


@app.get("/api/storage/mounts", dependencies=[Depends(_auth)])
async def storage_list_mounts() -> list[dict[str, Any]]:
    return _storage_call(app.state.storage.list_mounts)


@app.post("/api/storage/mounts", dependencies=[Depends(_auth)])
async def storage_create_mount(spec: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return _storage_call(app.state.storage.create_mount, spec)


@app.post("/api/storage/mounts/{name}/start", dependencies=[Depends(_auth)])
async def storage_start_mount(name: str) -> dict[str, Any]:
    return _storage_call(app.state.storage.start_mount, name)


@app.post("/api/storage/mounts/{name}/stop", dependencies=[Depends(_auth)])
async def storage_stop_mount(name: str) -> dict[str, Any]:
    return _storage_call(app.state.storage.stop_mount, name)


@app.delete("/api/storage/mounts/{name}", dependencies=[Depends(_auth)])
async def storage_delete_mount(name: str) -> dict[str, bool]:
    return _storage_call(app.state.storage.delete_mount, name)


@app.get("/api/tasks", dependencies=[Depends(_auth)])
async def tasks() -> dict[str, Any]:
    return app.state.tasks.snapshot()


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


# ---- emby ------------------------------------------------------------------
@app.get("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_users() -> list[dict[str, Any]]:
    return await app.state.emby.list_users()


@app.get("/api/emby/libraries", dependencies=[Depends(_auth)])
async def emby_libraries() -> list[dict[str, Any]]:
    return await app.state.emby.libraries()


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
