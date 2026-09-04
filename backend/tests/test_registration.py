"""Registration channels: who gets in, on whose word, and what it costs them.

The rule these tests exist to defend is that **resolve() decides and consume()
spends, in that order, with account creation in between**. A credential checked
and burned before the Emby account exists is a credential the member loses when
the username turns out to be taken -- they paid, and they have nothing.

So every channel is tested twice: the path where it admits someone and spends
exactly one thing, and the path where admission fails or creation fails and
nothing is spent at all.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.core.errors import ConfigError
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.registration import (
    ALPHABET,
    INVITE_LENGTH,
    REDEEM_LENGTH,
    Admission,
    RegistrationService,
    generate_code,
    mask_code,
    normalise,
)

ADMIN = ("admin", "change-me")


def _svc(tmp_path, cfg=None):
    db = Database(tmp_path / "reg.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    base = {"allow_admin_grant": True, "allow_invite": True,
            "allow_redeem": True, "register_days": 30,
            "default_group_id": ""}
    base.update(cfg or {})
    return RegistrationService(db, groups, lambda: base), members, groups, base


def _member(members, groups, name="holder"):
    return members.upsert(f"emby-{name}", name,
                          {"group_id": groups.default_group_id()},
                          actor="test")


# -- code shape --------------------------------------------------------------

def test_codes_avoid_the_characters_people_misread() -> None:
    """O/0 and I/1 are read off a phone and typed back by hand."""
    for banned in ("O", "0", "I", "1"):
        assert banned not in ALPHABET
    for _ in range(50):
        assert set(generate_code(INVITE_LENGTH)) <= set(ALPHABET)


def test_codes_are_compared_forgivingly_but_stored_exactly() -> None:
    assert normalise(" abcd-efgh ") == "ABCDEFGH"
    assert normalise(None) == ""


def test_masking_shows_enough_to_find_a_card_not_to_use_it() -> None:
    masked = mask_code("ABCDEFGHJKLM")
    assert masked.startswith("ABCD") and masked.endswith("JKLM")
    assert "EFGH" not in masked


# -- channel: admin grant ----------------------------------------------------

def test_a_pre_authorised_chat_needs_no_credential(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    reg.grant_admin("555", granted_by="operator")

    verdict = reg.resolve("555", None)
    assert verdict.allowed is True
    assert verdict.via == "admin"


def test_an_unknown_chat_with_no_credential_is_refused(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    verdict = reg.resolve("999", None)
    assert verdict.allowed is False
    assert verdict.via == ""
    assert "邀请码" in verdict.reason


def test_a_grant_admits_once_and_then_is_spent(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    reg.grant_admin("555")

    first = reg.resolve("555", None)
    assert reg.consume(first, "emby-new") is True
    assert reg.get_grant("555")["used_at"]

    # The same chat coming back is a stranger again.
    assert reg.resolve("555", None).allowed is False
    assert reg.consume(first, "emby-other") is False


def test_a_revoked_grant_stops_admitting(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    reg.grant_admin("555")
    reg.revoke_grant("555")
    assert reg.resolve("555", None).allowed is False


def test_granting_twice_is_one_slot_not_two(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    reg.grant_admin("555")
    reg.grant_admin("555")
    assert len(reg.list_grants()) == 1


def test_a_grant_needs_a_numeric_telegram_id(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    for bad in ("", "not-a-number", "@someone"):
        with pytest.raises(ConfigError):
            reg.grant_admin(bad)


# -- channel: invite ---------------------------------------------------------

def test_an_invite_admits_and_records_who_vouched(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=1)

    verdict = reg.resolve("777", issued["code"])
    assert verdict.allowed is True
    assert verdict.via == "invite"
    assert verdict.inviter_id == owner["emby_user_id"]


def test_an_invite_is_only_spent_by_consume(tmp_path) -> None:
    """Resolving twice must not cost the code anything."""
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=1)

    reg.resolve("777", issued["code"])
    reg.resolve("778", issued["code"])
    assert reg.get_invite(issued["code"])["uses_left"] == 1

    verdict = reg.resolve("777", issued["code"])
    assert reg.consume(verdict, "emby-new") is True
    assert reg.get_invite(issued["code"])["uses_left"] == 0


def test_an_exhausted_invite_is_refused_and_says_why(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=1)
    reg.consume(reg.resolve("777", issued["code"]), "emby-a")

    verdict = reg.resolve("778", issued["code"])
    assert verdict.allowed is False
    assert "用完" in verdict.reason
    # And a stale admission cannot be replayed to spend it below zero.
    stale = Admission(allowed=True, via="invite", credential=issued["code"])
    assert reg.consume(stale, "emby-b") is False
    assert reg.get_invite(issued["code"])["uses_left"] == 0


def test_a_multi_use_invite_admits_that_many_times(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=3)
    for n in range(3):
        assert reg.consume(reg.resolve(str(n), issued["code"]), f"emby-{n}")
    assert reg.resolve("9", issued["code"]).allowed is False


def test_an_expired_invite_is_refused(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=1, ttl_days=1)
    reg._db.execute("UPDATE invite_codes SET expires_at=? WHERE code=?",
                    (int(time.time()) - 60, issued["code"]))

    verdict = reg.resolve("777", issued["code"])
    assert verdict.allowed is False
    assert "过期" in verdict.reason


def test_an_unexpired_invite_still_works(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=1, ttl_days=30)
    assert reg.resolve("777", issued["code"]).allowed is True


def test_a_revoked_invite_is_refused(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    issued = reg.issue_invite(owner["emby_user_id"], uses=5)
    reg.revoke_invite(issued["code"])

    verdict = reg.resolve("777", issued["code"])
    assert verdict.allowed is False
    assert "作废" in verdict.reason


def test_invite_quota_is_debited_when_a_member_mints_one(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    uid = owner["emby_user_id"]
    reg.adjust_quota(uid, 2)

    reg.spend_quota_for_invite(uid)
    assert reg.invite_quota(uid) == 1
    reg.spend_quota_for_invite(uid)
    assert reg.invite_quota(uid) == 0


def test_a_member_with_no_quota_cannot_mint(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    with pytest.raises(ConfigError):
        reg.spend_quota_for_invite(owner["emby_user_id"])
    assert reg.list_invites(owner["emby_user_id"]) == []


def test_quota_never_goes_negative(tmp_path) -> None:
    reg, members, groups, _cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    assert reg.adjust_quota(owner["emby_user_id"], -5) == 0


# -- channel: redeem ---------------------------------------------------------

def test_a_card_carries_its_own_group_and_duration(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    group_id = groups.default_group_id()
    cards = reg.generate_redeem(group_id, days=90, count=1, batch="spring")

    verdict = reg.resolve("777", cards[0]["code"])
    assert verdict.allowed is True
    assert verdict.via == "redeem"
    assert verdict.days == 90
    assert verdict.group_id == group_id


def test_a_card_cannot_be_redeemed_twice(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    cards = reg.generate_redeem(groups.default_group_id(), days=30, count=1)
    card_value = cards[0]["code"]

    assert reg.consume(reg.resolve("777", card_value), "emby-a") is True
    verdict = reg.resolve("778", card_value)
    assert verdict.allowed is False
    assert "使用" in verdict.reason

    stale = Admission(allowed=True, via="redeem", credential=card_value)
    assert reg.consume(stale, "emby-b") is False
    assert reg.get_redeem(card_value)["used_by"] == "emby-a"


def test_a_used_card_records_who_spent_it_and_when(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    cards = reg.generate_redeem(groups.default_group_id(), days=30, count=1)
    reg.consume(reg.resolve("777", cards[0]["code"]), "emby-buyer")

    row = reg.get_redeem(cards[0]["code"])
    assert row["status"] == "used"
    assert row["used_by"] == "emby-buyer"
    assert row["used_at"]


def test_a_revoked_card_is_refused_but_a_spent_one_cannot_be_revoked(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    cards = reg.generate_redeem(groups.default_group_id(), days=30, count=2)
    reg.revoke_redeem(cards[0]["code"])
    assert reg.resolve("777", cards[0]["code"]).allowed is False

    reg.consume(reg.resolve("778", cards[1]["code"]), "emby-a")
    with pytest.raises(ConfigError):
        reg.revoke_redeem(cards[1]["code"])
    assert reg.get_redeem(cards[1]["code"])["status"] == "used"


def test_batches_are_generated_whole_and_counted(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    issued = reg.generate_redeem(groups.default_group_id(), days=30,
                                 count=25, batch="autumn")
    assert len(issued) == 25
    assert len({c["code"] for c in issued}) == 25
    assert all(len(c["code"]) == REDEEM_LENGTH for c in issued)
    assert reg.redeem_stats()["unused"] == 25
    assert "autumn" in reg.redeem_batches()


def test_generation_refuses_nonsense(tmp_path) -> None:
    reg, _members, groups, _cfg = _svc(tmp_path)
    good = groups.default_group_id()
    for kwargs in ({"group_id": "", "days": 30, "count": 1},
                   {"group_id": "no-such-group", "days": 30, "count": 1},
                   {"group_id": good, "days": 30, "count": 0},
                   {"group_id": good, "days": 30, "count": 9999},
                   {"group_id": good, "days": -1, "count": 1}):
        with pytest.raises(ConfigError):
            reg.generate_redeem(**kwargs)
    assert reg.redeem_stats()["total"] == 0


# -- channel precedence and switches ----------------------------------------

def test_a_grant_wins_over_a_credential(tmp_path) -> None:
    """Someone the operator named should not be charged a card as well."""
    reg, _members, groups, _cfg = _svc(tmp_path)
    reg.grant_admin("555")
    cards = reg.generate_redeem(groups.default_group_id(), days=30, count=1)

    verdict = reg.resolve("555", cards[0]["code"])
    assert verdict.via == "admin"
    reg.consume(verdict, "emby-a")
    assert reg.get_redeem(cards[0]["code"])["status"] == "unused"


def test_each_channel_can_be_shut_independently(tmp_path) -> None:
    reg, members, groups, cfg = _svc(tmp_path)
    owner = _member(members, groups, "owner")
    invite_value = reg.issue_invite(owner["emby_user_id"], uses=1)["code"]
    card_value = reg.generate_redeem(
        groups.default_group_id(), days=30, count=1)[0]["code"]
    reg.grant_admin("555")

    cfg["allow_invite"] = False
    assert reg.resolve("777", invite_value).allowed is False
    assert reg.resolve("777", card_value).allowed is True

    cfg["allow_invite"] = True
    cfg["allow_redeem"] = False
    assert reg.resolve("777", invite_value).allowed is True
    assert reg.resolve("777", card_value).allowed is False

    cfg["allow_admin_grant"] = False
    assert reg.resolve("555", None).allowed is False


def test_an_unknown_credential_is_refused_without_hinting(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    verdict = reg.resolve("777", "NOTACODE1234")
    assert verdict.allowed is False
    assert "无效" in verdict.reason


def test_consume_refuses_a_verdict_that_said_no(tmp_path) -> None:
    reg, _members, _groups, _cfg = _svc(tmp_path)
    assert reg.consume(reg.resolve("777", "NOPE"), "emby-a") is False
    assert reg.consume(None, "emby-a") is False


# -- API surface -------------------------------------------------------------

def test_registration_endpoints_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/registration/grants").status_code == 401
        assert client.post("/api/registration/grants",
                           json={"tg_user_id": "1"}).status_code == 401
        assert client.get("/api/members/x/invites").status_code == 401


def test_grants_round_trip_through_the_api() -> None:
    with TestClient(app) as client:
        created = client.post("/api/registration/grants", auth=ADMIN,
                              json={"tg_user_id": "424242"})
        assert created.status_code == 200
        listed = client.get("/api/registration/grants", auth=ADMIN).json()
        assert any(g["tg_user_id"] == "424242" for g in listed)

        gone = client.delete("/api/registration/grants/424242", auth=ADMIN)
        assert gone.status_code == 200
        listed = client.get("/api/registration/grants", auth=ADMIN).json()
        assert not any(g["tg_user_id"] == "424242" for g in listed)


def test_a_non_numeric_grant_is_rejected_by_the_api() -> None:
    with TestClient(app) as client:
        r = client.post("/api/registration/grants", auth=ADMIN,
                        json={"tg_user_id": "@someone"})
        assert r.status_code >= 400


def test_invite_quota_endpoint_moves_the_number() -> None:
    with TestClient(app) as client:
        groups = client.get("/api/groups", auth=ADMIN).json()
        client.put("/api/members/quota-user", auth=ADMIN,
                   json={"username": "quotauser", "group_id": groups[0]["id"]})

        after = client.post("/api/members/quota-user/invite-quota", auth=ADMIN,
                            json={"delta": 3}).json()
        assert after["quota"] == 3
        detail = client.get("/api/members/quota-user/invites", auth=ADMIN).json()
        assert detail["quota"] == 3
        assert detail["invites"] == []

        back = client.post("/api/members/quota-user/invite-quota", auth=ADMIN,
                           json={"delta": -10}).json()
        assert back["quota"] == 0


def test_invite_quota_on_an_unknown_member_is_404() -> None:
    with TestClient(app) as client:
        r = client.post("/api/members/no-such-user/invite-quota", auth=ADMIN,
                        json={"delta": 1})
        assert r.status_code == 404
        assert client.get("/api/members/no-such-user/invites",
                          auth=ADMIN).status_code == 404
