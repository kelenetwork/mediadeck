"""The bot is the front door: it registers people and holds a bearer credential.

What these tests pin down:

- Registration creates a real Emby account from the chat. There is no code to
  copy, because the chat itself proves who is asking.
- The two cases that *cannot* be self-proven — claiming a pre-existing account,
  or moving one to a new Telegram id — go through approval instead.
- Every gate is re-checked at creation time, not only when the conversation
  started: a slot can fill while someone is still typing a username.
- The credential never travels back to a browser or into the audit trail.
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.main import app
from app.modules.settings import mask_secret
from app.modules.telegram import USERNAME_RE, TelegramBot, generate_password

# Shaped like a real credential so format validation is exercised. Assembled
# from parts so nothing in this file can be mistaken for a live one.
FAKE_CRED = "1234567" + ":" + "placeholder-not-a-real-credential"

ADMIN = ("admin", "change-me")


# -- the credential must not leak -------------------------------------------

def test_saved_credential_never_comes_back_to_the_browser() -> None:
    with TestClient(app) as client:
        saved = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"bot_token": FAKE_CRED, "enabled": True}).json()
        assert saved["bot_token_set"] is True
        assert FAKE_CRED not in str(saved)
        assert saved["bot_token_masked"] == mask_secret(FAKE_CRED)

        fetched = client.get("/api/settings/telegram", auth=ADMIN).json()
        assert FAKE_CRED not in str(fetched)
        assert "bot_token" not in fetched


def test_audit_records_that_it_changed_not_what_it_changed_to() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "settings.telegram" in body
        assert FAKE_CRED not in body


def test_unretyped_credential_keeps_the_stored_one() -> None:
    """An edit form that only flips a checkbox must not wipe the credential."""
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        after = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"register_days": 5}).json()
        assert after["bot_token_set"] is True
        assert after["register_days"] == 5


def test_enabling_without_a_credential_is_refused() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": "", "enabled": False})
        assert client.post("/api/settings/telegram", auth=ADMIN,
                           json={"enabled": True, "bot_token": ""}).status_code >= 400


def test_registration_cannot_be_opened_on_a_stopped_bot() -> None:
    """Advertising a door nobody can walk through would just confuse members."""
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": "", "enabled": False})
        r = client.post("/api/settings/telegram", auth=ADMIN,
                        json={"enabled": False, "registration_enabled": True})
        assert r.status_code >= 400


def test_settings_bounds_are_enforced() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        for bad in ({"register_days": -1}, {"register_days": 99999},
                    {"max_users": -5}, {"register_days": "abc"}):
            assert client.post("/api/settings/telegram", auth=ADMIN,
                               json=bad).status_code >= 400, bad


def test_scheduling_settings_moved_to_the_plugin_cards() -> None:
    """The ranking post and expiry reminder are plugins now.

    Two switches for one post is how it ends up going out twice, so the old
    keys must be gone from the Telegram payload rather than merely ignored.
    """
    with TestClient(app) as client:
        cfg = client.get("/api/settings/telegram", auth=ADMIN).json()
        for gone in ("rankings_enabled", "rankings_hour", "rankings_chat",
                     "notify_expiring", "notify_expiring_days"):
            assert gone not in cfg
        ids = {c["id"] for c in client.get("/api/plugins", auth=ADMIN).json()}
        assert {"rankings_post", "expiry_reminder"} <= ids


def test_telegram_endpoints_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/settings/telegram").status_code == 401
        assert client.get("/api/telegram/requests").status_code == 401
        assert client.post("/api/telegram/group-audit").status_code == 401


# -- fakes ------------------------------------------------------------------

class _FakeEmby:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[str] = []
        self.passwords: list[tuple[str, str]] = []

    async def create_user(self, name: str):
        if self.fail:
            return None
        self.created.append(name)
        return {"Id": f"emby-{name}", "Name": name}

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        self.passwords.append((user_id, new_password))
        return True


class _FakeMembers:
    def __init__(self, linked: dict | None = None) -> None:
        self._linked = linked or {}
        self.upserted: list[tuple] = []
        self.bound: list[tuple] = []
        self._by_name: dict[str, dict] = {}

    def find_by_telegram(self, tg_user_id: str):
        return self._linked.get(str(tg_user_id))

    def find_by_username(self, username: str):
        return self._by_name.get(str(username).lower())

    def upsert(self, user_id, username, payload, actor="system"):
        self.upserted.append((user_id, username, payload))
        row = {"emby_user_id": user_id, "username": username,
               "status": "active", "expires_at": payload.get("expires_at")}
        self._by_name[username.lower()] = row
        return row

    def bind_telegram(self, user_id, tg_user_id, tg_username="", actor="operator"):
        self.bound.append((user_id, tg_user_id, tg_username))
        self._linked[str(tg_user_id)] = {
            "emby_user_id": user_id, "username": "someone",
            "status": "active", "expires_at": int(time.time()) + 86400,
        }
        return self._linked[str(tg_user_id)]

    def devices(self, user_id):
        return []

    def list(self, **kw):
        return list(self._linked.values())


def _bot(members=None, emby=None, cfg=None, db=None) -> TelegramBot:
    base = {"enabled": True, "bot_token": FAKE_CRED,
            "registration_enabled": True, "register_days": 30,
            "max_users": 0, "require_group": "", "default_group_id": "",
            "emby_public_url": "https://emby.example"}
    base.update(cfg or {})
    bot = TelegramBot(lambda: base, members or _FakeMembers(),
                      emby=emby, db=db)
    bot.sent = []  # type: ignore[attr-defined]

    async def fake_send(chat, text, keyboard=None):
        bot.sent.append(text)  # type: ignore[attr-defined]
        return True

    bot.send = fake_send  # type: ignore[assignment]
    return bot


# -- menus differ by audience -----------------------------------------------

def test_guest_and_member_see_different_menus() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    bot = _bot(_FakeMembers({"999": member}))

    guest_body, guest_keys = bot._home("111", "Stranger")
    member_body, member_keys = bot._home("999", "Friend")

    guest_actions = {b["callback_data"] for row in guest_keys for b in row}
    member_actions = {b["callback_data"] for row in member_keys for b in row}

    # A guest can only register or claim; member-only views are absent.
    assert "register" in guest_actions
    assert "claim" in guest_actions
    assert "devices" not in guest_actions

    # A member is past that step and must not be offered it again.
    assert "register" not in member_actions
    assert {"me", "expiry", "devices", "usage", "top"} <= member_actions

    assert "没有账号" in guest_body
    assert "someone" in member_body


def test_menu_follows_state_after_registering() -> None:
    members = _FakeMembers()
    bot = _bot(members)
    assert "register" in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}
    members.bind_telegram("u9", "42", "x")
    assert "register" not in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}


# -- registration -----------------------------------------------------------

def test_registration_creates_the_account_and_links_the_chat() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    bot = _bot(members, emby)
    asyncio.run(bot._finish_registration(1, "42", "tguser", "newmember"))

    assert emby.created == ["newmember"]
    assert members.upserted and members.upserted[0][1] == "newmember"
    # The Telegram id is the identity, recorded as owner at creation time.
    assert members.bound == [("emby-newmember", "42", "tguser")]


def test_the_generated_password_is_shown_once_and_not_chosen() -> None:
    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby)
    asyncio.run(bot._finish_registration(1, "42", "u", "newmember"))

    assert emby.passwords, "a password should have been set"
    issued = emby.passwords[0][1]
    assert len(issued) >= 12
    assert any(issued in msg for msg in bot.sent)  # type: ignore[attr-defined]
    assert any("不会再发第二次" in msg for msg in bot.sent)  # type: ignore[attr-defined]


def test_bad_usernames_are_refused_before_touching_emby() -> None:
    for bad in ("ab", "1startsdigit", "has space", "sym!bol", "x" * 21, ""):
        assert not USERNAME_RE.match(bad), bad
        emby = _FakeEmby()
        bot = _bot(_FakeMembers(), emby)
        asyncio.run(bot._finish_registration(1, "42", "u", bad))
        assert emby.created == [], f"{bad!r} reached Emby"


def test_a_taken_username_fails_without_creating_a_member() -> None:
    members, emby = _FakeMembers(), _FakeEmby(fail=True)
    bot = _bot(members, emby)
    asyncio.run(bot._finish_registration(1, "42", "u", "duplicate"))
    assert members.upserted == []
    assert members.bound == []
    assert any("创建失败" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_registration_closed_is_refused() -> None:
    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby, cfg={"registration_enabled": False})
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []
    assert any("暂停注册" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_a_full_slot_cap_blocks_registration() -> None:
    class _Db:
        def one(self, sql, params=()):
            return {"n": 10}

    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby, cfg={"max_users": 10}, db=_Db())
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []
    assert any("名额已满" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_the_cap_is_rechecked_at_creation_not_only_at_the_start() -> None:
    """A slot can fill while someone is still typing their username."""
    class _Db:
        def one(self, sql, params=()):
            return {"n": 5}

    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby, cfg={"max_users": 5}, db=_Db())
    asyncio.run(bot._finish_registration(1, "42", "u", "toolate"))
    assert emby.created == [], "the account was created after the cap was reached"


def test_an_existing_member_cannot_register_twice() -> None:
    members = _FakeMembers({"42": {"emby_user_id": "u1", "username": "a",
                                   "status": "active", "expires_at": None}})
    emby = _FakeEmby()
    bot = _bot(members, emby)
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []


def test_register_days_zero_means_no_expiry() -> None:
    members = _FakeMembers()
    bot = _bot(members, _FakeEmby(), cfg={"register_days": 0})
    asyncio.run(bot._finish_registration(1, "42", "u", "forever"))
    assert "expires_at" not in members.upserted[0][2]


# -- group requirement ------------------------------------------------------

def test_no_group_configured_lets_everyone_in() -> None:
    bot = _bot(cfg={"require_group": ""})
    allowed, _ = asyncio.run(bot.in_required_group("42"))
    assert allowed is True


def test_a_failed_group_lookup_does_not_close_registration() -> None:
    """Telegram being unreachable must not lock out every new member."""
    bot = _bot(cfg={"require_group": "@somegroup"})

    async def fail(*a, **k):
        return None

    bot._call = fail  # type: ignore[assignment]
    allowed, reason = asyncio.run(bot.in_required_group("42"))
    assert allowed is True
    assert reason == "group-check-unavailable"


def test_a_user_who_left_the_group_is_refused() -> None:
    bot = _bot(cfg={"require_group": "@somegroup"})

    async def left(*a, **k):
        return {"status": "left"}

    bot._call = left  # type: ignore[assignment]
    allowed, _ = asyncio.run(bot.in_required_group("42"))
    assert allowed is False


def test_group_members_of_every_rank_are_accepted() -> None:
    for status in ("creator", "administrator", "member", "restricted"):
        bot = _bot(cfg={"require_group": "@g"})

        async def ok(*a, _s=status, **k):
            return {"status": _s}

        bot._call = ok  # type: ignore[assignment]
        assert asyncio.run(bot.in_required_group("42"))[0] is True, status


# -- claim / rebind approval ------------------------------------------------

class _ReqDb:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next = 1

    def one(self, sql, params=()):
        if "tg_requests WHERE id" in sql:
            return next((r for r in self.rows if r["id"] == params[0]), None)
        if "tg_requests WHERE tg_user_id" in sql:
            return next(({"x": 1} for r in self.rows
                         if r["tg_user_id"] == params[0] and r["status"] == "pending"),
                        None)
        return {"n": 0}

    def query(self, sql, params=()):
        return [r for r in self.rows if r["status"] == "pending"]

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO tg_requests"):
            self.rows.append({
                "id": self._next, "kind": params[0], "tg_user_id": params[1],
                "tg_username": params[2], "wanted_username": params[3],
                "status": "pending", "created_at": params[4],
            })
            self._next += 1
        elif sql.startswith("UPDATE tg_requests"):
            rid = params[-1]
            for r in self.rows:
                if r["id"] == rid:
                    r["status"] = params[0]


def test_a_claim_becomes_a_pending_request_not_an_instant_link() -> None:
    members, db = _FakeMembers(), _ReqDb()
    bot = _bot(members, db=db)
    assert bot._create_request("bind", "42", "tguser", "oldaccount") is True
    assert members.bound == [], "claiming must not link before review"
    assert len(bot.pending_requests()) == 1


def test_a_second_request_from_one_chat_is_refused() -> None:
    bot = _bot(db=_ReqDb())
    assert bot._create_request("bind", "42", "u", "acct") is True
    assert bot._create_request("bind", "42", "u", "other") is False


def test_approving_links_the_account() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "oldaccount", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "tguser", "oldaccount")

    result = bot.review_request(1, approve=True, reviewer="admin")
    assert result["approved"] is True
    assert members.bound == [("emby-old", "42", "tguser")]


def test_rejecting_links_nothing() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "oldaccount", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "tguser", "oldaccount")

    bot.review_request(1, approve=False, reviewer="admin")
    assert members.bound == []


def test_approving_a_claim_for_an_unknown_account_fails_loudly() -> None:
    bot = _bot(_FakeMembers(), db=_ReqDb())
    bot._create_request("bind", "42", "u", "does-not-exist")
    try:
        bot.review_request(1, approve=True)
    except ValueError:
        return
    raise AssertionError("approving a claim on a missing account should fail")


def test_a_request_cannot_be_reviewed_twice() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "acct", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "u", "acct")
    bot.review_request(1, approve=True)
    try:
        bot.review_request(1, approve=True)
    except KeyError:
        return
    raise AssertionError("a settled request should not be reviewable again")


# -- rankings ---------------------------------------------------------------

class _FakeStats:
    def top_users(self, days=30, limit=20):
        return [{"username": "alice", "hours": 12.5, "plays": 30, "bytes": 1},
                {"username": "bob", "hours": 8.0, "plays": 12, "bytes": 1}]

    def top_titles(self, days=30, limit=20):
        return [{"title": "Some Show", "plays": 40, "hours": 20.0,
                 "viewers": 5, "type": "Series"}]


def test_rankings_list_both_viewers_and_titles() -> None:
    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_FakeStats())
    text = bot._rankings_text(1)
    assert "alice" in text and "12.5" in text
    assert "Some Show" in text and "40" in text


def test_rankings_say_so_when_there_is_nothing_yet() -> None:
    class _Empty:
        def top_users(self, **k):
            return []

        def top_titles(self, **k):
            return []

    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_Empty())
    assert "还没有播放记录" in bot._rankings_text(1)


def test_a_broken_stats_source_does_not_crash_the_bot() -> None:
    class _Broken:
        def top_users(self, **k):
            raise RuntimeError("db down")

        def top_titles(self, **k):
            raise RuntimeError("db down")

    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_Broken())
    assert "还没有播放记录" in bot._rankings_text(1)


# -- disabled bot behaves ---------------------------------------------------

def test_a_bot_without_a_credential_reports_disabled_and_does_not_call_out() -> None:
    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""}, _FakeMembers())
    assert bot.enabled is False
    assert bot.status()["running"] is False
    assert asyncio.run(bot._call("getMe")) is None


def test_generated_passwords_differ() -> None:
    assert generate_password() != generate_password()
