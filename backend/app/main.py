"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from pathlib import Path as FilePath
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app.adapters.live import LiveEmby, LiveProbe, probe_emby
from app.adapters.mock import MockEmby, MockProbe
from app.core.cache import TTLCache
from app.core.config import settings
from app.core.errors import ConfigError, NotConfigured, UpstreamError
from app.core.store import SettingsStore
from app.modules.events import EventStream, safe_stream
from app.modules.imports import ImportManager, JobKind, MockExecutor
from app.modules.mounts import MockMounts, MountsReader
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.playback import PlaybackRouter, caller_token
from app.modules.provisioning import (
    emby_frontend_snippet,
    enroll_command,
    install_script,
)
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
    app.state.cache = TTLCache()
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
    # Nodes always come from the settings store -- in mock mode the demo fleet
    # was seeded into it at bootstrap, so the settings page and the scheduler
    # can never disagree about which nodes exist.
    app.state.scheduler = Scheduler(
        app.state.settings_service.nodes(),
        probe,
        policy=dispatch["policy"],
        load_threshold=dispatch["load_threshold"],
    )
    # Node/policy edits in the UI reconfigure this scheduler in place.
    app.state.settings_service.bind_scheduler(app.state.scheduler)

    # Playback interception: the piece that puts the scheduler on the real
    # client path instead of only the /stream test edge.
    app.state.playback = PlaybackRouter(
        app.state.emby,
        app.state.scheduler,
        app.state.settings_service.playback_config,
        app.state.settings_service.emby_config,
    )

    async def probe_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await app.state.scheduler.refresh()
            # Wakes early when nodes change, so a node added in the UI shows
            # its real health at once rather than after up to 15 seconds.
            await app.state.scheduler.wait_for_change(15)

    app.state.probe_task = asyncio.create_task(probe_loop())

    async def _nodes_topic() -> Any:
        config = {n["name"]: n for n in app.state.settings_service.nodes_public()}
        out = []
        for state in app.state.scheduler.snapshot():
            row = dict(config.get(state["name"], {}))
            row.update(state)
            out.append(row)
        return out

    app.state.events = EventStream({
        "nodes": _nodes_topic,
        "sessions": lambda: app.state.cache.resolve(
            "emby:sessions", app.state.emby.active_sessions, ttl=5),
        "pipeline": lambda: asyncio.to_thread(app.state.pipeline.snapshot),
        "tasks": lambda: asyncio.to_thread(app.state.tasks.snapshot),
        "mounts": lambda: asyncio.to_thread(app.state.mounts.snapshot),
        "playback": lambda: asyncio.to_thread(app.state.playback.recent, 30),
    })


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


@app.get("/api/stream", dependencies=[Depends(_auth)])
async def event_stream(request: Request, topics: str = "") -> StreamingResponse:
    """Server-sent events: push changes instead of polling every 30s.

    Polling was wrong in both directions -- a stream starting now stayed
    invisible for up to 30s, while an idle panel hammered Emby forever, and
    the periodic re-render wiped whatever the operator was typing.
    """
    wanted = [t.strip() for t in topics.split(",") if t.strip()]
    return StreamingResponse(
        safe_stream(app.state.events.iterate(wanted)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold
            # every event until the buffer fills -- i.e. no live updates.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/agent/loadprobe.py", include_in_schema=False)
async def agent_loadprobe() -> FileResponse:
    """Serve the node probe agent so the installer can fetch it from here.

    Kept as a route rather than a copy under static/ so there is exactly one
    copy of the agent in the repo and it cannot drift.
    """
    for candidate in (
        FilePath(__file__).resolve().parents[2] / "agent" / "loadprobe.py",
        FilePath(__file__).resolve().parents[3] / "agent" / "loadprobe.py",
    ):
        if candidate.is_file():
            return FileResponse(candidate, media_type="text/x-python")
    raise HTTPException(404, "agent not found in this deployment")


# ---- settings --------------------------------------------------------------
@app.get("/api/settings", dependencies=[Depends(_auth)])
async def settings_overview() -> dict[str, Any]:
    """Everything the settings page renders, in one round trip."""
    service = app.state.settings_service
    return {
        "mock_mode": settings().mediadeck_mock,
        "emby": service.emby_public(),
        "dispatch": service.dispatch_config(),
        "playback": service.playback_config(),
        "integration": service.integration_config(),
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
    """Live health merged with stored config, so the UI has one source.

    The scheduler knows health; the settings store knows media roots and
    whether a signing key is set. Returning only one of them forces the UI to
    stitch two lists together and get them out of sync.
    """
    config = {n["name"]: n for n in app.state.settings_service.nodes_public()}
    merged = []
    for state in app.state.scheduler.snapshot():
        row = dict(config.get(state["name"], {}))
        row.update(state)
        merged.append(row)
    return merged


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


# ---- playback interception -------------------------------------------------
@app.get("/api/settings/playback", dependencies=[Depends(_auth)])
async def settings_playback_get() -> dict[str, Any]:
    return app.state.settings_service.playback_config()


@app.put("/api/settings/playback", dependencies=[Depends(_auth)])
async def settings_playback_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_playback(payload)
    # Path mapping changed -> previously resolved paths may be stale.
    app.state.playback.invalidate()
    return saved


@app.get("/api/playback/log", dependencies=[Depends(_auth)])
async def playback_log(limit: int = 100) -> list[dict[str, Any]]:
    return app.state.playback.recent(limit)


@app.get("/api/playback/preview", dependencies=[Depends(_auth)])
async def playback_preview(item_id: str) -> dict[str, Any]:
    """Dry-run one interception so the operator can confirm path mapping.

    A wrong media-root mapping yields 404s on the node that are painful to
    debug from client logs; this shows the resolved target first.
    """
    decision = await app.state.playback.route(
        item_id, f"emby/Videos/{item_id}/stream.mkv", {"Static": "true"}
    )
    return {
        "redirected": decision.redirected,
        "target": decision.target,
        "reason": decision.reason,
        "node": decision.node,
        "pool": decision.pool,
        "media_path": decision.media_path,
        "signed": decision.signed,
    }


# ---- integration / node provisioning ---------------------------------------
@app.get("/api/settings/integration", dependencies=[Depends(_auth)])
async def settings_integration_get() -> dict[str, Any]:
    return app.state.settings_service.integration_config()


@app.put("/api/settings/integration", dependencies=[Depends(_auth)])
async def settings_integration_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    return app.state.settings_service.save_integration(payload)


@app.get("/api/integration/frontend", dependencies=[Depends(_auth)])
async def integration_frontend(server: str = "caddy") -> dict[str, Any]:
    """Reverse-proxy rule that puts the panel on the real playback path.

    Answers "how does my existing Emby domain dispatch to nodes": the operator
    keeps one public Emby hostname and only stream requests reach the panel.
    """
    service = app.state.settings_service
    integration = service.integration_config()
    emby_public = integration["emby_public_url"] or service.emby_config()["url"]
    panel_public = integration["panel_public_url"] or "http://127.0.0.1:8300"
    return {
        "server": server,
        "config": emby_frontend_snippet(panel_public, emby_public, server),
    }


@app.post("/api/nodes/{name}/rotate-secret", dependencies=[Depends(_auth)])
async def node_rotate_secret(name: str) -> dict[str, Any]:
    """Issue a new signing key for one node, invalidating its issued links."""
    try:
        return app.state.settings_service.rotate_node_secret(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None


@app.get("/api/nodes/{name}/enroll", dependencies=[Depends(_auth)])
async def node_enroll_command(name: str) -> dict[str, Any]:
    """The single command that turns a bare server into this node.

    Everything the node needs -- Drive identity, media roots, cache, signing
    key -- is already stored against the node, so the operator does not fill
    anything in on the target machine. The installer fetches that config from
    the panel using a one-shot enrollment token.
    """
    service = app.state.settings_service
    node = service.node(name)
    if node is None:
        raise HTTPException(404, "unknown node")
    panel = service.integration_config()["panel_public_url"]
    if not panel:
        raise HTTPException(
            409, "请先在「系统设置 → 接入方式」填写面板对外地址，节点需要用它回连"
        )
    token = service.node_enroll_token(name)
    return {
        "node": name,
        "command": enroll_command(panel, token),
        "ready": bool(node.pools),
        "warnings": (
            [] if node.pools else ["该节点尚未配置媒体根，安装后无法提供任何文件"]
        ),
    }


@app.get("/api/nodes/{name}/install", dependencies=[Depends(_auth)])
async def node_install_script(name: str) -> dict[str, Any]:
    """Render the installer for review (same content the one-liner fetches)."""
    service = app.state.settings_service
    node = service.node(name)
    if node is None:
        raise HTTPException(404, "unknown node")
    panel = service.integration_config()["panel_public_url"] or "http://127.0.0.1:8300"
    return {
        "node": name,
        "signing_enabled": bool(node.sign_secret),
        "script": install_script(node, panel),
    }


@app.get("/api/enroll/{token}/script", include_in_schema=False)
async def enroll_script(token: str) -> PlainTextResponse:
    """Unauthenticated by design: the enrollment token *is* the credential.

    A bare server has no panel login, so the one-liner cannot use HTTP Basic.
    The token is per node, unguessable, and only yields that node's installer.
    """
    service = app.state.settings_service
    node = service.node_by_enroll_token(token)
    if node is None:
        raise HTTPException(404, "invalid or expired enrollment token")
    panel = service.integration_config()["panel_public_url"] or "http://127.0.0.1:8300"
    return PlainTextResponse(install_script(node, panel),
                             media_type="text/x-shellscript")


async def emby_video_stream(item_id: str, rest: str, request: Request) -> RedirectResponse:
    """Emby-compatible stream edge.

    A reverse proxy sends real client playback here, so this endpoint is
    public by necessity -- and therefore must not weaken Emby's own auth.
    The caller's Emby token is verified against Emby before any signed node
    URL is issued; otherwise the panel would become a way to fetch media
    without logging in at all.

    Unverified callers are passed through to Emby rather than rejected, so
    Emby applies its own decision and the fail-open contract still holds.
    """
    query = dict(request.query_params)
    # Preserve the exact incoming path: the fallback URL must point back at the
    # same Emby endpoint the client actually asked for, not a normalised guess.
    decision = await app.state.playback.route(
        item_id, request.url.path.lstrip("/"), query,
        caller_token=caller_token(request.headers, query),
        require_auth=True,
    )
    if not decision.target:
        raise HTTPException(409, "Emby origin not configured")
    return RedirectResponse(decision.target, status_code=302)


# Real client traffic uses several shapes for this path. Observed in this
# deployment's Emby log: /emby/videos/<id>/original.mkv (lowercase, by far the
# most common), /Videos/<id>/stream (no /emby prefix) and mixed casings.
# Starlette routes are case-sensitive, so registering only one shape means real
# playback silently never reaches the panel and dispatch appears to do nothing.
for _prefix in ("/emby/Videos", "/emby/videos", "/Videos", "/videos"):
    app.get(_prefix + "/{item_id}/{rest:path}", include_in_schema=False)(
        emby_video_stream
    )


# ---- emby ------------------------------------------------------------------
@app.get("/api/emby/users", dependencies=[Depends(_auth)])
async def emby_users() -> list[dict[str, Any]]:
    return await app.state.emby.list_users()


@app.get("/api/emby/libraries", dependencies=[Depends(_auth)])
async def emby_libraries() -> list[dict[str, Any]]:
    # One item-count query per library makes this the slowest view in the
    # panel; pages auto-refresh, so cache it instead of re-running per render.
    return await app.state.cache.resolve(
        "emby:libraries", app.state.emby.libraries, ttl=120
    )


@app.get("/api/emby/sessions", dependencies=[Depends(_auth)])
async def emby_sessions() -> list[dict[str, Any]]:
    # Short TTL: sessions must still feel live, but a 30s auto-refresh plus
    # page switches should not hammer Emby.
    return await app.state.cache.resolve(
        "emby:sessions", app.state.emby.active_sessions, ttl=5
    )


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
