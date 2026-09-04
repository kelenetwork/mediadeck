"""Telegram bot: registration, account self-service and rankings.

The bot is the front door for new members. Someone who has never been here
registers in the chat and walks away with a working Emby account; someone who
already has one lands on their own status. The keyboard is chosen from that
state on every render, so neither audience is offered buttons that lead
nowhere.

Registration creates the Emby account directly. There is no code to copy from
a panel, because the chat itself already proves who is asking: the Telegram id
is the identity, and it is recorded as the owner at creation time. That leaves
exactly two cases needing human review, and they both go through the approval
queue rather than the registration path:

- someone whose Emby account predates the bot and wants to claim it
- someone moving their account to a different Telegram id

Both are attempts to take control of an account the requester cannot otherwise
prove they own, so an operator decides.

Passwords are generated, never typed. A chat transcript is not a safe place to
put one, and a password the member chose in a hurry is the one they reuse.

Polling, not webhooks. A webhook needs a public HTTPS route into the panel;
long polling reaches out instead, so the panel stays reachable only from where
it already was.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import string
import time
from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"

# Telegram closes an idle long poll itself; this only has to be shorter than
# the client timeout so a hung socket is noticed rather than waited on forever.
POLL_TIMEOUT = 25
HTTP_TIMEOUT = POLL_TIMEOUT + 10

# A username has to survive being an Emby login and a path component.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")

# Registration conversation state is intentionally short-lived: an abandoned
# half-finished signup should not hold a slot or confuse the next /start.
PENDING_TTL = 600.0

REQUEST_KINDS = ("bind", "rebind")


def generate_password(length: int = 12) -> str:
    """Passwords are issued, not chosen: the member never types one in chat."""
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def _fmt_expiry(expires_at: int | None) -> str:
    if not expires_at:
        return "永久"
    left = expires_at - int(time.time())
    if left <= 0:
        return "已过期"
    days = left // 86400
    if days >= 1:
        return f"{days} 天后到期"
    return f"{max(1, left // 3600)} 小时内到期"


def _fmt_bytes(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class TelegramBot:
    """Long-polling bot bound to the panel's member records."""

    def __init__(self, config_provider: Any, members: Any, emby: Any = None,
                 stats: Any = None, db: Any = None,
                 registration: Any = None) -> None:
        self._config = config_provider
        self._members = members
        self._emby = emby
        self._stats = stats
        self._db = db
        self._registration = registration
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._last_error = ""
        self._last_poll_at = 0.0
        self._started_at = 0.0
        # chat id -> what the bot is waiting for, with a deadline
        self._pending: dict[str, tuple[str, float, dict[str, Any]]] = {}

    # -- config ---------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        return self._config() or {}

    def _token(self) -> str:
        return str(self._cfg().get("bot_token") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled")) and bool(self._token())

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "enabled": self.enabled,
            "last_poll_at": int(self._last_poll_at) or None,
            "last_error": self._last_error,
            "pending_conversations": len(self._pending),
            "started_at": int(self._started_at) or None,
        }

    # -- transport ------------------------------------------------------------

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    timeout: float = 20) -> Any:
        auth_part = self._token()
        if not auth_part:
            return None
        url = f"{API_ROOT}/bot{auth_part}/{method}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload or {})
            body = r.json()
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            # The token is in the URL, so raw exception text is not safe to keep.
            self._last_error = f"{type(exc).__name__}: 请求失败"
            return None
        if not body.get("ok"):
            self._last_error = str(body.get("description") or "Telegram 拒绝了请求")
            return None
        self._last_error = ""
        return body.get("result")

    async def verify(self) -> dict[str, Any]:
        """Check the credential by asking who the bot is. Never echoes it."""
        me = await self._call("getMe", timeout=15)
        if not me:
            return {"ok": False, "error": self._last_error or "无法连接 Telegram"}
        return {"ok": True, "username": me.get("username", ""),
                "name": me.get("first_name", ""), "id": me.get("id")}

    async def send(self, chat_id: str | int, text: str,
                   keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return await self._call("sendMessage", payload) is not None

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text})

    async def _edit(self, chat_id: str | int, message_id: int, text: str,
                    keyboard: list[list[dict[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        await self._call("editMessageText", payload)

    # -- group membership -----------------------------------------------------

    async def in_required_group(self, tg_user_id: str) -> tuple[bool, str]:
        """Is this user in the group registration requires?

        Returns (allowed, reason). With no group configured everyone passes.

        A lookup failure passes too: Telegram being unreachable, or the bot not
        being an administrator of the group, must not silently close
        registration for everyone. The operator sees the reason instead.
        """
        chat = str(self._cfg().get("require_group") or "").strip()
        if not chat:
            return True, ""
        result = await self._call(
            "getChatMember", {"chat_id": chat, "user_id": int(tg_user_id)},
            timeout=15)
        if result is None:
            return True, "group-check-unavailable"
        status = str((result or {}).get("status") or "")
        if status in ("creator", "administrator", "member", "restricted"):
            return True, status
        return False, status or "left"

    # -- menus ----------------------------------------------------------------

    @staticmethod
    def guest_menu() -> list[list[dict[str, str]]]:
        """No account yet: register, or claim one that already exists."""
        return [
            [{"text": "🆕 注册账号", "callback_data": "register"}],
            [{"text": "🔗 认领已有账号", "callback_data": "claim"},
             {"text": "❓ 使用说明", "callback_data": "help"}],
        ]

    @staticmethod
    def member_menu() -> list[list[dict[str, str]]]:
        return [
            [{"text": "👤 我的账号", "callback_data": "me"},
             {"text": "⏳ 有效期", "callback_data": "expiry"}],
            [{"text": "📺 我的设备", "callback_data": "devices"},
             {"text": "📊 观看统计", "callback_data": "usage"}],
            [{"text": "🎫 我的邀请码", "callback_data": "invites"},
             {"text": "🏆 本站排行", "callback_data": "top"}],
            [{"text": "🔑 重置密码", "callback_data": "resetpw"},
             {"text": "🔄 刷新", "callback_data": "home"}],
        ]

    def _member_for_chat(self, tg_user_id: str) -> dict[str, Any] | None:
        return self._members.find_by_telegram(str(tg_user_id))

    @staticmethod
    def _status_label(member: dict[str, Any]) -> str:
        return {
            "active": "✅ 正常", "suspended": "⛔ 已停用",
            "expired": "⌛ 已过期", "exhausted": "📵 已超额",
            "pending": "🕓 待开通",
        }.get(str(member.get("status") or ""), str(member.get("status") or "未知"))

    def _home(self, tg_user_id: str, tg_name: str) -> tuple[str, list[list[dict[str, str]]]]:
        member = self._member_for_chat(tg_user_id)
        if not member:
            state = "开放注册中" if self._registration_open() else "当前暂停注册"
            body = (
                f"👋 你好，{tg_name}\n\n"
                f"这里是影视库的账号服务。<b>{state}</b>\n\n"
                "· 没有账号 → 点「注册账号」，需要邀请码或卡密\n"
                "· 已有账号但没关联 → 点「认领已有账号」，需要管理员确认"
            )
            return body, self.guest_menu()
        body = (
            f"🎬 <b>{member.get('username') or '成员'}</b>\n"
            f"状态：{self._status_label(member)}\n"
            f"有效期：{_fmt_expiry(member.get('expires_at'))}\n\n"
            "选择要查看的内容："
        )
        return body, self.member_menu()

    # -- registration ---------------------------------------------------------

    def _sweep_pending(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for chat, (_, deadline, _) in list(self._pending.items()):
            if deadline <= now:
                self._pending.pop(chat, None)

    def registration_slots(self) -> tuple[int, int]:
        """(used, cap). A cap of 0 means unlimited."""
        cap = int(self._cfg().get("max_users") or 0)
        used = 0
        if self._db is not None:
            with contextlib.suppress(Exception):
                row = self._db.one("SELECT COUNT(*) AS n FROM members")
                used = int((row or {}).get("n") or 0)
        return used, cap

    def _registration_open(self) -> bool:
        """Is any channel open? Three closed switches is a closed door."""
        cfg = self._cfg()
        return any(bool(cfg.get(key, True)) for key in
                   ("allow_admin_grant", "allow_invite", "allow_redeem"))

    async def _registration_blocked(self, tg_user_id: str) -> str:
        """Why this user may not register right now, or '' if they may.

        This is the gate that applies to *everyone*, whatever channel they came
        through: slots, group membership, Emby being reachable. Which channel
        admits them is a separate question, answered by RegistrationService.
        """
        if not self._registration_open():
            return "当前暂停注册，请稍后再来或联系管理员。"
        if self._emby is None:
            return "后台未连接 Emby，暂时无法开户。"
        used, cap = self.registration_slots()
        if cap and used >= cap:
            return f"注册名额已满（{used}/{cap}），请联系管理员。"
        allowed, _status = await self.in_required_group(tg_user_id)
        if not allowed:
            return "需要先加入官方群组才能注册。"
        return ""

    _USERNAME_PROMPT = (
        "🆕 <b>注册账号</b>\n\n请直接发送你想要的用户名：\n\n"
        "· 3–20 个字符，字母开头\n"
        "· 只能用字母、数字和下划线\n\n"
        "<i>密码由系统生成，不需要你输入。10 分钟内有效。</i>")

    async def _start_registration(self, chat_id: Any, tg_user_id: str) -> None:
        """Pre-authorised users skip straight to the username.

        Asking someone the operator already named for a code they were never
        given is a dead end they cannot get out of.
        """
        if self._member_for_chat(tg_user_id):
            await self.send(chat_id, "你已经有账号了。", self.member_menu())
            return
        blocked = await self._registration_blocked(tg_user_id)
        if blocked:
            await self.send(chat_id, f"🚫 {blocked}", self.guest_menu())
            return
        self._sweep_pending()

        admission = self._resolve(tg_user_id, None)
        if admission is not None and admission.allowed:
            self._pending[str(chat_id)] = (
                "username", time.time() + PENDING_TTL,
                {"admission": admission})
            await self.send(chat_id, self._USERNAME_PROMPT)
            return

        if self._registration is None:
            # No registration service wired (older deployments / tests): fall
            # back to the plain username step rather than blocking everyone.
            self._pending[str(chat_id)] = ("username", time.time() + PENDING_TTL, {})
            await self.send(chat_id, self._USERNAME_PROMPT)
            return

        self._pending[str(chat_id)] = (
            "credential", time.time() + PENDING_TTL, {})
        await self.send(
            chat_id,
            "🎟 <b>注册账号</b>\n\n请发送你的<b>邀请码</b>或<b>卡密</b>：\n\n"
            "· 邀请码由老用户生成，8 位\n"
            "· 卡密由管理员发放，12 位\n\n"
            "<i>大小写不敏感，10 分钟内有效。</i>")

    def _resolve(self, tg_user_id: str, credential: str | None) -> Any:
        """Ask the registration service for a verdict, tolerating its absence."""
        if self._registration is None:
            return None
        try:
            return self._registration.resolve(tg_user_id, credential)
        except Exception:  # noqa: BLE001 - a member must not see a stack trace
            self._last_error = "注册通道解析失败"
            return None

    async def _submit_credential(self, chat_id: Any, tg_user_id: str,
                                 credential: str) -> None:
        """Validate the code, then move to the username step.

        The credential is checked but *not* spent here: the account does not
        exist yet, and a failure after this point must leave the code good.
        """
        admission = self._resolve(tg_user_id, credential)
        if admission is None:
            self._pending.pop(str(chat_id), None)
            await self.send(chat_id, "🚫 注册暂时不可用，请稍后再试。",
                            self.guest_menu())
            return
        if not admission.allowed:
            # The conversation stays open: a mistyped code should cost one
            # message, not the whole flow.
            await self.send(
                chat_id,
                f"❌ {admission.reason}\n\n请重新发送邀请码或卡密，或点下面的按钮返回。",
                [[{"text": "↩️ 返回", "callback_data": "home"}]])
            return
        self._pending[str(chat_id)] = (
            "username", time.time() + PENDING_TTL, {"admission": admission})
        await self.send(chat_id, f"✅ {admission.reason}\n\n" + self._USERNAME_PROMPT)

    async def _finish_registration(self, chat_id: Any, tg_user_id: str,
                                   tg_username: str, username: str,
                                   admission: Any = None) -> None:
        username = username.strip()
        if not USERNAME_RE.match(username):
            await self.send(chat_id,
                            "❌ 用户名不符合要求：3–20 字符、字母开头、只含字母数字下划线。\n"
                            "请重新发送一个。")
            return

        # Re-check at the moment of creation, not only when the conversation
        # started: a slot can fill or registration can close while someone is
        # still typing.
        blocked = await self._registration_blocked(tg_user_id)
        if blocked:
            self._pending.pop(str(chat_id), None)
            await self.send(chat_id, f"🚫 {blocked}", self.guest_menu())
            return

        await self.send(chat_id, "⏳ 正在创建账号…")
        password = generate_password()
        try:
            created = await self._emby.create_user(username)
        except Exception:  # noqa: BLE001 - message is for a member, not a dev
            created = None
        if not created or not created.get("Id"):
            self._pending.pop(str(chat_id), None)
            await self.send(
                chat_id,
                "❌ 创建失败，可能是用户名已被占用。请点「注册账号」换一个再试。",
                self.guest_menu())
            return

        emby_id = str(created["Id"])
        with contextlib.suppress(Exception):
            await self._emby.set_user_password(emby_id, password)

        cfg = self._cfg()
        now = int(time.time())
        # The admission decides the terms when there is one: a card bought for
        # a better group must not silently downgrade to the default plan.
        days = int(cfg.get("register_days") or 0)
        group_id = str(cfg.get("default_group_id") or "")
        via, inviter_id = "admin", ""
        if admission is not None:
            via = str(getattr(admission, "via", "") or "admin")
            inviter_id = str(getattr(admission, "inviter_id", "") or "")
            group_id = str(getattr(admission, "group_id", "") or group_id)
            days = int(getattr(admission, "days", 0) or 0)

        payload: dict[str, Any] = {
            "status": "active",
            "register_via": via,
            "inviter_id": inviter_id,
            "register_at": now,
        }
        if group_id:
            payload["group_id"] = group_id
        if days > 0:
            payload["expires_at"] = now + days * 86400

        self._members.upsert(emby_id, username, payload, actor="telegram")
        self._members.bind_telegram(emby_id, tg_user_id, tg_username,
                                    actor="telegram")
        # Only now: the account exists and the chat is linked, so spending the
        # credential can no longer strand someone who paid for it.
        if admission is not None and self._registration is not None:
            with contextlib.suppress(Exception):
                self._registration.consume(admission, emby_id)
        self._pending.pop(str(chat_id), None)

        server = str(cfg.get("emby_public_url") or "").strip()
        lines = [
            "✅ <b>注册成功</b>\n",
            f"用户名：<code>{username}</code>",
            f"密码：<code>{password}</code>",
        ]
        if server:
            lines.append(f"服务器：{server}")
        if days > 0:
            lines.append(f"有效期：{days} 天")
        lines.append("\n<i>请立刻保存密码，这条消息不会再发第二次。</i>")
        await self.send(chat_id, "\n".join(lines), self.member_menu())

    # -- member invites -------------------------------------------------------

    async def _invites_view(self, chat_id: Any, message_id: int,
                            member: dict[str, Any], mint: bool = False) -> None:
        """A member's own invite codes, and the slots they have left.

        Minting is a member-visible spend: one slot becomes one single-use
        code. Showing the remaining count next to the button is what stops the
        obvious "why did nothing happen" when they are out.
        """
        if self._registration is None:
            await self._edit(chat_id, message_id, "邀请功能暂未开放。",
                             self.member_menu())
            return
        user_id = str(member.get("emby_user_id") or "")
        notice = ""
        if mint:
            try:
                issued = self._registration.spend_quota_for_invite(user_id)
                notice = f"✅ 新邀请码：<code>{issued.get('code', '')}</code>\n\n"
            except Exception as exc:  # noqa: BLE001 - shown to a member
                notice = f"❌ {exc}\n\n"

        quota = 0
        codes: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            quota = self._registration.invite_quota(user_id)
            codes = self._registration.list_invites(user_id, limit=10)

        lines = [f"{notice}🎫 <b>我的邀请码</b>\n", f"剩余名额：<b>{quota}</b>"]
        if codes:
            lines.append("")
            for row in codes:
                left = int(row.get("uses_left") or 0)
                if row.get("revoked"):
                    tail = "已作废"
                elif left <= 0:
                    tail = "已用完"
                else:
                    tail = f"剩 {left} 次 · {_fmt_expiry(row.get('expires_at'))}"
                lines.append(f"<code>{row.get('code', '')}</code> · {tail}")
        else:
            lines.append("\n你还没有生成过邀请码。")
        lines.append("\n<i>把邀请码发给朋友，他们注册时填写即可。</i>")

        keyboard: list[list[dict[str, str]]] = []
        if quota > 0:
            keyboard.append(
                [{"text": f"➕ 生成新码（剩 {quota}）",
                  "callback_data": "invite_new"}])
        keyboard.append([{"text": "↩️ 返回", "callback_data": "home"}])
        await self._edit(chat_id, message_id, "\n".join(lines), keyboard)

    # -- claim / rebind requests ---------------------------------------------

    def _create_request(self, kind: str, tg_user_id: str, tg_username: str,
                        wanted: str) -> bool:
        if self._db is None or kind not in REQUEST_KINDS:
            return False
        existing = self._db.one(
            "SELECT 1 AS x FROM tg_requests WHERE tg_user_id=? AND status='pending'",
            (str(tg_user_id),))
        if existing:
            return False
        self._db.execute(
            "INSERT INTO tg_requests"
            "(kind,tg_user_id,tg_username,wanted_username,status,created_at) "
            "VALUES(?,?,?,?, 'pending', ?)",
            (kind, str(tg_user_id), tg_username, wanted, int(time.time())))
        return True

    def pending_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        return self._db.query(
            "SELECT * FROM tg_requests WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT ?", (max(1, min(limit, 500)),))

    def review_request(self, request_id: int, approve: bool,
                       reviewer: str = "operator") -> dict[str, Any]:
        """Approve or reject. Approving is what actually moves the linkage."""
        if self._db is None:
            raise KeyError(request_id)
        row = self._db.one("SELECT * FROM tg_requests WHERE id=?", (request_id,))
        if not row or row.get("status") != "pending":
            raise KeyError(request_id)

        if approve:
            member = self._members.find_by_username(row["wanted_username"])
            if not member:
                self._db.execute(
                    "UPDATE tg_requests SET status='rejected',reviewed_at=?,"
                    "reviewed_by=?,note=? WHERE id=?",
                    (int(time.time()), reviewer, "找不到该账号", request_id))
                raise ValueError(f"找不到账号: {row['wanted_username']}")
            self._members.bind_telegram(
                member["emby_user_id"], row["tg_user_id"],
                row.get("tg_username") or "", actor=reviewer)

        self._db.execute(
            "UPDATE tg_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?",
            ("approved" if approve else "rejected", int(time.time()),
             reviewer, request_id))
        return {"id": request_id, "approved": approve,
                "tg_user_id": row["tg_user_id"]}

    # -- rankings -------------------------------------------------------------

    def _rankings_text(self, days: int = 1) -> str:
        if self._stats is None:
            return "统计暂不可用。"
        window = "今日" if days <= 1 else f"近 {days} 天"
        lines = [f"🏆 <b>{window}排行</b>\n"]
        with contextlib.suppress(Exception):
            users = self._stats.top_users(days=days, limit=5)
            if users:
                lines.append("<b>观看时长</b>")
                for i, u in enumerate(users, 1):
                    lines.append(
                        f"{i}. {u['username']} · {u['hours']} 小时 · {u['plays']} 次")
                lines.append("")
        with contextlib.suppress(Exception):
            titles = self._stats.top_titles(days=days, limit=5)
            if titles:
                lines.append("<b>热门影片</b>")
                for i, t in enumerate(titles, 1):
                    lines.append(
                        f"{i}. {t['title']} · {t['plays']} 次 · {t['hours']} 小时")
        if len(lines) == 1:
            lines.append("暂时还没有播放记录。")
        return "\n".join(lines)

    # -- update handling ------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        from_user = message.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_username = str(from_user.get("username") or "")
        tg_name = from_user.get("first_name") or tg_username or "朋友"
        text = str(message.get("text") or "").strip()
        if not chat_id or not tg_user_id:
            return

        self._sweep_pending()
        waiting = self._pending.get(str(chat_id))
        if waiting and text and not text.startswith("/"):
            kind, _, extra = waiting
            if kind == "credential":
                await self._submit_credential(chat_id, tg_user_id, text)
                return
            if kind == "username":
                await self._finish_registration(
                    chat_id, tg_user_id, tg_username, text,
                    admission=extra.get("admission"))
                return
            if kind == "claim":
                self._pending.pop(str(chat_id), None)
                created = self._create_request(
                    extra.get("request_kind", "bind"), tg_user_id,
                    tg_username, text.strip())
                await self.send(
                    chat_id,
                    "📨 已提交申请，等待管理员确认。" if created
                    else "你已经有一条待处理的申请了，请耐心等待。",
                    self.guest_menu())
                return

        body, keyboard = self._home(tg_user_id, tg_name)
        await self.send(chat_id, body, keyboard)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        from_user = callback.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_name = from_user.get("first_name") or "朋友"
        await self._answer_callback(str(callback.get("id") or ""))
        if not chat_id or not message_id:
            return

        # Re-read binding state on every tap: the member could have been
        # unlinked from the panel while this keyboard sat on their screen.
        member = self._member_for_chat(tg_user_id)

        if data == "register":
            await self._start_registration(chat_id, tg_user_id)
            return
        if data == "claim":
            self._pending[str(chat_id)] = (
                "claim", time.time() + PENDING_TTL, {"request_kind": "bind"})
            await self._edit(
                chat_id, message_id,
                "🔗 <b>认领已有账号</b>\n\n请发送你在影视库里的<b>用户名</b>。\n\n"
                "<i>管理员确认后会关联到这个 Telegram。</i>")
            return
        if data == "help":
            await self._edit(
                chat_id, message_id,
                "❓ <b>使用说明</b>\n\n"
                "· <b>注册账号</b>：直接创建，密码由系统生成\n"
                "· <b>认领已有账号</b>：老账号关联到这个 Telegram，需管理员确认\n"
                "· 关联后可查有效期、设备、用量和排行，到期前会主动提醒\n\n"
                "遇到问题请联系管理员。",
                self.member_menu() if member else self.guest_menu())
            return
        if data == "home":
            body, keyboard = self._home(tg_user_id, tg_name)
            await self._edit(chat_id, message_id, body, keyboard)
            return
        if data == "top":
            # Rankings are about the library, not one account, so they stay
            # available to anyone who found the bot.
            await self._edit(chat_id, message_id, self._rankings_text(1),
                             self.member_menu() if member else self.guest_menu())
            return

        if not member:
            await self._edit(chat_id, message_id,
                             "这个 Telegram 还没有账号。", self.guest_menu())
            return

        if data == "me":
            await self._edit(
                chat_id, message_id,
                f"👤 <b>{member.get('username') or '-'}</b>\n\n"
                f"状态：{self._status_label(member)}\n"
                f"用户组：{member.get('group_name') or '默认'}\n"
                f"有效期：{_fmt_expiry(member.get('expires_at'))}\n"
                f"备注：{member.get('note') or '—'}",
                self.member_menu())
            return
        if data == "expiry":
            expires = member.get("expires_at")
            when = time.strftime("%Y-%m-%d", time.localtime(expires)) if expires else "永久有效"
            await self._edit(
                chat_id, message_id,
                f"⏳ <b>有效期</b>\n\n到期时间：{when}\n状态：{_fmt_expiry(expires)}\n\n"
                "<i>需要续期请联系管理员。</i>",
                self.member_menu())
            return
        if data == "devices":
            devices = self._members.devices(str(member.get("emby_user_id")))
            if not devices:
                text = "📺 <b>我的设备</b>\n\n还没有记录到设备。"
            else:
                rows = []
                for d in devices[:8]:
                    seen = d.get("last_seen_at")
                    when = time.strftime("%m-%d %H:%M", time.localtime(seen)) if seen else "—"
                    flag = "🚫 " if d.get("blocked") else ""
                    rows.append(f"{flag}{d.get('device_name') or d.get('device_id')} · {when}")
                text = "📺 <b>我的设备</b>\n\n" + "\n".join(rows)
            await self._edit(chat_id, message_id, text, self.member_menu())
            return
        if data == "usage":
            used = member.get("traffic_used_bytes") or 0
            seen = member.get("last_seen_at")
            await self._edit(
                chat_id, message_id,
                f"📊 <b>观看统计</b>\n\n本周期用量：{_fmt_bytes(used)}\n"
                f"最近活跃：{time.strftime('%Y-%m-%d %H:%M', time.localtime(seen)) if seen else '—'}",
                self.member_menu())
            return
        if data in ("invites", "invite_new"):
            await self._invites_view(chat_id, message_id, member, mint=data == "invite_new")
            return
        if data == "resetpw":
            if self._emby is None:
                await self._edit(chat_id, message_id, "后台未连接 Emby，暂时无法重置。",
                                 self.member_menu())
                return
            password = generate_password()
            ok = False
            with contextlib.suppress(Exception):
                ok = await self._emby.set_user_password(
                    str(member.get("emby_user_id")), password)
            await self._edit(
                chat_id, message_id,
                (f"🔑 <b>新密码</b>\n\n<code>{password}</code>\n\n"
                 "<i>请立刻保存，这条消息不会再发第二次。</i>") if ok
                else "❌ 重置失败，请稍后再试或联系管理员。",
                self.member_menu())
            return

    # -- polling loop ---------------------------------------------------------

    async def _poll_once(self) -> None:
        result = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": POLL_TIMEOUT,
             "allowed_updates": ["message", "callback_query"]},
            timeout=HTTP_TIMEOUT)
        self._last_poll_at = time.time()
        for update in result or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            try:
                if "message" in update:
                    await self._handle_message(update["message"])
                elif "callback_query" in update:
                    await self._handle_callback(update["callback_query"])
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                self._last_error = f"处理更新失败: {type(exc).__name__}"

    async def run(self) -> None:
        """Poll while enabled; idle cheaply while not.

        Disabling the bot in the panel must not require a restart, so the loop
        stays alive and simply stops reaching out.
        """
        self._started_at = time.time()
        backoff = 1.0
        while True:
            if not self.enabled:
                await asyncio.sleep(5)
                continue
            try:
                await self._poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"轮询失败: {type(exc).__name__}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # -- outbound notifications ----------------------------------------------

    async def notify_member(self, member: dict[str, Any], text: str) -> bool:
        chat_id = member.get("tg_user_id")
        if not chat_id or not self.enabled:
            return False
        return await self.send(chat_id, text)

    async def notify_expiring(self, members: list[dict[str, Any]]) -> int:
        sent = 0
        for member in members:
            if not member.get("tg_user_id"):
                continue
            ok = await self.notify_member(
                member,
                "⏳ <b>有效期提醒</b>\n\n"
                f"账号 <b>{member.get('username') or '-'}</b> "
                f"{_fmt_expiry(member.get('expires_at'))}。\n"
                "需要续期请联系管理员。")
            sent += 1 if ok else 0
        return sent

    async def broadcast_rankings(self, chat_id: str, days: int = 1) -> bool:
        """Daily ranking post, for a group or channel."""
        if not chat_id or not self.enabled:
            return False
        return await self.send(chat_id, self._rankings_text(days))

    async def audit_group_membership(self) -> dict[str, Any]:
        """Which linked members have left the required group.

        Reported, never enforced: someone who left a chat has not necessarily
        stopped paying, and silently suspending them would be the panel making
        a call that belongs to a person.
        """
        chat = str(self._cfg().get("require_group") or "").strip()
        if not chat or not self.enabled:
            return {"checked": 0, "left": [], "unavailable": True}
        left: list[dict[str, Any]] = []
        checked = 0
        # linked_telegram(), not list(): the latter caps at 500 rows, so a
        # larger install would silently skip everyone past the cap and still
        # report "all present". An audit that under-reports is worse than none,
        # because it is believed.
        for member in self._members.linked_telegram():
            tg_id = member.get("tg_user_id")
            if not tg_id:
                continue
            checked += 1
            allowed, status = await self.in_required_group(str(tg_id))
            if not allowed:
                left.append({
                    "emby_user_id": member.get("emby_user_id"),
                    "username": member.get("username"),
                    "tg_user_id": tg_id,
                    "status": status,
                })
        return {"checked": checked, "left": left, "unavailable": False}
