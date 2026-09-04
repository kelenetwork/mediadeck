"""The bot talks to two audiences and holds a bearer credential.

What these tests pin down:

- A bot token is a bearer credential. Anyone holding it can read every message
  the bot receives and post as it, so it must never travel back to a browser
  and must never appear in the audit trail.
- The menu is chosen from binding state on every render. A guest offered
  member-only buttons would hit dead ends; a member offered "bind" would be
  told to do something they already did.
- One chat speaks for one member. Rebinding must detach the previous holder
  loudly, or they keep believing they still receive notifications.
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.main import app
from app.modules.settings import mask_secret
from app.modules.telegram import BindCodes, TelegramBot

# Shaped like a real credential so format validation is exercised; not a real one.
FAKE_CRED = "123456789:THIS-IS-NOT-REAL-0000000000000000000"

ADMIN = ("admin", "change-me")


# -- the credential must not leak -------------------------------------------

def test_saved_credential_never_comes_back_to_the_browser() -> None:
    with TestClient(app) as client:
        saved = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"bot_token": FAKE_CRED, "enabled": True}).json()
        assert saved["bot_token_set"] is True
        # The full value must not appear anywhere in the response body.
        assert FAKE_CRED not in str(saved)
        assert saved["bot_token_masked"] == mask_secret(FAKE_CRED)

        fetched = client.get("/api/settings/telegram", auth=ADMIN).json()
        assert FAKE_CRED not in str(fetched)
        assert "bot_token" not in fetched


def test_masking_shows_enough_to_recognise_never_enough_to_use() -> None:
    masked = mask_secret(FAKE_CRED)
    assert masked.startswith(FAKE_CRED[:4])
    assert masked.endswith(FAKE_CRED[-4:])
    assert FAKE_CRED[8:-8] not in masked


def test_audit_records_that_it_changed_not_what_it_changed_to() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        entries = client.get("/api/audit?limit=20", auth=ADMIN).json()
        body = str(entries)
        assert "settings.telegram" in body
        assert FAKE_CRED not in body


def test_unretyped_credential_keeps_the_stored_one() -> None:
    """An edit form that only flips a checkbox must not wipe the credential."""
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        after = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"notify_expiring_days": 5}).json()
        assert after["bot_token_set"] is True
        assert after["notify_expiring_days"] == 5


def test_enabling_without_a_credential_is_refused() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": "", "enabled": False})
        r = client.post("/api/settings/telegram", auth=ADMIN,
                        json={"enabled": True, "bot_token": ""})
        assert r.status_code >= 400


def test_malformed_credential_is_refused() -> None:
    with TestClient(app) as client:
        r = client.post("/api/settings/telegram", auth=ADMIN,
                        json={"bot_token": "no-colon-here", "enabled": False})
        assert r.status_code >= 400


def test_telegram_settings_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/settings/telegram").status_code == 401
        assert client.post("/api/settings/telegram", json={}).status_code == 401


# -- menus differ by audience -----------------------------------------------

class _FakeMembers:
    """Just enough member service to drive menu selection."""

    def __init__(self, linked: dict[str, dict] | None = None) -> None:
        self._linked = linked or {}
        self.bound: list[tuple] = []
        self.unbound: list[str] = []

    def find_by_telegram(self, tg_user_id: str):
        return self._linked.get(str(tg_user_id))

    def bind_telegram(self, user_id, tg_user_id, tg_username="", actor="operator"):
        self.bound.append((user_id, tg_user_id, tg_username))
        self._linked[str(tg_user_id)] = {
            "emby_user_id": user_id, "username": "someone", "status": "active",
            "expires_at": int(time.time()) + 86400 * 30,
        }
        return self._linked[str(tg_user_id)]

    def unbind_telegram(self, user_id, actor="operator"):
        self.unbound.append(user_id)
        for k, v in list(self._linked.items()):
            if v["emby_user_id"] == user_id:
                self._linked.pop(k)
        return {"emby_user_id": user_id}

    def devices(self, user_id):
        return []


def _bot(members: _FakeMembers) -> TelegramBot:
    return TelegramBot(lambda: {"enabled": False, "bot_token": ""}, members)


def test_guest_and_member_see_different_menus() -> None:
    member_row = {"emby_user_id": "u1", "username": "someone", "status": "active",
                  "expires_at": int(time.time()) + 86400}
    bot = _bot(_FakeMembers({"999": member_row}))

    guest_body, guest_keys = bot._home("111", "Stranger")
    member_body, member_keys = bot._home("999", "Friend")

    guest_actions = {b["callback_data"] for row in guest_keys for b in row}
    member_actions = {b["callback_data"] for row in member_keys for b in row}

    # A guest can only usefully link an account.
    assert "bind" in guest_actions
    assert "me" not in guest_actions
    assert "devices" not in guest_actions

    # A member is past that step and must not be offered it again.
    assert "bind" not in member_actions
    assert {"me", "expiry", "devices", "usage", "unbind"} <= member_actions

    assert "还没有绑定" in guest_body
    assert "someone" in member_body


def test_menu_follows_state_after_binding() -> None:
    """A chat that binds mid-conversation must stop seeing the guest menu."""
    members = _FakeMembers()
    bot = _bot(members)
    assert "bind" in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}

    members.bind_telegram("u9", "42", "x")
    assert "bind" not in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}


# -- bind codes -------------------------------------------------------------

def test_bind_code_is_single_use() -> None:
    codes = BindCodes()
    code, _ = codes.issue("u1", "someone")
    assert codes.redeem(code) == ("u1", "someone")
    assert codes.redeem(code) is None, "a replayed code must not bind again"


def test_bind_code_expires() -> None:
    codes = BindCodes(ttl=-1)
    code, _ = codes.issue("u1", "someone")
    assert codes.redeem(code) is None


def test_reissuing_invalidates_the_previous_code() -> None:
    """A forgotten code left in a chat must not stay redeemable forever."""
    codes = BindCodes()
    first, _ = codes.issue("u1", "someone")
    second, _ = codes.issue("u1", "someone")
    assert first != second
    assert codes.redeem(first) is None
    assert codes.redeem(second) == ("u1", "someone")


def test_bind_code_ignores_case_and_padding() -> None:
    codes = BindCodes()
    code, _ = codes.issue("u1", "someone")
    assert codes.redeem(f"  {code.lower()}  ") == ("u1", "someone")


def test_wrong_code_does_not_bind_anything() -> None:
    members = _FakeMembers()
    bot = _bot(members)
    sent: list[str] = []

    async def fake_send(chat, text, keyboard=None):
        sent.append(text)
        return True

    bot.send = fake_send  # type: ignore[assignment]
    asyncio.run(bot._try_bind(1, "42", {"id": 42}, "ZZZZZZ"))
    assert members.bound == []
    assert any("无效" in s for s in sent)


def test_already_linked_chat_cannot_bind_a_second_account() -> None:
    members = _FakeMembers({"42": {"emby_user_id": "u1", "username": "a",
                                   "status": "active", "expires_at": None}})
    bot = _bot(members)
    code, _ = bot.codes.issue("u2", "b")
    sent: list[str] = []

    async def fake_send(chat, text, keyboard=None):
        sent.append(text)
        return True

    bot.send = fake_send  # type: ignore[assignment]
    asyncio.run(bot._try_bind(1, "42", {"id": 42}, code))
    assert members.bound == []
    assert any("已经绑定" in s for s in sent)


# -- bind code endpoint -----------------------------------------------------

def test_bind_code_endpoint_requires_auth_and_a_real_member() -> None:
    with TestClient(app) as client:
        assert client.post("/api/members/whoever/telegram/bind-code").status_code == 401
        r = client.post("/api/members/does-not-exist/telegram/bind-code", auth=ADMIN)
        assert r.status_code == 404


# -- disabled bot behaves ---------------------------------------------------

def test_a_bot_without_a_credential_reports_disabled_and_does_not_call_out() -> None:
    bot = _bot(_FakeMembers())
    assert bot.enabled is False
    assert bot.status()["running"] is False
    # Nothing stored means no outbound call is even attempted.
    assert asyncio.run(bot._call("getMe")) is None
