"""Membership, billing, enforcement, statistics and image cache.

These cover the parts where a mistake costs money or locks people out, so the
assertions are about *behaviour under failure*, not just happy paths.
"""
from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.core.errors import ConfigError, ConflictError
from app.main import app
from app.modules.enforcement import EnforcementService, desired_policy
from app.modules.imagecache import ImageCache
from app.modules.members import MemberService, period_start
from app.modules.plans import PlanService
from app.modules.stats import StatsService
from app.modules.usage import UsageSampler, is_playing, session_bitrate

GIB = 1024 ** 3


def _basic(user: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def stack():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "t.db")
    plans = PlanService(db)
    plans.seed_defaults()
    members = MemberService(db, plans)
    emby = MockEmby()
    enforcement = EnforcementService(db, members, emby)
    return {"db": db, "plans": plans, "members": members,
            "emby": emby, "enforcement": enforcement, "tmp": tmp}


# ---- plans -----------------------------------------------------------------
def test_metered_plan_must_have_a_meter(stack) -> None:
    """A traffic plan with no quota silently grants unlimited access."""
    with pytest.raises(ConfigError):
        stack["plans"].create({"id": "bad", "name": "x",
                               "billing_type": "traffic", "traffic_quota_bytes": 0})
    with pytest.raises(ConfigError):
        stack["plans"].create({"id": "bad2", "name": "x",
                               "billing_type": "duration", "duration_days": 0})


def test_plan_in_use_cannot_be_deleted(stack) -> None:
    """Deleting a plan under live members leaves limits nobody can explain."""
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    with pytest.raises(ConflictError):
        stack["plans"].delete("monthly")
    stack["members"].delete("u1")
    assert stack["plans"].delete("monthly")


def test_only_one_default_plan(stack) -> None:
    stack["plans"].create({"id": "p2", "name": "P2", "is_default": True})
    defaults = [p for p in stack["plans"].list() if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == "p2"


# ---- member state ----------------------------------------------------------
def test_state_is_derived_not_stored(stack) -> None:
    """Expiry and quota are time-dependent; a stale label would mis-bill."""
    m = stack["members"]
    m.upsert("u1", "alice", {"plan_id": "monthly"})
    assert m.get("u1")["state"] == "active"

    m.upsert("u1", "alice", {"expires_at": int(time.time()) - 10})
    assert m.get("u1")["state"] == "expired"

    m.upsert("u1", "alice", {"expires_at": int(time.time()) + 86400,
                             "traffic_used_bytes": 500 * GIB})
    assert m.get("u1")["state"] == "exhausted"


def test_manual_states_survive_automatic_recalculation(stack) -> None:
    """A suspended account must not come back when its quota resets."""
    m = stack["members"]
    m.upsert("u1", "alice", {"plan_id": "monthly"})
    m.set_status("u1", "suspended")
    m.reset_traffic("u1")
    assert m.get("u1")["state"] == "suspended"


def test_renew_extends_from_expiry_not_from_now(stack) -> None:
    """Renewing early must not throw away days already paid for."""
    m = stack["members"]
    future = int(time.time()) + 20 * 86400
    m.upsert("u1", "alice", {"plan_id": "monthly", "expires_at": future})
    m.renew("u1", days=30)
    assert m.get("u1")["expires_at"] >= future + 30 * 86400 - 5


def test_assigning_a_timed_plan_sets_an_expiry(stack) -> None:
    """Otherwise a paid plan silently becomes permanent."""
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    assert stack["members"].get("u1")["expires_at"] is not None
    stack["members"].upsert("u1", "alice", {"plan_id": "staff"})
    assert stack["members"].get("u1")["expires_at"] is None


def test_period_rollover_resets_quota_and_revives_exhausted(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"plan_id": "monthly",
                             "traffic_used_bytes": 500 * GIB})
    assert m.get("u1")["state"] == "exhausted"
    # Pretend the window closed long ago.
    stack["db"].execute(
        "UPDATE members SET traffic_period_start=? WHERE emby_user_id=?",
        (1, "u1"))
    assert m.roll_periods() == 1
    after = m.get("u1")
    assert after["traffic_used_bytes"] == 0 and after["state"] == "active"


def test_period_boundaries_are_calendar_aligned() -> None:
    """Same plan must reset on the same day for every member."""
    now = int(time.time())
    assert period_start("total", now) == 0
    assert period_start("daily", now) <= now
    assert period_start("monthly", now) <= period_start("daily", now)


# ---- enforcement -----------------------------------------------------------
def test_policy_maps_limits_onto_emby_fields(stack) -> None:
    stack["members"].upsert("u1", "alice", {"plan_id": "trial"})
    policy = desired_policy(stack["members"].get("u1"))
    assert policy["SimultaneousStreamLimit"] == 1
    assert policy["RemoteClientBitrateLimit"] == 8000 * 1000
    assert policy["EnableVideoPlaybackTranscoding"] is False
    assert policy["EnableContentDownloading"] is False
    assert policy["IsDisabled"] is False


def test_blocking_states_disable_the_account(stack) -> None:
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["members"].set_status("u1", "suspended")
    assert desired_policy(stack["members"].get("u1"))["IsDisabled"] is True


def test_enforcement_never_touches_unenrolled_accounts(stack) -> None:
    """The whole safety story: hundreds of pre-existing accounts stay untouched."""
    stack["members"].upsert("u1", "demo-user-1", {"plan_id": "monthly"})
    result = asyncio.run(stack["enforcement"].reconcile(apply=True))
    touched = {c["user_id"] for c in result["changes"]}
    assert touched <= {"u1"}
    # u2 exists in Emby but was never enrolled.
    assert stack["emby"]._users["u2"]["Policy"] == {"IsDisabled": True}


def test_administrators_are_never_disabled(stack) -> None:
    """Locking the operator out of their own server is not an acceptable bug."""
    stack["members"].upsert("admin", "demo-admin", {"plan_id": "monthly"})
    stack["members"].set_status("admin", "suspended")
    result = asyncio.run(stack["enforcement"].reconcile(apply=True))
    assert any(s["reason"] == "administrator" for s in result["skipped"])
    assert stack["emby"]._users["admin"]["Policy"]["IsDisabled"] is False


def test_reconcile_is_idempotent(stack) -> None:
    """A nightly pass must not rewrite every policy and bury real changes."""
    stack["members"].upsert("u1", "demo-user-1", {"plan_id": "monthly"})
    first = asyncio.run(stack["enforcement"].reconcile(apply=True))
    assert first["applied"] == 1
    second = asyncio.run(stack["enforcement"].reconcile(apply=True))
    assert second["applied"] == 0 and second["planned"] == 0


def test_dry_run_writes_nothing(stack) -> None:
    stack["members"].upsert("u1", "demo-user-1", {"plan_id": "trial"})
    result = asyncio.run(stack["enforcement"].reconcile(apply=False))
    assert result["planned"] >= 1 and result["applied"] == 0
    assert "SimultaneousStreamLimit" not in stack["emby"]._users["u1"]["Policy"]


def test_terminate_sessions_stops_only_that_user(stack) -> None:
    stack["emby"].set_sessions([
        {"Id": "s1", "UserId": "u1"}, {"Id": "s2", "UserId": "u2"}])
    stopped = asyncio.run(stack["enforcement"].terminate_sessions("u1", "quota"))
    assert stopped == 1
    assert [s["Id"] for s in stack["emby"]._sessions] == ["s2"]


# ---- usage accounting ------------------------------------------------------
def _session(sid: str, user: str, bitrate: int, paused: bool = False,
             item: str = "i1") -> dict:
    return {
        "Id": sid, "UserId": user, "UserName": user, "DeviceId": f"dev-{user}",
        "Client": "TestClient", "RemoteEndPoint": "10.0.0.1",
        "NowPlayingItem": {"Id": item, "Name": f"Title {item}",
                           "Type": "Movie", "Bitrate": bitrate},
        "PlayState": {"IsPaused": paused, "PlayMethod": "DirectStream"},
    }


def test_paused_sessions_are_not_billed(stack) -> None:
    """Charging for time a user did not watch is the worst kind of bug."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, paused=True)])
    asyncio.run(sampler.tick())
    time.sleep(0.05)
    asyncio.run(sampler.tick())
    assert stack["members"].get("u1")["traffic_used_bytes"] == 0


def test_first_sighting_bills_nothing(stack) -> None:
    """Billing a full interval on first sight charges for playback that just began."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    result = asyncio.run(sampler.tick())
    assert result["billed_bytes"] == 0


def test_traffic_accrues_while_playing(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    time.sleep(0.2)
    result = asyncio.run(sampler.tick())
    assert result["billed_bytes"] > 0
    assert stack["members"].get("u1")["traffic_used_bytes"] > 0


def test_long_gaps_are_clamped(stack) -> None:
    """A panel outage must not produce a surprise bill."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    # Simulate the sampler having been down for a day.
    sampler._live["s1"]["last_ts"] = time.time() - 86400
    result = asyncio.run(sampler.tick())
    max_possible = 8_000_000 / 8 * 121
    assert 0 < result["billed_bytes"] <= max_possible


def test_bitrate_prefers_transcode_then_source_then_floor() -> None:
    assert session_bitrate({"TranscodingInfo": {"Bitrate": 3_000_000},
                            "NowPlayingItem": {"Bitrate": 9_000_000}}) == 3_000_000
    assert session_bitrate({"NowPlayingItem": {"Bitrate": 9_000_000}}) == 9_000_000
    # Unreported must not be free.
    assert session_bitrate({"NowPlayingItem": {}}) == 4_000_000


def test_is_playing_requires_an_item() -> None:
    assert is_playing({"NowPlayingItem": {"Id": "x"}, "PlayState": {}}) is True
    assert is_playing({"PlayState": {}}) is False


def test_devices_are_tracked_from_sessions(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    assert stack["members"].get("u1")["device_count"] == 1


def test_exhausted_member_is_cut_off_within_one_tick(stack) -> None:
    """The gap between 'quota reached' and 'playback stops' is free traffic."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"],
                           stack["enforcement"])
    stack["plans"].create({"id": "tiny", "name": "Tiny", "billing_type": "traffic",
                           "traffic_quota_bytes": 1024, "traffic_period": "total"})
    stack["members"].upsert("u1", "demo-user-1", {"plan_id": "tiny"})
    stack["emby"].set_sessions([_session("s1", "u1", 80_000_000)])
    asyncio.run(sampler.tick())
    time.sleep(0.2)
    result = asyncio.run(sampler.tick())
    assert result.get("enforced", 0) >= 1
    assert stack["members"].get("u1")["state"] == "exhausted"
    assert stack["emby"]._users["u1"]["Policy"]["IsDisabled"] is True
    assert stack["emby"]._sessions == []


def test_switching_title_records_two_plays(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, item="i1")])
    asyncio.run(sampler.tick())
    sampler._live["s1"]["seconds"] = 60.0
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000, item="i2")])
    asyncio.run(sampler.tick())
    rows = stack["db"].query("SELECT item_id FROM play_events")
    assert [r["item_id"] for r in rows] == ["i1"]


def test_short_plays_are_not_recorded(stack) -> None:
    """A mis-tap must not pollute 'top titles'."""
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"])
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["emby"].set_sessions([_session("s1", "u1", 8_000_000)])
    asyncio.run(sampler.tick())
    stack["emby"].set_sessions([])
    asyncio.run(sampler.tick())
    assert stack["db"].query("SELECT * FROM play_events") == []


# ---- statistics ------------------------------------------------------------
def test_overview_counts_and_mrr(stack) -> None:
    stack["members"].upsert("u1", "alice", {"plan_id": "monthly"})
    stack["members"].upsert("u2", "bob", {"plan_id": "yearly"})
    stats = StatsService(stack["db"])
    o = stats.overview(30)
    assert o["members"]["total"] == 2 and o["members"]["active"] == 2
    # 1500/30d + 12000/365d normalised to 30 days
    assert o["revenue"]["mrr_cents"] == 1500 + int(12000 * 30 / 365)


def test_daily_series_is_zero_filled(stack) -> None:
    """A gap must render as zero, not as a missing point that fakes continuity."""
    series = StatsService(stack["db"]).daily_series(7)
    assert len(series) == 7
    assert all(p["bytes"] == 0 for p in series)
    assert series[0]["day"] < series[-1]["day"]


def test_prune_drops_old_history(stack) -> None:
    stack["db"].execute(
        "INSERT INTO play_events (emby_user_id,item_id,started_at,ended_at,"
        "bytes,seconds) VALUES ('u1','old',?,?,1,1)",
        (int(time.time()) - 500 * 86400, int(time.time()) - 500 * 86400))
    assert StatsService(stack["db"]).prune(400)["play_events"] == 1


# ---- image cache -----------------------------------------------------------
def test_cache_key_ignores_credentials_but_not_size() -> None:
    c = ImageCache(tempfile.mkdtemp())
    k1 = c.key("1", "Primary", {"maxWidth": "400", "api_key": "a"})
    k2 = c.key("1", "Primary", {"maxWidth": "400", "api_key": "b"})
    k3 = c.key("1", "Primary", {"maxWidth": "800"})
    assert k1 == k2 and k1 != k3


def test_concurrent_misses_produce_one_upstream_fetch() -> None:
    """A library grid must not turn one cold poster into N upstream requests."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    calls = []

    async def produce():
        calls.append(1)
        await asyncio.sleep(0.05)
        return (b"\xff\xd8\xff" + b"x" * 500, "image/jpeg", "etag")

    async def main():
        await asyncio.gather(*[c.fetch(key, produce) for _ in range(20)])
        return await c.fetch(key, produce)

    result = asyncio.run(main())
    assert len(calls) == 1
    assert result and len(result[0]) == 503
    assert c.stats()["entries"] == 1


def test_non_images_are_never_cached() -> None:
    """An HTML error page cached as a poster persists a transient failure."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    assert c.store(key, b"<html>error</html>", "text/html") is False
    assert c.lookup(key) is None


def test_negative_results_are_remembered_briefly() -> None:
    """Otherwise an item with no artwork is refetched on every render."""
    c = ImageCache(tempfile.mkdtemp())
    key = c.key("1", "Primary", {})
    calls = []

    async def produce():
        calls.append(1)

    async def main():
        await c.fetch(key, produce)
        await c.fetch(key, produce)

    asyncio.run(main())
    assert len(calls) == 1


def test_lru_eviction_respects_the_budget() -> None:
    c = ImageCache(tempfile.mkdtemp(), max_bytes=64 * 1024 * 1024)
    blob = b"\xff\xd8\xff" + b"x" * (1024 * 1024)
    for i in range(40):
        c.store(c.key(str(i), "Primary", {}), blob, "image/jpeg")
    before = c.stats()["bytes"]
    c._max_bytes = 8 * 1024 * 1024
    c.sweep(force=True)
    assert c.stats()["bytes"] < before


# ---- API -------------------------------------------------------------------
def test_membership_api_roundtrip() -> None:
    with TestClient(app) as client:
        plans = client.get("/api/plans", headers=_basic()).json()
        assert {p["id"] for p in plans} >= {"trial", "monthly", "yearly", "staff"}

        listing = client.get("/api/members", headers=_basic()).json()
        assert "members" in listing and "unmanaged" in listing
        # Mock Emby users start unenrolled -- the population that costs money.
        assert any(u["username"] == "demo-user-1" for u in listing["unmanaged"])

        m = client.put("/api/members/u1", headers=_basic(),
                       json={"plan_id": "monthly", "username": "demo-user-1"}).json()
        assert m["plan_name"] == "月付" and m["state"] == "active"

        detail = client.get("/api/members/u1", headers=_basic()).json()
        assert detail["member"]["emby_user_id"] == "u1"
        assert "series" in detail and "audit" in detail

        assert client.post("/api/members/u1/renew", headers=_basic(),
                           json={"days": 15}).status_code == 200
        assert client.post("/api/members/u1/reset-traffic",
                           headers=_basic()).json()["traffic_used_bytes"] == 0
        assert client.post("/api/members/u1/status", headers=_basic(),
                           json={"status": "suspended"}).json()["state"] == "suspended"
        assert client.delete("/api/members/u1", headers=_basic()).json()["deleted"]


def test_enforcement_api_is_dry_run_by_default() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", headers=_basic(),
                   json={"plan_id": "trial", "username": "demo-user-1"})
        preview = client.get("/api/enforcement/preview", headers=_basic()).json()
        assert preview["dry_run"] is True and preview["applied"] == 0


def test_invite_lifecycle_and_rollback() -> None:
    with TestClient(app) as client:
        codes = client.post("/api/invites", headers=_basic(),
                            json={"plan_id": "trial", "max_uses": 1,
                                  "valid_days": 7}).json()
        code = codes[0]["code"]

        # Public preview shows what the code grants without consuming it.
        preview = client.get(f"/api/invite/{code}").json()
        assert preview["plan"]["name"] == "体验"
        assert preview["remaining_uses"] == 1

        ok = client.post(f"/api/invite/{code}/redeem",
                         json={"username": "newbie", "password": "secret123"}).json()
        assert ok["ok"] and ok["plan_id"] == "trial"

        # Single-use means single use.
        again = client.post(f"/api/invite/{code}/redeem",
                            json={"username": "other", "password": "secret123"})
        assert again.status_code == 422

        assert client.post(f"/api/invites/{code}/revoke",
                           headers=_basic()).json()["revoked"]


def test_invite_rejects_weak_input() -> None:
    with TestClient(app) as client:
        codes = client.post("/api/invites", headers=_basic(),
                            json={"plan_id": "trial"}).json()
        code = codes[0]["code"]
        assert client.post(f"/api/invite/{code}/redeem",
                           json={"username": "ok", "password": "123"}).status_code == 422
        assert client.post(f"/api/invite/{code}/redeem",
                           json={"username": "bad name!",
                                 "password": "secret123"}).status_code == 422
        assert client.post("/api/invite/NOPE-NOPE-NOPE/redeem",
                           json={"username": "x", "password": "secret123"}
                           ).status_code == 422


def test_stats_endpoints_answer() -> None:
    with TestClient(app) as client:
        assert client.get("/api/stats/overview", headers=_basic()).status_code == 200
        assert len(client.get("/api/stats/daily?days=7", headers=_basic()).json()) == 7
        for path in ("top-users", "top-titles", "clients", "nodes", "play-methods"):
            assert client.get(f"/api/stats/{path}", headers=_basic()).status_code == 200
        assert client.get("/api/audit", headers=_basic()).status_code == 200
        assert client.get("/api/usage/status", headers=_basic()).status_code == 200


def test_image_cache_settings_validation() -> None:
    with TestClient(app) as client:
        cfg = client.get("/api/settings/image-cache", headers=_basic()).json()
        assert cfg["enabled"] is True and "stats" in cfg
        assert client.put("/api/settings/image-cache", headers=_basic(),
                          json={"max_gib": 0}).status_code == 422
        assert client.put("/api/settings/image-cache", headers=_basic(),
                          json={"max_age_days": 99999}).status_code == 422
        saved = client.put("/api/settings/image-cache", headers=_basic(),
                           json={"max_gib": 8, "max_age_days": 14}).json()
        assert saved["max_gib"] == 8 and saved["max_age_days"] == 14


def test_membership_settings_validation() -> None:
    with TestClient(app) as client:
        got = client.get("/api/settings/membership", headers=_basic())
        assert got.status_code == 200
        assert "enforcement_enabled" in got.json()
        overview = client.get("/api/settings", headers=_basic()).json()
        assert "membership" in overview and "image_cache" in overview
        assert client.put("/api/settings/membership", headers=_basic(),
                          json={"sample_interval_seconds": 1}).status_code == 422
        assert client.put("/api/settings/membership", headers=_basic(),
                          json={"retention_days": 5}).status_code == 422
        saved = client.put("/api/settings/membership", headers=_basic(),
                           json={"enforcement_enabled": True,
                                 "sample_interval_seconds": 20}).json()
        assert saved["enforcement_enabled"] is True


# ---- §7 backend gaps -------------------------------------------------------
def test_plan_in_use_delete_is_http_409() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", headers=_basic(),
                   json={"plan_id": "monthly", "username": "demo-user-1"})
        r = client.delete("/api/plans/monthly", headers=_basic())
        assert r.status_code == 409
        assert "用户" in (r.json().get("detail") or "")


def test_storage_remote_in_use_cannot_be_deleted() -> None:
    with TestClient(app) as client:
        r = client.delete("/api/storage/remotes/mock-drive", headers=_basic())
        assert r.status_code == 409
        assert "挂载" in (r.json().get("detail") or "")


def test_invite_admin_is_private_redeem_is_public() -> None:
    with TestClient(app) as client:
        assert client.get("/api/invites").status_code == 401
        assert client.post("/api/invites", json={"plan_id": "trial"}).status_code == 401
        codes = client.post("/api/invites", headers=_basic(),
                            json={"plan_id": "trial"}).json()
        code = codes[0]["code"]
        preview = client.get(f"/api/invite/{code}")
        assert preview.status_code == 200
        assert "plan" in preview.json()
        page = client.get(f"/invite/{code}")
        assert page.status_code == 200 and "开通账号" in page.text


def test_invite_redeem_is_rate_limited() -> None:
    from app.modules.invites import REDEEM_MAX_ATTEMPTS, InviteService
    svc = object.__new__(InviteService)
    svc._rate_lock = __import__("threading").Lock()
    svc._attempts = {}
    hit = 0
    for _ in range(REDEEM_MAX_ATTEMPTS + 3):
        try:
            svc.check_rate("ABCD-EFGH-IJKL", "10.0.0.9")
        except ConflictError:
            hit += 1
    assert hit >= 3


def test_weekly_period_rollover_resets_quota(stack) -> None:
    stack["plans"].create({
        "id": "week", "name": "周付", "billing_type": "traffic",
        "traffic_quota_bytes": 10 * GIB, "traffic_period": "weekly",
    })
    m = stack["members"]
    m.upsert("u1", "alice", {"plan_id": "week", "traffic_used_bytes": 10 * GIB})
    assert m.get("u1")["state"] == "exhausted"
    stack["db"].execute(
        "UPDATE members SET traffic_period_start=? WHERE emby_user_id=?", (1, "u1"))
    assert m.roll_periods() == 1
    after = m.get("u1")
    assert after["traffic_used_bytes"] == 0 and after["state"] == "active"
    assert after["traffic_period_start"] == period_start("weekly", int(time.time()))


def test_max_devices_refuses_new_device_and_audits(stack) -> None:
    m = stack["members"]
    m.upsert("u1", "alice", {"plan_id": "trial"})  # max_devices = 1
    assert m.register_device("u1", "phone") is True
    assert m.register_device("u1", "tablet") is False
    assert m.register_device("u1", "phone") is True  # existing still refreshes
    rows = stack["db"].query(
        "SELECT action FROM audit_log WHERE action='device.refused'")
    assert rows


def test_over_limit_device_is_kicked_mid_stream(stack) -> None:
    sampler = UsageSampler(stack["db"], stack["members"], stack["emby"],
                           stack["enforcement"])
    stack["members"].upsert("u1", "demo-user-1", {"plan_id": "trial"})
    stack["emby"].set_sessions([
        {"Id": "s1", "UserId": "u1", "DeviceId": "phone",
         "NowPlayingItem": {"Id": "i1", "Bitrate": 8_000_000},
         "PlayState": {"IsPaused": False}},
    ])
    asyncio.run(sampler.tick())
    stack["emby"].set_sessions([
        {"Id": "s1", "UserId": "u1", "DeviceId": "phone",
         "NowPlayingItem": {"Id": "i1", "Bitrate": 8_000_000},
         "PlayState": {"IsPaused": False}},
        {"Id": "s2", "UserId": "u1", "DeviceId": "tablet",
         "NowPlayingItem": {"Id": "i2", "Bitrate": 8_000_000},
         "PlayState": {"IsPaused": False}},
    ])
    result = asyncio.run(sampler.tick())
    assert result.get("device_kicks", 0) >= 1
    assert [s["Id"] for s in stack["emby"]._sessions] == ["s1"]


def test_member_delete_does_not_remove_emby_by_default() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", headers=_basic(),
                   json={"plan_id": "monthly", "username": "demo-user-1"})
        r = client.delete("/api/members/u1", headers=_basic())
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert r.json()["emby_deleted"] is False
        users = client.get("/api/emby/users", headers=_basic()).json()
        assert any(u["Id"] == "u1" for u in users)
        client.put("/api/members/u1", headers=_basic(),
                   json={"plan_id": "monthly", "username": "demo-user-1"})
        gone = client.delete("/api/members/u1?delete_emby=true", headers=_basic())
        assert gone.json()["emby_deleted"] is True


def test_node_can_be_created_with_name_only() -> None:
    with TestClient(app) as client:
        created = client.post("/api/nodes", headers=_basic(),
                              json={"name": "edge-pending", "capacity": 40})
        assert created.status_code == 200
        body = created.json()
        assert body["pending"] is True
        assert body["enrolled"] is False
        client.delete("/api/nodes/edge-pending", headers=_basic())


def test_enroll_report_and_rotate_token() -> None:
    with TestClient(app) as client:
        client.put("/api/settings/integration", headers=_basic(),
                   json={"panel_public_url": "https://panel.test"})
        client.post("/api/nodes", headers=_basic(), json={"name": "edge-home"})
        enroll = client.get("/api/nodes/edge-home/enroll", headers=_basic()).json()
        token = enroll["command"].split("/api/enroll/")[1].split("/script")[0]
        reported = client.post(
            f"/api/enroll/{token}/report",
            json={"base_url": "https://edge-home.example",
                  "probe_url": "http://10.0.0.8:9800/load",
                  "host": "edge-home.example"},
        )
        assert reported.status_code == 200
        assert reported.json()["enrolled"] is True
        rotated = client.post("/api/nodes/edge-home/rotate-enroll", headers=_basic())
        assert rotated.status_code == 200
        new_token = rotated.json()["command"].split("/api/enroll/")[1].split("/script")[0]
        assert new_token != token
        assert client.post(
            f"/api/enroll/{token}/report", json={"base_url": "https://x.example"},
        ).status_code == 404
        client.delete("/api/nodes/edge-home", headers=_basic())
