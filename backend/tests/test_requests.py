"""Media requests: the quota, the deduplication, and the race.

Three things here are load-bearing, and each is tested from both sides.

- **The quota counts refusals.** A rejected request still spent the slot,
  because otherwise a member can ask for unavailable titles forever. The month
  boundary is checked on read, so a rolled-over month is correct immediately.
- **One row per title.** Two members asking for the same film is one request.
  Two uploaders downloading the same 40GB is the failure being prevented.
- **One claimer.** Two uploaders tapping 接单 at the same instant produce one
  winner and one refusal naming the winner -- never two owners.

The last group covers who is *allowed* to close a request: an uploader who did
not take the job cannot mark it done, because the requester would then be told
their title was handled by somebody who never touched it.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.requests import (
    RequestError,
    RequestService,
    current_period,
    display_title,
)

ADMIN = ("admin", "change-me")


class _FakeTmdb:
    """Stands in for TMDB. ``answers`` maps (type, id) -> metadata."""

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = answers or {}
        self.calls: list = []

    async def resolve(self, media_type, tmdb_id):
        self.calls.append((media_type, tmdb_id))
        found = self.answers.get((media_type, int(tmdb_id)))
        if found is not None:
            return media_type, dict(found)
        other = "tv" if media_type == "movie" else "movie"
        found = self.answers.get((other, int(tmdb_id)))
        if found is not None:
            return other, dict(found)
        return media_type, None


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "req.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    tmdb = _FakeTmdb({("movie", 550): {"title": "搏击俱乐部", "year": 1999,
                                       "poster_path": "/p.jpg",
                                       "overview": "简介"}})
    service = RequestService(db, members, groups, tmdb)
    return db, groups, members, service, tmdb


@pytest.fixture()
def member(stack) -> str:
    _, _, members, _, _ = stack
    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    members.bind_telegram("u1", "999", "alice_tg", actor="test")
    return "u1"


def _uploader(members, user_id: str = "up1", name: str = "bob",
              tg: str = "555") -> str:
    members.upsert(user_id, name, {"group_id": "standard"}, actor="test")
    members.set_roles(user_id, ["uploader"], actor="test")
    if tg:
        members.bind_telegram(user_id, tg, name + "_tg", actor="test")
    return user_id


def _create(service, user_id="u1", media_type="movie", tmdb_id=550, note=""):
    return asyncio.run(service.create(user_id, media_type, tmdb_id, note))


# -- the whitelist group -----------------------------------------------------

def test_the_whitelist_group_is_seeded_and_never_expires(stack) -> None:
    _, groups, _, _, _ = stack
    wl = groups.get("whitelist")
    assert wl is not None
    assert wl["duration_days"] == 0
    assert wl["billing_mode"] == "none"
    assert wl["max_streams"] == 10 and wl["max_devices"] == 10
    assert wl["traffic_quota_bytes"] == 0
    assert wl["request_quota"] == 0


def test_seeding_the_whitelist_twice_changes_nothing(stack) -> None:
    """Idempotent: boot happens more than once."""
    _, groups, _, _, _ = stack
    before = groups.get("whitelist")
    assert groups.ensure_whitelist() == 0
    assert groups.seed_defaults() == 0
    assert groups.get("whitelist") == before
    assert len([g for g in groups.list() if g["id"] == "whitelist"]) == 1


def test_a_renamed_whitelist_group_is_left_alone(stack) -> None:
    """The operator's edits outrank the seed."""
    _, groups, _, _, _ = stack
    groups.update("whitelist", {"name": "内部人员"})
    groups.ensure_whitelist()
    assert groups.get("whitelist")["name"] == "内部人员"


def test_a_deleted_whitelist_group_comes_back_so_prouser_keeps_working(stack) -> None:
    _, groups, _, _, _ = stack
    groups.delete("whitelist")
    assert groups.get("whitelist") is None
    assert groups.ensure_whitelist() == 1
    assert groups.get("whitelist") is not None


# -- quota -------------------------------------------------------------------

def test_the_default_group_allows_three_requests_a_month(stack, member) -> None:
    _, _, _, service, _ = stack
    assert service.remaining(member) == 3


def test_each_request_spends_one_slot(stack, member) -> None:
    _, _, _, service, _ = stack
    _create(service, tmdb_id=550)
    assert service.remaining(member) == 2
    _create(service, tmdb_id=551)
    assert service.remaining(member) == 1


def test_the_fourth_request_in_a_month_is_refused(stack, member) -> None:
    _, _, _, service, _ = stack
    for tmdb_id in (1, 2, 3):
        _create(service, tmdb_id=tmdb_id)
    assert service.remaining(member) == 0
    with pytest.raises(RequestError) as exc:
        _create(service, tmdb_id=4)
    assert "已用完" in str(exc.value)
    # And nothing was written for the refused attempt.
    assert len(service.list()) == 3


def test_a_rejected_request_does_not_refund_the_slot(stack, member) -> None:
    """Deriving the count from open rows would let a member ask for
    unavailable titles forever."""
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)
    service.resolve(req["id"], up, done=False, note="没有片源")

    assert service.remaining(member) == 2


def test_the_counter_resets_when_the_month_rolls_over(stack, member) -> None:
    db, _, _, service, _ = stack
    for tmdb_id in (1, 2, 3):
        _create(service, tmdb_id=tmdb_id)
    assert service.remaining(member) == 0

    # Only the stored period is moved back; the count stays. A member whose
    # month rolled over must be correct the first time they ask, not whenever
    # some scheduled job next runs.
    db.execute("UPDATE members SET request_period='1999-01' WHERE emby_user_id=?",
               (member,))

    assert service.used(member) == 0
    assert service.remaining(member) == 3


def test_a_new_months_request_starts_the_count_at_one_not_four(stack, member) -> None:
    db, _, _, service, _ = stack
    for tmdb_id in (1, 2, 3):
        _create(service, tmdb_id=tmdb_id)
    db.execute("UPDATE members SET request_period='1999-01' WHERE emby_user_id=?",
               (member,))

    _create(service, tmdb_id=4)

    row = db.one("SELECT * FROM members WHERE emby_user_id=?", (member,))
    assert row["request_used"] == 1
    assert row["request_period"] == current_period()
    assert service.remaining(member) == 2


def test_a_group_with_zero_quota_is_unlimited(stack) -> None:
    """None, not a large number: the bot prints 不限 for it, and a sentinel
    integer would eventually be shown to somebody as a count."""
    _, _, members, service, _ = stack
    members.upsert("u9", "vip", {"group_id": "whitelist"}, actor="test")
    assert service.remaining("u9") is None
    for tmdb_id in range(1, 6):
        _create(service, user_id="u9", tmdb_id=tmdb_id)
    assert service.remaining("u9") is None
    assert len(service.list()) == 5


def test_an_unknown_member_has_no_allowance_and_cannot_request(stack) -> None:
    _, _, _, service, _ = stack
    assert service.remaining("ghost") == 0
    with pytest.raises(RequestError):
        _create(service, user_id="ghost")


def test_the_group_quota_is_editable_and_takes_effect_at_once(stack, member) -> None:
    _, groups, _, service, _ = stack
    groups.update("standard", {"request_quota": 1})
    assert service.remaining(member) == 1
    _create(service, tmdb_id=550)
    with pytest.raises(RequestError):
        _create(service, tmdb_id=551)


def test_an_older_form_that_omits_the_quota_does_not_uncap_requests(stack) -> None:
    """A UI predating the field posts no request_quota. Treating that as 0
    would silently give everyone unlimited requests."""
    _, groups, _, _, _ = stack
    groups.update("standard", {"name": "普通用户"})
    assert groups.get("standard")["request_quota"] == 3


# -- deduplication -----------------------------------------------------------

def test_the_same_title_cannot_be_requested_twice_while_open(stack, member) -> None:
    _, _, members, service, _ = stack
    members.upsert("u2", "carol", {"group_id": "standard"}, actor="test")
    _create(service, user_id="u1", tmdb_id=550)

    with pytest.raises(RequestError) as exc:
        _create(service, user_id="u2", tmdb_id=550)
    assert "已经有人求过" in str(exc.value)
    assert len(service.list()) == 1


def test_a_claimed_title_still_blocks_a_second_request(stack, member) -> None:
    """'Being worked on' is exactly when a duplicate is most expensive."""
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)

    with pytest.raises(RequestError):
        _create(service, tmdb_id=550)


def test_the_same_title_can_be_requested_again_once_it_is_closed(stack, member) -> None:
    """A film that was rejected for lack of a source may appear later."""
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)
    service.resolve(req["id"], up, done=False, note="暂无片源")

    again = _create(service, tmdb_id=550)
    assert again["status"] == "open"
    assert len(service.list()) == 2


def test_a_film_and_a_series_sharing_an_id_are_different_requests(stack, member) -> None:
    _, _, _, service, tmdb = stack
    tmdb.answers[("tv", 550)] = {"title": "同号剧集", "year": 2020}
    _create(service, media_type="movie", tmdb_id=550)
    _create(service, media_type="tv", tmdb_id=550)
    assert len(service.list()) == 2


# -- metadata is optional ----------------------------------------------------

def test_a_lookup_that_answers_fills_in_title_year_and_poster(stack, member) -> None:
    _, _, _, service, _ = stack
    req = _create(service, tmdb_id=550)
    assert req["title"] == "搏击俱乐部"
    assert req["year"] == 1999
    assert req["poster_path"] == "/p.jpg"
    assert req["display_title"] == "搏击俱乐部 (1999)"


def test_with_no_key_the_request_is_still_created_under_a_placeholder(stack, member) -> None:
    """The owner has no TMDB key. Requests must still work."""
    db, groups, members, _, _ = stack
    service = RequestService(db, members, groups, tmdb=None)

    req = service and _create(service, tmdb_id=12345)

    assert req["status"] == "open"
    assert req["title"] == ""
    assert req["display_title"] == "#12345"
    assert req["tmdb_id"] == 12345


def test_an_unknown_id_still_becomes_a_request(stack, member) -> None:
    _, _, _, service, _ = stack
    req = _create(service, tmdb_id=99999)
    assert req["status"] == "open" and req["display_title"] == "#99999"


def test_a_bare_id_that_turns_out_to_be_a_series_is_stored_as_one(stack, member) -> None:
    _, _, _, service, tmdb = stack
    tmdb.answers[("tv", 1396)] = {"title": "绝命毒师", "year": 2008}
    req = _create(service, media_type="movie", tmdb_id=1396)
    assert req["media_type"] == "tv"
    assert req["title"] == "绝命毒师"


def test_the_requester_and_their_chat_are_recorded_on_the_row(stack, member) -> None:
    """Whoever gets told the outcome is decided at creation time."""
    _, _, _, service, _ = stack
    req = _create(service, tmdb_id=550, note="求1080p")
    assert req["emby_user_id"] == "u1"
    assert req["username"] == "alice"
    assert req["tg_user_id"] == "999"
    assert req["note"] == "求1080p"


@pytest.mark.parametrize("bad", [0, -1, "abc", None])
def test_a_nonsense_id_is_refused(stack, member, bad) -> None:
    _, _, _, service, _ = stack
    with pytest.raises(RequestError):
        _create(service, tmdb_id=bad)


# -- claiming ----------------------------------------------------------------

def test_claiming_marks_the_owner_and_the_time(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)

    result = service.claim(req["id"], up)

    assert result["ok"] is True
    row = result["request"]
    assert row["status"] == "claimed"
    assert row["claimed_by"] == up
    assert row["claimed_by_name"] == "bob"
    assert row["claimed_at"] >= req["created_at"]


def test_only_the_first_of_two_simultaneous_claims_wins(stack, member) -> None:
    """The whole point of the conditional UPDATE: two uploaders tapping at the
    same instant must not both believe they own the job."""
    _, _, members, service, _ = stack
    first = _uploader(members, "up1", "bob", "555")
    second = _uploader(members, "up2", "dave", "556")
    req = _create(service, tmdb_id=550)

    won = service.claim(req["id"], first)
    lost = service.claim(req["id"], second)

    assert won["ok"] is True
    assert lost["ok"] is False
    assert lost["claimed_by"] == first
    assert lost["claimed_by_name"] == "bob"
    assert "已被接单" in lost["reason"]
    assert service.get(req["id"])["claimed_by"] == first


def test_claiming_an_already_finished_request_is_refused_with_a_reason(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    other = _uploader(members, "up2", "dave", "556")
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)
    service.resolve(req["id"], up, done=True)

    lost = service.claim(req["id"], other)
    assert lost["ok"] is False
    assert "处理完毕" in lost["reason"]


def test_claiming_a_request_that_does_not_exist_raises(stack) -> None:
    _, _, _, service, _ = stack
    with pytest.raises(RequestError):
        service.claim(4242, "up1")


# -- resolving ---------------------------------------------------------------

def test_the_claimer_can_close_it_as_done(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)

    out = service.resolve(req["id"], up, done=True, note="已入库")

    assert out["ok"] is True
    row = out["request"]
    assert row["status"] == "done"
    assert row["result_note"] == "已入库"
    assert row["resolved_at"] >= row["claimed_at"]


def test_the_claimer_can_close_it_as_rejected_with_a_reason(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)

    out = service.resolve(req["id"], up, done=False, note="全网无片源")

    assert out["request"]["status"] == "rejected"
    assert out["request"]["result_note"] == "全网无片源"


def test_an_uploader_who_did_not_take_the_job_cannot_close_it(stack, member) -> None:
    """Otherwise the requester is told their title was handled by somebody
    who never touched it."""
    _, _, members, service, _ = stack
    holder = _uploader(members, "up1", "bob", "555")
    stranger = _uploader(members, "up2", "dave", "556")
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], holder)

    with pytest.raises(RequestError) as exc:
        service.resolve(req["id"], stranger, done=True)
    assert "别人接的单" in str(exc.value)
    assert service.get(req["id"])["status"] == "claimed"


def test_an_admin_can_close_a_request_somebody_else_holds(stack, member) -> None:
    _, _, members, service, _ = stack
    holder = _uploader(members, "up1", "bob", "555")
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], holder)

    out = service.resolve(req["id"], "admin-user", done=True, is_admin=True)
    assert out["request"]["status"] == "done"


def test_an_unclaimed_request_cannot_be_resolved(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)

    with pytest.raises(RequestError) as exc:
        service.resolve(req["id"], up, done=True)
    assert "待接单" in str(exc.value)


def test_a_request_cannot_be_resolved_twice(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    req = _create(service, tmdb_id=550)
    service.claim(req["id"], up)
    service.resolve(req["id"], up, done=True)

    with pytest.raises(RequestError):
        service.resolve(req["id"], up, done=False, note="反悔了")


# -- uploader fan-out bookkeeping -------------------------------------------

def test_every_notified_uploader_is_recorded_with_their_message(stack, member) -> None:
    """Without the message ids a claim cannot take the button away from the
    uploaders who did not win."""
    _, _, members, service, _ = stack
    _uploader(members, "up1", "bob", "555")
    _uploader(members, "up2", "dave", "556")
    req = _create(service, tmdb_id=550)

    service.record_notice(req["id"], "555", 1001)
    service.record_notice(req["id"], "556", 1002)

    notices = service.notices(req["id"])
    assert {(n["tg_user_id"], n["message_id"]) for n in notices} == {
        ("555", 1001), ("556", 1002)}


def test_recording_the_same_chat_twice_updates_rather_than_duplicates(stack, member) -> None:
    _, _, _, service, _ = stack
    req = _create(service, tmdb_id=550)
    service.record_notice(req["id"], "555", 1001)
    service.record_notice(req["id"], "555", 1009)

    notices = service.notices(req["id"])
    assert len(notices) == 1 and notices[0]["message_id"] == 1009


def test_only_uploaders_with_a_linked_chat_are_reachable(stack, member) -> None:
    _, _, members, service, _ = stack
    _uploader(members, "up1", "bob", "555")
    _uploader(members, "up2", "dave", tg="")  # no chat: cannot be messaged
    members.upsert("plain", "eve", {"group_id": "standard"}, actor="test")
    members.bind_telegram("plain", "777", "eve_tg", actor="test")

    reachable = {u["emby_user_id"] for u in service.uploaders()}
    assert reachable == {"up1"}


# -- listing and stats -------------------------------------------------------

def test_requests_are_listed_newest_first_and_filterable(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    first = _create(service, tmdb_id=1)
    second = _create(service, tmdb_id=2)
    service.claim(first["id"], up)

    assert [r["id"] for r in service.list()] == [second["id"], first["id"]]
    assert [r["id"] for r in service.list(status="open")] == [second["id"]]
    assert [r["id"] for r in service.list(status="claimed")] == [first["id"]]
    assert len(service.list(status="active")) == 2


def test_a_member_can_see_their_own_recent_requests(stack, member) -> None:
    _, _, members, service, _ = stack
    members.upsert("u2", "carol", {"group_id": "standard"}, actor="test")
    _create(service, user_id="u1", tmdb_id=1)
    _create(service, user_id="u2", tmdb_id=2)

    mine = service.for_user("u1")
    assert [r["tmdb_id"] for r in mine] == [1]


def test_stats_count_each_status_and_the_month(stack, member) -> None:
    _, _, members, service, _ = stack
    up = _uploader(members)
    a = _create(service, tmdb_id=1)
    b = _create(service, tmdb_id=2)
    _create(service, tmdb_id=3)
    service.claim(a["id"], up)
    service.resolve(a["id"], up, done=True)
    service.claim(b["id"], up)

    st = service.stats()
    assert st["open"] == 1 and st["claimed"] == 1 and st["done"] == 1
    assert st["rejected"] == 0
    assert st["month_total"] == 3
    assert st["period"] == current_period()


def test_display_title_falls_back_to_the_id_when_there_is_no_name() -> None:
    assert display_title({"tmdb_id": 42, "title": ""}) == "#42"
    assert display_title({"tmdb_id": 42, "title": "片名"}) == "片名"
    assert display_title({"tmdb_id": 42, "title": "片名", "year": 2001}) == "片名 (2001)"


# -- API ---------------------------------------------------------------------

def test_the_request_endpoints_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/requests").status_code == 401
        assert client.get("/api/requests/stats").status_code == 401
        assert client.post("/api/requests/1/claim").status_code == 401
        assert client.post("/api/requests/1/resolve", json={}).status_code == 401


def test_the_api_lists_requests_and_reports_stats() -> None:
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"},
                                 actor="test")
        asyncio.run(app.state.requests.create("u1", "movie", 550))

        rows = client.get("/api/requests", auth=ADMIN).json()
        assert len(rows) == 1 and rows[0]["tmdb_id"] == 550

        st = client.get("/api/requests/stats", auth=ADMIN).json()
        assert st["open"] == 1 and st["month_total"] == 1


def test_the_api_claims_and_resolves_a_request() -> None:
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"},
                                 actor="test")
        app.state.members.upsert("up1", "bob", {"group_id": "standard"},
                                 actor="test")
        app.state.members.set_roles("up1", ["uploader"], actor="test")
        req = asyncio.run(app.state.requests.create("u1", "movie", 550))

        claimed = client.post(f"/api/requests/{req['id']}/claim", auth=ADMIN,
                              json={"user_id": "up1"}).json()
        assert claimed["ok"] is True

        done = client.post(f"/api/requests/{req['id']}/resolve", auth=ADMIN,
                           json={"done": True, "note": "已入库"}).json()
        assert done["request"]["status"] == "done"
        assert done["request"]["result_note"] == "已入库"


def test_the_api_reports_a_lost_claim_rather_than_failing(stack) -> None:
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"},
                                 actor="test")
        for uid, name in (("up1", "bob"), ("up2", "dave")):
            app.state.members.upsert(uid, name, {"group_id": "standard"},
                                     actor="test")
            app.state.members.set_roles(uid, ["uploader"], actor="test")
        req = asyncio.run(app.state.requests.create("u1", "movie", 550))

        client.post(f"/api/requests/{req['id']}/claim", auth=ADMIN,
                    json={"user_id": "up1"})
        second = client.post(f"/api/requests/{req['id']}/claim", auth=ADMIN,
                             json={"user_id": "up2"})

        assert second.status_code == 200
        assert second.json()["ok"] is False


def test_the_api_rejects_an_unknown_request(stack) -> None:
    with TestClient(app) as client:
        assert client.post("/api/requests/9999/claim", auth=ADMIN,
                           json={}).status_code == 404
        assert client.post("/api/requests/9999/resolve", auth=ADMIN,
                           json={"done": True}).status_code == 404


def test_claiming_and_resolving_are_written_to_the_audit_trail() -> None:
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"},
                                 actor="test")
        app.state.members.upsert("up1", "bob", {"group_id": "standard"},
                                 actor="test")
        app.state.members.set_roles("up1", ["uploader"], actor="test")
        req = asyncio.run(app.state.requests.create("u1", "movie", 550))
        client.post(f"/api/requests/{req['id']}/claim", auth=ADMIN,
                    json={"user_id": "up1"})
        client.post(f"/api/requests/{req['id']}/resolve", auth=ADMIN,
                    json={"done": True})

        body = str(client.get("/api/audit?limit=50", auth=ADMIN).json())
        assert "request.claim" in body and "request.done" in body


def test_the_tmdb_key_never_comes_back_from_the_settings_api() -> None:
    creds = "tmdb" + "-" + "placeholder-not-a-real-key"
    with TestClient(app) as client:
        saved = client.put("/api/settings/integration", auth=ADMIN, json={
            "tmdb_api_key": creds, "tmdb_language": "en-US"}).json()
        assert creds not in str(saved)
        assert saved["tmdb_api_key_set"] is True
        assert saved["tmdb_language"] == "en-US"

        fetched = client.get("/api/settings/integration", auth=ADMIN).json()
        assert creds not in str(fetched)
        assert "tmdb_api_key" not in fetched

        everything = client.get("/api/settings", auth=ADMIN).json()
        assert creds not in str(everything)


def test_editing_the_language_does_not_wipe_the_stored_key() -> None:
    creds = "tmdb" + "-" + "placeholder-not-a-real-key"
    with TestClient(app) as client:
        client.put("/api/settings/integration", auth=ADMIN,
                   json={"tmdb_api_key": creds})
        after = client.put("/api/settings/integration", auth=ADMIN,
                           json={"tmdb_language": "ja-JP"}).json()
        assert after["tmdb_api_key_set"] is True
        assert after["tmdb_language"] == "ja-JP"


def test_the_group_api_round_trips_the_request_quota() -> None:
    with TestClient(app) as client:
        created = client.post("/api/groups", auth=ADMIN, json={
            "id": "reqtest", "name": "求片测试组", "billing_mode": "none",
            "request_quota": 7}).json()
        assert created["request_quota"] == 7

        updated = client.put("/api/groups/reqtest", auth=ADMIN,
                             json={"request_quota": 0}).json()
        assert updated["request_quota"] == 0


def test_the_whitelist_group_is_present_on_a_booted_panel() -> None:
    with TestClient(app) as client:
        ids = {g["id"] for g in client.get("/api/groups", auth=ADMIN).json()}
        assert "whitelist" in ids
