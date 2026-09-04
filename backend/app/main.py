"""mediadeck backend entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
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
from app.core.db import Database
from app.core.errors import ConfigError, ConflictError, NotConfigured, UpstreamError
from app.core.store import SettingsStore
from app.modules.access import AccessRules
from app.modules.enforcement import EnforcementService
from app.modules.events import EventStream, safe_stream
from app.modules.groups import GroupService
from app.modules.imagecache import ALLOWED_IMAGE_TYPES, ImageCache
from app.modules.imports import ImportManager, JobKind, MockExecutor
from app.modules.members import MemberService, random_password, rate_bytes_per_sec
from app.modules.mounts import MockMounts, MountsReader
from app.modules.pipeline import MockPipeline, PipelineReader
from app.modules.playback import PlaybackRouter, caller_device, caller_token
from app.modules.provisioning import (
    emby_frontend_snippet,
    enroll_command,
    install_script,
)
from app.modules.scheduler import Scheduler
from app.modules.settings import SettingsService
from app.modules.sharing import SharingDetector
from app.modules.signing import user_tag
from app.modules.stats import StatsService
from app.modules.storage import MockStorage, StorageManager
from app.modules.tasks import MockTasks, TasksReader
from app.modules.telegram import TelegramBot
from app.modules.updater import MockUpdater, Updater
from app.modules.usage import UsageSampler

app = FastAPI(title="mediadeck", version="0.1.0")
security = HTTPBasic()


async def _auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:  # noqa: B008
    cfg = settings()
    user_ok = secrets.compare_digest(credentials.username, cfg.mediadeck_admin_user)
    pass_ok = secrets.compare_digest(credentials.password, cfg.mediadeck_admin_password)
    if user_ok and pass_ok:
        return credentials.username
    # Members holding the admin *role* may operate the panel with their Emby
    # credentials (owner decision 2026-08-30). The role check runs first and
    # reads only our own DB, so a random visitor cannot use the panel login
    # form to brute-force Emby passwords of non-admin accounts.
    member_user = await _role_admin_auth(credentials.username, credentials.password)
    if member_user:
        return member_user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})


async def _role_admin_auth(username: str, password: str) -> str | None:
    members = getattr(app.state, "members", None)
    emby = getattr(app.state, "emby", None)
    if not members or not emby or not username or not password:
        return None
    candidates = [m for m in members.list(role="admin", limit=200)
                  if (m.get("username") or "").lower() == username.lower()
                  and m.get("state") == "active"]
    if not candidates:
        return None
    # Positive results are cached briefly so every API call in a browsing
    # session does not become an Emby authentication round-trip.
    cache_key = f"panelauth:{username}"
    entry = app.state.cache.get(cache_key) if hasattr(app.state, "cache") else None
    if entry and secrets.compare_digest(entry, _digest(password)):
        return username
    try:
        user = await emby.authenticate_user(username, password)
    except Exception:  # noqa: BLE001 - Emby down must read as 401, not 500
        return None
    if not user or str(user.get("Id")) != str(candidates[0]["emby_user_id"]):
        return None
    if hasattr(app.state, "cache"):
        app.state.cache.set(cache_key, _digest(password), ttl=300)
    return username


def _digest(secret_text: str) -> str:
    import hashlib
    return hashlib.sha256(secret_text.encode()).hexdigest()


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


@app.exception_handler(ConflictError)
async def _conflict_error_handler(_: Any, exc: ConflictError) -> JSONResponse:
    """Well-formed but currently impossible: the operator must resolve state first."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


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
    async def _member_rate(token: str, device: str = "") -> tuple[int, str]:
        """Caller credential -> (bandwidth cap in bytes/s, anonymised user tag).

        Signed into every node URL, so the node enforces the member's cap and
        can attribute real transfer bytes back to the user. Cached briefly:
        one playback start must not cost two extra round trips every time.

        The device id is part of the cache key, not just the token: one admin
        api_key is shared by every session it can see, so caching by token
        alone would hand the first resolved user's cap to everyone else.
        """
        cache_key = f"rate:{user_tag(token)}:{user_tag(device)}"
        cached = app.state.cache.get(cache_key)
        if cached is not None:
            return cached
        result: tuple[int, str] = (0, "")
        try:
            uid = await app.state.emby.user_for_token(token, device)
            if uid:
                member = app.state.members.get(uid)
                kbps = int((member or {}).get("bandwidth_limit_kbps") or 0)
                result = (rate_bytes_per_sec(kbps), user_tag(uid))
        except Exception:  # noqa: BLE001 - fail open: sign uncapped
            result = (0, "")
        # An unresolved caller is cached only briefly: it means the stream is
        # running uncapped and unattributed, and that must self-heal as soon
        # as the session registers rather than persist for a full minute.
        app.state.cache.set(cache_key, result, ttl=60 if result[1] else 10)
        return result

    app.state.playback = PlaybackRouter(
        app.state.emby,
        app.state.scheduler,
        app.state.settings_service.playback_config,
        app.state.settings_service.emby_config,
        rate_resolver=_member_rate,
    )

    # ---- membership, billing, statistics --------------------------------
    # Operational data lives in SQLite rather than the settings JSON: traffic
    # accounting appends continuously and has to answer aggregate questions,
    # which a rewritten-in-full document cannot do safely.
    app.state.db = Database(cfg.data_dir / "mediadeck.db")
    app.state.groups = GroupService(app.state.db)
    app.state.groups.seed_defaults()
    app.state.members = MemberService(app.state.db, app.state.groups)
    app.state.enforcement = EnforcementService(
        app.state.db, app.state.members, app.state.emby)
    app.state.stats = StatsService(app.state.db)
    # Rides along with the sampler: it already holds the only live view of who
    # is playing from where, so detection costs no extra Emby calls.
    app.state.sharing = SharingDetector(app.state.db)
    app.state.access = AccessRules(app.state.db)
    app.state.usage = UsageSampler(
        app.state.db, app.state.members, app.state.emby, app.state.enforcement,
        sharing=app.state.sharing)

    image_cfg = app.state.settings_service.image_cache_config()
    app.state.images = ImageCache(
        cfg.data_dir / "imagecache",
        max_bytes=image_cfg["max_bytes"],
        max_age_seconds=image_cfg["max_age_days"] * 86400,
    )

    def _node_for_item(item_id: str) -> str:
        """Which node most recently served this item, for traffic attribution.

        Read from the dispatch log rather than tracked separately: the log is
        already the authoritative record of what the scheduler decided, so a
        second source could only ever disagree with it.
        """
        if not item_id:
            return ""
        for entry in reversed(app.state.playback.recent(60)):
            if entry.get("item_id") == item_id and entry.get("node"):
                return str(entry["node"])
        return ""

    async def usage_loop() -> None:
        """Sample playback, roll billing periods, and keep caches bounded.

        One loop rather than several timers: these steps must not interleave
        (rolling a period while sampling could reset a quota mid-write), and
        serialising them keeps the ordering obvious.

        Every step is individually guarded: a failure in housekeeping must not
        stop metering, because unmetered playback is the one outcome that
        costs money.
        """
        housekeeping_due = 0.0
        prune_due = 0.0
        while True:
            membership = app.state.settings_service.membership_config()
            await asyncio.sleep(max(5, int(membership["sample_interval_seconds"])))
            with contextlib.suppress(Exception):
                await app.state.usage.tick(node_of=_node_for_item)

            now = time.time()
            if now >= housekeeping_due:
                housekeeping_due = now + 600
                with contextlib.suppress(Exception):
                    app.state.members.roll_periods()
                # Enforcement only writes to Emby once the operator has
                # switched it on; until then the panel observes and reports.
                if membership["enforcement_enabled"]:
                    with contextlib.suppress(Exception):
                        await app.state.enforcement.reconcile(apply=True)
                with contextlib.suppress(Exception):
                    app.state.images.sweep()

            if now >= prune_due:
                prune_due = now + 86400
                with contextlib.suppress(Exception):
                    app.state.stats.prune(int(membership["retention_days"]))

    app.state.usage_task = asyncio.create_task(usage_loop())

    async def probe_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await app.state.scheduler.refresh()
            # Wakes early when nodes change, so a node added in the UI shows
            # its real health at once rather than after up to 15 seconds.
            await app.state.scheduler.wait_for_change(15)

    app.state.probe_task = asyncio.create_task(probe_loop())

    # ---- telegram bot ----------------------------------------------------
    # Long polling, not a webhook: a webhook needs a public HTTPS route into
    # the panel, while polling reaches out instead and leaves the panel
    # reachable only from where it already was.
    app.state.telegram = TelegramBot(
        app.state.settings_service.telegram_config, app.state.members,
        emby=app.state.emby, stats=app.state.stats, db=app.state.db)
    app.state.telegram.start()

    async def telegram_notify_loop() -> None:
        """Daily expiry reminders, and the daily ranking post.

        Both are keyed by day rather than by an interval, so a restart cannot
        produce a second round of the same message a few hours after the first.
        The two are tracked separately: they fire at different hours, and a
        restart between them must not cancel whichever has not gone out yet.
        """
        reminded_on = ""
        ranked_on = ""
        while True:
            await asyncio.sleep(300)
            with contextlib.suppress(Exception):
                cfg = app.state.settings_service.telegram_config()
                if not cfg["enabled"]:
                    continue
                today = time.strftime("%Y-%m-%d")
                hour = time.localtime().tm_hour

                # Not before 10:00: a reminder that arrives at 04:00 wakes
                # someone up to tell them about a renewal days away.
                if (cfg["notify_expiring"] and reminded_on != today
                        and hour >= 10):
                    due = app.state.members.expiring_within(
                        cfg["notify_expiring_days"])
                    await app.state.telegram.notify_expiring(due)
                    reminded_on = today

                if (cfg["rankings_enabled"] and cfg["rankings_chat"]
                        and ranked_on != today and hour >= cfg["rankings_hour"]):
                    await app.state.telegram.broadcast_rankings(
                        cfg["rankings_chat"], days=1)
                    ranked_on = today

    app.state.telegram_task = asyncio.create_task(telegram_notify_loop())

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
        # Must be the *decorated* payload: the live push and the manual
        # refresh have to agree, otherwise the dashboard shows speeds only
        # when you hit refresh and looks frozen the rest of the time.
        "sessions": _sessions_with_speed,
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
async def root(_: str = Depends(_auth)) -> HTMLResponse:
    # Cache-busting: stamp static asset URLs with the deployed commit so a
    # release is visible on the next reload without a forced refresh.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    try:
        ver = str(app.state.updater.version().get("commit") or "")
    except Exception:  # noqa: BLE001 - version stamping must never break the page
        ver = ""
    if ver:
        for asset in ("app.css", "app.js", "ops.js"):
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html)


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
        "membership": service.membership_config(),
        "image_cache": service.image_cache_config(),
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


@app.post("/api/nodes/{name}/rotate-enroll", dependencies=[Depends(_auth)])
async def node_rotate_enroll(name: str) -> dict[str, Any]:
    """Invalidate the current install command; the old one-liner stops working."""
    try:
        app.state.settings_service.rotate_enroll_token(name)
    except KeyError:
        raise HTTPException(404, "unknown node") from None
    return await node_enroll_command(name)


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
    enrolled = bool(node.first_seen_at)
    return {
        "node": name,
        "command": enroll_command(panel, token),
        "ready": bool(node.pools),
        "enrolled": enrolled,
        "pending": not enrolled,
        "first_seen_at": node.first_seen_at,
        "enrolled_host": node.enrolled_host,
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


@app.post("/api/enroll/{token}/report", include_in_schema=False)
async def enroll_report(token: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:  # noqa: B008
    """Public by design: the install token is the credential.

    The node reports the addresses it actually has, so the operator never has
    to type them. A wrong token is indistinguishable from an expired one.
    """
    try:
        return app.state.settings_service.apply_enroll_report(token, payload or {})
    except KeyError:
        raise HTTPException(404, "invalid or expired enrollment token") from None
    except ConfigError as exc:
        raise HTTPException(422, str(exc)) from None


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

    # Access rules run before routing. They decide whether this caller may be
    # handed a signed node URL at all; refusing here rather than after routing
    # means a denied request never causes a node to be selected or a signature
    # to be minted.
    verdict = app.state.access.evaluate(
        user_agent=request.headers.get("user-agent", ""),
        remote_ip=request.client.host if request.client else "",
    )
    if not verdict["allowed"]:
        with contextlib.suppress(Exception):
            app.state.access.record_block(
                username="",
                user_agent=request.headers.get("user-agent", ""),
                remote_ip=request.client.host if request.client else "",
                reason=verdict["reason"], rule_id=verdict["rule_id"],
                item_id=item_id)
        raise HTTPException(403, "access denied by rule")

    # Preserve the exact incoming path: the fallback URL must point back at the
    # same Emby endpoint the client actually asked for, not a normalised guess.
    decision = await app.state.playback.route(
        item_id, request.url.path.lstrip("/"), query,
        caller_token=caller_token(request.headers, query),
        caller_device=caller_device(request.headers, query),
        require_auth=True,
    )

    # Behind a front-door proxy, a "go to Emby instead" answer must not be a
    # redirect: the proxy matches that URL too, sends it back here, and the
    # client loops forever -- turning fail-open into total failure. Answer 204
    # instead so the proxy serves the origin itself and the client never sees
    # the extra hop. Standalone callers still get the plain redirect.
    if not decision.redirected and request.headers.get("x-mediadeck-proxy"):
        return Response(status_code=204, headers={
            "X-Mediadeck-Fallback": decision.reason,
        })

    if not decision.target:
        raise HTTPException(409, "Emby origin not configured")
    return RedirectResponse(decision.target, status_code=302, headers={
        "X-Mediadeck-Node": decision.node or "",
        "X-Mediadeck-Decision": decision.reason,
    })


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


@app.get("/api/emby/latest", dependencies=[Depends(_auth)])
async def emby_latest(limit: int = 12) -> list[dict[str, Any]]:
    """Recently added titles for the dashboard poster wall.

    Cached for a minute: the dashboard auto-refreshes every 30s, and "what was
    added today" does not change between two consecutive renders. The artwork
    itself is served by the cached-image route, so a warm wall costs Emby
    nothing beyond this one list query.
    """
    limit = max(1, min(limit, 24))
    return await app.state.cache.resolve(
        f"emby:latest:{limit}",
        lambda: app.state.emby.latest_items(limit),
        ttl=60,
    )


async def _sessions_with_speed() -> list[dict[str, Any]]:
    """Active sessions annotated with live bandwidth.

    Shared by the REST endpoint and the SSE topic on purpose: when only the
    REST path decorated the payload, speeds appeared on manual refresh and
    vanished on every live push, which reads as "the number is frozen".

    Speed prefers what the *node* measured on the wire (nginx speed log,
    keyed by anonymised user tag). Sessions served by the Emby origin have no
    node measurement and fall back to the usage sampler's estimate, flagged
    so the UI can mark it approximate.
    """
    # Short TTL: sessions must still feel live, but a 30s auto-refresh plus
    # page switches should not hammer Emby.
    sessions = await app.state.cache.resolve(
        "emby:sessions", app.state.emby.active_sessions, ttl=5
    )
    est = app.state.usage.live_speeds()
    node_speeds = app.state.scheduler.user_speeds()
    # A 302 to a node that the probe has not attributed yet is still on the
    # node. Showing the sampler's bitrate estimate made those rows look like
    # origin traffic (the ≈ the operator read as "did not go through ca1").
    redirected_tags = {
        str(entry.get("utag") or "")
        for entry in app.state.playback.recent(200)
        if entry.get("redirected") and entry.get("utag")
    }
    out = []
    for session in sessions:
        s = dict(session)
        tag = user_tag(str(s.get("UserId") or ""))
        real = node_speeds.get(tag)
        if real is not None:
            # Node speeds are per *user*: concurrent sessions of one account
            # share the tag, so both rows show the account's wire rate.
            # ZERO is a real measurement, not an absence: a player fills its
            # buffer and then reads nothing for a minute, and during that
            # pause the wire truly carries 0 B/s for this viewer. Treating 0
            # as falsy pushed every buffered viewer back to the bitrate
            # estimate, which is exactly the wrong number being reported.
            s["SpeedBps"] = int(real)
            s["SpeedSource"] = "node"
        elif tag and tag in redirected_tags:
            s["SpeedBps"] = 0
            s["SpeedSource"] = "node"
        else:
            s["SpeedBps"] = int(est.get(str(s.get("Id") or ""), 0))
            s["SpeedSource"] = "estimate"
        # MB/s (owner's unit of choice); bytes stay available for precision.
        s["SpeedMBps"] = round(s["SpeedBps"] / 1048576, 1)
        out.append(s)
    return out


@app.get("/api/emby/sessions", dependencies=[Depends(_auth)])
async def emby_sessions() -> list[dict[str, Any]]:
    return await _sessions_with_speed()


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


async def _reissue_rate_caps(
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    reason: str = "",
    enforce: bool | None = None,
    kick: bool = True,
) -> None:
    """Drop cached signatures and stop playback so a new cap is picked up.

    The signed URL carries ``r=`` for up to six hours. Changing the number in
    the panel does nothing until the client asks for a new URL, which is why
    a 15 MB/s save looked ignored: every in-flight link was still ``r=0``.
    """
    app.state.cache.drop_prefix("rate:")
    if enforce is None:
        enforce = bool(
            app.state.settings_service.membership_config().get(
                "enforcement_enabled"))
    uids: set[str] = set()
    if user_id:
        uids.add(str(user_id))
    elif group_id:
        for member in app.state.members.list(group_id=group_id, limit=5000):
            overrides = member.get("overrides") or {}
            if "bandwidth_limit_kbps" in overrides:
                continue
            uid = member.get("emby_user_id")
            if uid:
                uids.add(str(uid))
    if enforce:
        for uid in uids:
            with contextlib.suppress(Exception):
                await app.state.enforcement.enforce_now(uid, reason)
    if kick and uids:
        with contextlib.suppress(Exception):
            await app.state.enforcement.terminate_users(uids, reason)


# ---- user groups -----------------------------------------------------------
@app.get("/api/groups", dependencies=[Depends(_auth)])
async def groups_list() -> list[dict[str, Any]]:
    return app.state.groups.list()


@app.post("/api/groups", dependencies=[Depends(_auth)])
async def groups_create(payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    group = app.state.groups.create(payload)
    app.state.members.audit(user, "group.create", group["id"], group["name"])
    return group


@app.put("/api/groups/{group_id}", dependencies=[Depends(_auth)])
async def groups_update(group_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    before = app.state.groups.get(group_id) or {}
    group = app.state.groups.update(group_id, payload)
    app.state.members.audit(user, "group.update", group_id, group["name"])
    if before.get("bandwidth_limit_kbps") != group.get("bandwidth_limit_kbps"):
        await _reissue_rate_caps(
            group_id=group_id, reason="用户组限速已更新")
    return group


@app.delete("/api/groups/{group_id}", dependencies=[Depends(_auth)])
async def groups_delete(group_id: str, user: str = Depends(_auth)) -> dict[str, bool]:
    app.state.groups.delete(group_id)
    app.state.members.audit(user, "group.delete", group_id)
    return {"deleted": True}


# ---- members ---------------------------------------------------------------
@app.get("/api/members", dependencies=[Depends(_auth)])
async def members_list(status: str | None = None, group_id: str | None = None,
                       role: str | None = None, search: str | None = None,
                       limit: int = 500) -> dict[str, Any]:
    """Members plus the Emby accounts that are not enrolled yet.

    Showing both in one payload is deliberate: the operator needs to see who is
    *not* being metered, which is exactly the population that costs money
    silently.
    """
    limit = max(1, min(int(limit or 500), 5000))
    members = app.state.members.list(status=status, group_id=group_id,
                                     role=role, search=search, limit=limit)
    truncated = len(members) >= limit
    known = {m["emby_user_id"] for m in members}
    unmanaged: list[dict[str, Any]] = []
    try:
        for u in await app.state.emby.list_users():
            if u["Id"] in known:
                continue
            policy = u.get("Policy") or {}
            unmanaged.append({
                "emby_user_id": u["Id"],
                "username": u.get("Name"),
                "is_admin": bool(policy.get("IsAdministrator")),
                "disabled": bool(policy.get("IsDisabled")),
            })
    except Exception as exc:  # noqa: BLE001 - the member list must still render
        return {"members": members, "unmanaged": [],
                "unmanaged_error": str(exc)[:200], "truncated": truncated,
                "limit": limit}
    if search:
        needle = search.lower()
        unmanaged = [u for u in unmanaged if needle in (u["username"] or "").lower()]
    return {"members": members, "unmanaged": unmanaged[:500],
            "unmanaged_total": len(unmanaged), "truncated": truncated,
            "limit": limit}


@app.post("/api/members/enroll-defaults", dependencies=[Depends(_auth)])
async def members_enroll_defaults(user: str = Depends(_auth)) -> dict[str, Any]:
    """Put every unmanaged, non-admin Emby account into the default group."""
    users = [u for u in await app.state.emby.list_users()
             if not (u.get("Policy") or {}).get("IsAdministrator")]
    enrolled = app.state.members.enroll_defaults(users, actor=user)
    return {"enrolled": enrolled}


@app.post("/api/members/{user_id}/roles", dependencies=[Depends(_auth)])
async def members_roles(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.members.set_roles(user_id, payload.get("roles"), actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None


@app.get("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_get(user_id: str, days: int = 30) -> dict[str, Any]:
    detail = app.state.members.detail(user_id)
    if not detail:
        raise HTTPException(404, "unknown member")
    days = max(1, min(int(days or 30), 400))
    stats = app.state.stats.member_detail(user_id, days)
    series = stats.get("series") or []
    usage = {
        "days": days,
        "bytes": sum(int(p.get("bytes") or 0) for p in series),
        "hours": round(sum(float(p.get("hours") or 0) for p in series), 2),
        "plays": sum(int(p.get("plays") or 0) for p in series),
        "series": series,
    }
    plays = list(stats.get("recent_plays") or [])[:20]
    sessions = []
    with contextlib.suppress(Exception):
        sessions = await app.state.emby.sessions_for_user(user_id)
    return {
        **detail,
        "usage": usage,
        "plays": plays,
        "series": series,
        "recent_plays": plays,
        "active_sessions": [{
            "id": s.get("Id"),
            "client": s.get("Client"),
            "device": s.get("DeviceName"),
            "item": (s.get("NowPlayingItem") or {}).get("Name"),
            "paused": bool((s.get("PlayState") or {}).get("IsPaused")),
        } for s in sessions],
    }


@app.put("/api/members/{user_id}/overrides", dependencies=[Depends(_auth)])
async def members_overrides(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                            user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        before = app.state.members.get(user_id) or {}
        member = app.state.members.set_overrides(user_id, payload, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    rate_changed = (
        before.get("bandwidth_limit_kbps") != member.get("bandwidth_limit_kbps"))
    if rate_changed or app.state.settings_service.membership_config()["enforcement_enabled"]:
        await _reissue_rate_caps(
            user_id=user_id, reason="成员限速已更新",
            enforce=app.state.settings_service.membership_config()[
                "enforcement_enabled"],
            kick=rate_changed)
    return member


@app.put("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_upsert(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                         user: str = Depends(_auth)) -> dict[str, Any]:
    username = str(payload.get("username") or "")
    if not username:
        with contextlib.suppress(Exception):
            for u in await app.state.emby.list_users():
                if u["Id"] == user_id:
                    username = u.get("Name") or ""
                    break
    before = app.state.members.get(user_id)
    member = app.state.members.upsert(user_id, username, payload, actor=user)
    rate_changed = bool(
        before and before.get("bandwidth_limit_kbps") != member.get(
            "bandwidth_limit_kbps"))
    if rate_changed:
        await _reissue_rate_caps(user_id=user_id, reason="成员限速已更新")
    elif app.state.settings_service.membership_config()["enforcement_enabled"]:
        with contextlib.suppress(Exception):
            await app.state.enforcement.enforce_now(user_id, "member updated")
    return member


@app.delete("/api/members/{user_id}", dependencies=[Depends(_auth)])
async def members_delete(user_id: str, delete_emby: bool = False,
                         user: str = Depends(_auth)) -> dict[str, bool]:
    """Un-enrol by default. Deleting the Emby account is an explicit extra."""
    try:
        app.state.members.delete(user_id, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    emby_deleted = False
    if delete_emby:
        with contextlib.suppress(Exception):
            emby_deleted = bool(await app.state.emby.delete_user(user_id))
        app.state.members.audit(
            user, "member.delete_emby", user_id,
            "Emby account deleted" if emby_deleted else "Emby delete failed",
            ok=emby_deleted)
    return {"deleted": True, "emby_deleted": emby_deleted}


@app.post("/api/members/{user_id}/renew", dependencies=[Depends(_auth)])
async def members_renew(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        member = app.state.members.renew(user_id, payload.get("days"), actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        with contextlib.suppress(Exception):
            await app.state.enforcement.enforce_now(user_id, "renewed")
    return member


@app.get("/api/access/rules", dependencies=[Depends(_auth)])
async def access_rules_list() -> list[dict[str, Any]]:
    return app.state.access.list()


@app.post("/api/access/rules", dependencies=[Depends(_auth)])
async def access_rule_add(payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        created = app.state.access.add(
            str(payload.get("kind") or ""),
            str(payload.get("pattern") or ""),
            str(payload.get("action") or "deny"),
            str(payload.get("note") or ""),
            bool(payload.get("enabled", True)))
    except ValueError as exc:
        # Validation happens at save time, where a person is present to read the
        # error, rather than at match time on the playback path.
        raise HTTPException(400, str(exc)) from None
    app.state.members.audit(user, "access.rule.add", "",
                            f"{created['kind']} {created['action']}")
    return created


@app.delete("/api/access/rules/{rule_id}", dependencies=[Depends(_auth)])
async def access_rule_remove(rule_id: int, user: str = Depends(_auth)) -> dict[str, bool]:
    if not app.state.access.remove(rule_id):
        raise HTTPException(404, "rule not found")
    app.state.members.audit(user, "access.rule.remove", str(rule_id), "")
    return {"removed": True}


@app.post("/api/access/rules/{rule_id}/enabled", dependencies=[Depends(_auth)])
async def access_rule_toggle(rule_id: int, payload: dict[str, Any] = Body(...),  # noqa: B008
                             user: str = Depends(_auth)) -> dict[str, bool]:
    enabled = bool(payload.get("enabled", True))
    if not app.state.access.set_enabled(rule_id, enabled):
        raise HTTPException(404, "rule not found")
    app.state.members.audit(user, "access.rule.toggle", str(rule_id),
                            f"enabled={enabled}")
    return {"enabled": enabled}


@app.get("/api/access/blocks", dependencies=[Depends(_auth)])
async def access_blocks(limit: int = 100) -> list[dict[str, Any]]:
    """Refused requests. A block that leaves no trace is indistinguishable
    from a broken node, and the operator debugs the wrong thing."""
    return app.state.access.blocks(limit)


@app.get("/api/sharing", dependencies=[Depends(_auth)])
async def sharing_findings(limit: int = 50) -> dict[str, Any]:
    """Accounts seen playing from more than one network at once.

    Reported, never acted on: the cost of being wrong is locking out a paying
    member over a VPN reconnect, so the judgement stays with a person.
    """
    return {
        "items": app.state.sharing.recent(limit),
        "status": app.state.sharing.status(),
    }


@app.post("/api/members/bulk", dependencies=[Depends(_auth)])
async def members_bulk(payload: dict[str, Any] = Body(...),  # noqa: B008
                       user: str = Depends(_auth)) -> dict[str, Any]:
    """Apply one action to many members, reporting per-member outcomes.

    Partial success is the normal case: one member may have been deleted in
    another tab while the operator was ticking boxes. Failing the whole batch
    for that would make the operator redo work that already succeeded, so each
    id is attempted independently and the failures are named.

    Deliberately excludes deletion. A mis-click on a checkbox column is easy,
    and a bulk delete is the one action with no way back.
    """
    action = str(payload.get("action") or "").strip()
    ids = payload.get("user_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "user_ids 不能为空")
    if len(ids) > 500:
        raise HTTPException(400, "单次最多处理 500 个成员")

    allowed = {"renew", "suspend", "activate", "reset-traffic"}
    if action not in allowed:
        raise HTTPException(400, f"action 必须是 {'/'.join(sorted(allowed))} 之一")

    days = payload.get("days")
    if action == "renew":
        try:
            days = int(days) if days is not None else None
        except (TypeError, ValueError):
            raise HTTPException(400, "days 必须是整数") from None
        if days is not None and not 1 <= days <= 3650:
            raise HTTPException(400, "续期天数必须在 1–3650 之间")

    ok: list[str] = []
    failed: list[dict[str, str]] = []
    for raw in ids:
        user_id = str(raw)
        try:
            if action == "renew":
                app.state.members.renew(user_id, days, actor=user)
            elif action == "suspend":
                app.state.members.set_status(user_id, "suspended", actor=user)
            elif action == "activate":
                app.state.members.set_status(user_id, "active", actor=user)
            else:
                app.state.members.reset_traffic(user_id, actor=user)
            ok.append(user_id)
        except KeyError:
            failed.append({"user_id": user_id, "error": "成员不存在"})
        except Exception as exc:  # noqa: BLE001 - reported per member, not raised
            failed.append({"user_id": user_id, "error": str(exc)})

    app.state.members.audit(
        user, f"member.bulk.{action}", "",
        f"requested={len(ids)} ok={len(ok)} failed={len(failed)}")

    # Enforcement runs once after the batch rather than per member: pushing the
    # same policy change 200 times would hammer Emby for no extra correctness.
    if ok and app.state.settings_service.membership_config()["enforcement_enabled"]:
        for user_id in ok:
            with contextlib.suppress(Exception):
                await app.state.enforcement.enforce_now(user_id, f"bulk {action}")

    return {"action": action, "requested": len(ids),
            "ok": len(ok), "failed": failed}


@app.post("/api/members/{user_id}/reset-traffic", dependencies=[Depends(_auth)])
async def members_reset_traffic(user_id: str, user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        member = app.state.members.reset_traffic(user_id, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        with contextlib.suppress(Exception):
            await app.state.enforcement.enforce_now(user_id, "traffic reset")
    return member


@app.post("/api/members/{user_id}/status", dependencies=[Depends(_auth)])
async def members_status(user_id: str, payload: dict[str, Any] = Body(...),  # noqa: B008
                         user: str = Depends(_auth)) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    try:
        member = app.state.members.set_status(user_id, status, actor=user)
    except KeyError:
        raise HTTPException(404, "unknown member") from None
    if app.state.settings_service.membership_config()["enforcement_enabled"]:
        with contextlib.suppress(Exception):
            await app.state.enforcement.enforce_now(user_id, f"status={status}")
        if status in ("suspended", "pending"):
            with contextlib.suppress(Exception):
                await app.state.enforcement.terminate_sessions(user_id, "账号已停用")
    return member


async def _reset_member_password(user_id: str, payload: dict[str, Any],
                                 actor: str) -> dict[str, Any]:
    """Set or randomise a member's Emby password.

    Returned in cleartext exactly once, because the operator has to relay it;
    it is never stored by the panel.
    """
    password = str(payload.get("password") or "") or random_password()
    if len(password) < 6:
        raise HTTPException(422, "密码至少 6 位")
    if not await app.state.emby.set_user_password(user_id, password):
        raise HTTPException(404, "unknown user")
    app.state.members.audit(actor, "member.password", user_id, "password changed")
    return {"ok": True, "password": password}


@app.post("/api/members/{user_id}/password", dependencies=[Depends(_auth)])
async def members_password(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                           user: str = Depends(_auth)) -> dict[str, Any]:
    return await _reset_member_password(user_id, payload, user)


@app.post("/api/members/{user_id}/actions/reset-password", dependencies=[Depends(_auth)])
async def members_reset_password(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                 user: str = Depends(_auth)) -> dict[str, Any]:
    return await _reset_member_password(user_id, payload, user)


async def _kick_member(user_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    reason = str(payload.get("reason") or "管理员结束了此次播放")
    stopped = await app.state.enforcement.terminate_sessions(user_id, reason)
    app.state.members.audit(actor, "member.kick", user_id, f"{stopped} session(s)")
    return {"stopped": stopped}


@app.post("/api/members/{user_id}/kick", dependencies=[Depends(_auth)])
async def members_kick(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                       user: str = Depends(_auth)) -> dict[str, Any]:
    return await _kick_member(user_id, payload, user)


@app.post("/api/members/{user_id}/actions/kick", dependencies=[Depends(_auth)])
async def members_action_kick(user_id: str, payload: dict[str, Any] = Body(default={}),  # noqa: B008
                              user: str = Depends(_auth)) -> dict[str, Any]:
    return await _kick_member(user_id, payload, user)


@app.get("/api/members/{user_id}/devices", dependencies=[Depends(_auth)])
async def members_devices(user_id: str) -> list[dict[str, Any]]:
    return app.state.members.devices(user_id)


async def _set_device_blocked(user_id: str, device_id: str, blocked: bool,
                              actor: str) -> dict[str, Any]:
    try:
        row = app.state.members.set_device_blocked(
            user_id, device_id, blocked, actor=actor)
    except KeyError:
        raise HTTPException(404, "unknown device") from None
    return {"ok": True, "blocked": bool(row.get("blocked")), "device": row}


@app.post("/api/members/{user_id}/devices/{device_id}/block", dependencies=[Depends(_auth)])
async def members_block_device(user_id: str, device_id: str,
                               payload: dict[str, Any] = Body(default={}),  # noqa: B008
                               user: str = Depends(_auth)) -> dict[str, Any]:
    # Dedicated /unblock is the canonical clear; this still accepts blocked=false
    # so the existing drawer button keeps working.
    return await _set_device_blocked(
        user_id, device_id, bool(payload.get("blocked", True)), user)


@app.post("/api/members/{user_id}/devices/{device_id}/unblock", dependencies=[Depends(_auth)])
async def members_unblock_device(user_id: str, device_id: str,
                                 user: str = Depends(_auth)) -> dict[str, Any]:
    return await _set_device_blocked(user_id, device_id, False, user)


@app.delete("/api/members/{user_id}/devices/{device_id}", dependencies=[Depends(_auth)])
async def members_forget_device(user_id: str, device_id: str,
                                user: str = Depends(_auth)) -> dict[str, bool]:
    changed = app.state.db.execute(
        "DELETE FROM devices WHERE emby_user_id=? AND device_id=?",
        (user_id, device_id))
    if not changed:
        raise HTTPException(404, "unknown device")
    app.state.members.audit(user, "device.forget", user_id, device_id)
    return {"deleted": True}


# ---- enforcement -----------------------------------------------------------
@app.get("/api/enforcement/preview", dependencies=[Depends(_auth)])
async def enforcement_preview(user_id: str | None = None) -> dict[str, Any]:
    """Dry-run: exactly what would be written to Emby, and to whom."""
    return await app.state.enforcement.reconcile(apply=False, user_id=user_id)


@app.post("/api/enforcement/apply", dependencies=[Depends(_auth)])
async def enforcement_apply(payload: dict[str, Any] = Body(default={}),  # noqa: B008
                            user: str = Depends(_auth)) -> dict[str, Any]:
    result = await app.state.enforcement.reconcile(
        apply=True, user_id=payload.get("user_id"), force=bool(payload.get("force")))
    app.state.members.audit(user, "enforce.manual", payload.get("user_id") or "*",
                            f"applied={result.get('applied')}")
    return result


# ---- statistics ------------------------------------------------------------
@app.get("/api/stats/overview", dependencies=[Depends(_auth)])
async def stats_overview(days: int = 30) -> dict[str, Any]:
    return app.state.stats.overview(days)


@app.get("/api/stats/daily", dependencies=[Depends(_auth)])
async def stats_daily(days: int = 30) -> list[dict[str, Any]]:
    return app.state.stats.daily_series(days)


@app.get("/api/stats/top-users", dependencies=[Depends(_auth)])
async def stats_top_users(days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    return app.state.stats.top_users(days, limit)


@app.get("/api/stats/top-titles", dependencies=[Depends(_auth)])
async def stats_top_titles(days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    return app.state.stats.top_titles(days, limit)


@app.get("/api/stats/clients", dependencies=[Depends(_auth)])
async def stats_clients(days: int = 30) -> list[dict[str, Any]]:
    return app.state.stats.client_breakdown(days)


@app.get("/api/stats/nodes", dependencies=[Depends(_auth)])
async def stats_nodes(days: int = 30) -> list[dict[str, Any]]:
    return app.state.stats.node_breakdown(days)


@app.get("/api/stats/play-methods", dependencies=[Depends(_auth)])
async def stats_play_methods(days: int = 30) -> dict[str, Any]:
    return app.state.stats.play_method_breakdown(days)


@app.get("/api/audit", dependencies=[Depends(_auth)])
async def audit_log(limit: int = 100, offset: int = 0, subject: str | None = None,
                    actor: str | None = None, action: str | None = None
                    ) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    items = app.state.members.audit_log(
        limit, offset=offset, subject=subject, actor=actor, action=action)
    total = app.state.members.audit_count(
        subject=subject, actor=actor, action=action)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/usage/status", dependencies=[Depends(_auth)])
async def usage_status() -> dict[str, Any]:
    return {
        **app.state.usage.status(),
        "membership": app.state.settings_service.membership_config(),
    }


# ---- image cache -----------------------------------------------------------
@app.get("/api/settings/image-cache", dependencies=[Depends(_auth)])
async def image_cache_get() -> dict[str, Any]:
    return {**app.state.settings_service.image_cache_config(),
            "stats": app.state.images.stats()}


@app.put("/api/settings/image-cache", dependencies=[Depends(_auth)])
async def image_cache_save(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    saved = app.state.settings_service.save_image_cache(payload)
    # Rebuild rather than mutate: the budget and age bounds are constructor
    # arguments, and a stale sweeper would keep enforcing the old numbers.
    app.state.images = ImageCache(
        settings().data_dir / "imagecache",
        max_bytes=saved["max_bytes"],
        max_age_seconds=saved["max_age_days"] * 86400,
    )
    return {**saved, "stats": app.state.images.stats()}


@app.post("/api/settings/image-cache/clear", dependencies=[Depends(_auth)])
async def image_cache_clear(user: str = Depends(_auth)) -> dict[str, Any]:
    removed = app.state.images.clear()
    app.state.members.audit(user, "imagecache.clear", "", f"{removed} entries")
    return {"removed": removed}


@app.post("/api/settings/image-cache/sweep", dependencies=[Depends(_auth)])
async def image_cache_sweep() -> dict[str, Any]:
    return app.state.images.sweep(force=True)


@app.get("/api/settings/telegram", dependencies=[Depends(_auth)])
async def telegram_get() -> dict[str, Any]:
    # Never the raw token: it is a bearer credential, and anyone holding it can
    # read every message the bot receives and post as it.
    return {**app.state.settings_service.telegram_public(),
            "status": app.state.telegram.status()}


@app.post("/api/settings/telegram", dependencies=[Depends(_auth)])
async def telegram_save(payload: dict[str, Any] = Body(...),  # noqa: B008
                        user: str = Depends(_auth)) -> dict[str, Any]:
    saved = app.state.settings_service.save_telegram(payload)
    # The audit trail records that the token changed, never what it changed to.
    app.state.members.audit(
        user, "settings.telegram", "",
        f"enabled={saved['enabled']} token_set={saved['bot_token_set']}")
    return {**saved, "status": app.state.telegram.status()}


@app.post("/api/settings/telegram/verify", dependencies=[Depends(_auth)])
async def telegram_verify() -> dict[str, Any]:
    """Ask Telegram who the bot is. Proves the token without printing it."""
    return await app.state.telegram.verify()


@app.get("/api/telegram/requests", dependencies=[Depends(_auth)])
async def telegram_requests() -> list[dict[str, Any]]:
    """Claim and rebind requests awaiting a decision.

    Registration never appears here: the chat itself proves who is asking, so
    a brand-new account needs no review. These two do, because both are
    attempts to take control of an account the requester cannot otherwise
    prove they own.
    """
    return app.state.telegram.pending_requests()


@app.post("/api/telegram/requests/{request_id}/review", dependencies=[Depends(_auth)])
async def telegram_request_review(request_id: int,
                                  payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                  user: str = Depends(_auth)) -> dict[str, Any]:
    approve = bool(payload.get("approve", False))
    try:
        result = app.state.telegram.review_request(request_id, approve, reviewer=user)
    except KeyError:
        raise HTTPException(404, "request not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    # Tell the requester either way: silence reads as the operator ignoring
    # them, and they open a second request.
    with contextlib.suppress(Exception):
        await app.state.telegram.send(
            result["tg_user_id"],
            "✅ 申请已通过，账号已关联到这个 Telegram。" if approve
            else "❌ 申请未通过，如有疑问请联系管理员。")
    return result


@app.post("/api/telegram/group-audit", dependencies=[Depends(_auth)])
async def telegram_group_audit() -> dict[str, Any]:
    """Which linked members have left the required group.

    Reported, never enforced: leaving a chat is not the same as stopping
    paying, and suspending on that basis is a person's call.
    """
    return await app.state.telegram.audit_group_membership()


@app.post("/api/telegram/rankings/send", dependencies=[Depends(_auth)])
async def telegram_send_rankings(payload: dict[str, Any] = Body(default={}),  # noqa: B008
                                 user: str = Depends(_auth)) -> dict[str, bool]:
    cfg = app.state.settings_service.telegram_config()
    chat = str(payload.get("chat_id") or cfg.get("rankings_chat") or "").strip()
    if not chat:
        raise HTTPException(400, "未配置排行榜推送目标")
    days = int(payload.get("days") or 1)
    ok = await app.state.telegram.broadcast_rankings(chat, days)
    app.state.members.audit(user, "telegram.rankings", "", f"days={days} ok={ok}")
    return {"sent": ok}


@app.post("/api/members/{user_id}/telegram/unbind", dependencies=[Depends(_auth)])
async def telegram_unbind(user_id: str, user: str = Depends(_auth)) -> dict[str, Any]:
    try:
        return app.state.members.unbind_telegram(user_id, actor=user)
    except KeyError:
        raise HTTPException(404, "member not found") from None


@app.get("/api/settings/membership", dependencies=[Depends(_auth)])
async def membership_get() -> dict[str, Any]:
    return app.state.settings_service.membership_config()


@app.put("/api/settings/membership", dependencies=[Depends(_auth)])
async def membership_save(payload: dict[str, Any] = Body(...),  # noqa: B008
                          user: str = Depends(_auth)) -> dict[str, Any]:
    saved = app.state.settings_service.save_membership(payload)
    app.state.members.audit(user, "settings.membership", "",
                            f"enforcement={saved['enforcement_enabled']}")
    return saved


@app.get("/emby/Items/{item_id}/Images/{image_type}", include_in_schema=False)
async def cached_image(item_id: str, image_type: str, request: Request) -> Response:
    """Serve Emby artwork from local disk.

    A library grid fires dozens of poster requests and Emby re-derives each one
    every time. Point the front door here and repeat views become disk reads,
    freeing Emby's CPU exactly when the UI needs to feel instant.

    Unknown image types fall through to Emby rather than erroring: this sits on
    a public path, so it must never be the reason artwork disappears.
    """
    origin = app.state.settings_service.emby_config().get("url", "").rstrip("/")
    query = dict(request.query_params)
    passthrough = f"{origin}/emby/Items/{item_id}/Images/{image_type}"
    if query:
        passthrough = f"{passthrough}?{urlencode(query)}"

    cfg = app.state.settings_service.image_cache_config()
    if not cfg["enabled"] or image_type not in ALLOWED_IMAGE_TYPES or not origin:
        return RedirectResponse(passthrough, status_code=302)

    key = app.state.images.key(item_id, image_type, query)

    async def produce() -> tuple[bytes, str, str] | None:
        emby_cfg = app.state.settings_service.emby_config()
        headers = {"X-Emby-Token": emby_cfg.get("api_key", "")}
        async with httpx.AsyncClient(
                timeout=20, verify=bool(emby_cfg.get("verify_ssl", True))) as client:
            r = await client.get(passthrough, headers=headers)
            if r.status_code != 200 or not r.content:
                return None
            return (r.content,
                    r.headers.get("content-type", "image/jpeg"),
                    r.headers.get("etag", ""))

    result = await app.state.images.fetch(key, produce)
    if not result:
        # Cache miss and upstream had nothing: let Emby answer directly so a
        # transient failure never becomes a permanently broken image.
        return RedirectResponse(passthrough, status_code=302)

    data, content_type, etag = result
    # Honour conditional requests: browsers revalidate artwork constantly, and
    # a 304 avoids re-sending megabytes of posters on every page load.
    if etag and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    headers = {
        "Cache-Control": "public, max-age=604800",
        "X-Mediadeck-Cache": "hit",
    }
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type=content_type, headers=headers)
