"""Telegram bot: menu-driven, and aware of who it is talking to.

Two audiences share one entry point. Someone who has not linked an account yet
can only be offered a way to link one; a linked member should land straight on
their own status. Showing both groups the same wall of buttons means half of
them are dead ends, so the menu is chosen from the binding state on every
render rather than being a fixed keyboard.

Binding uses a short-lived one-time code issued from the panel. The bot never
asks for an Emby password: a chat transcript is not a safe place to type one,
and a code that expires limits the damage if it is pasted in the wrong window.

Polling, not webhooks. A webhook needs a public HTTPS route into the panel;
long polling reaches out instead, so the panel stays reachable only from where
it already was.
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"

# A bind code is typed by hand on a phone, so it trades entropy for legibility
# and buys the difference back with a short lifetime and single use.
BIND_CODE_TTL = 600.0
BIND_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0, I/1
BIND_CODE_LEN = 6

# Telegram closes an idle long poll itself; this only has to be shorter than
# the client timeout so a hung socket is noticed rather than waited on forever.
POLL_TIMEOUT = 25
HTTP_TIMEOUT = POLL_TIMEOUT + 10


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
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class BindCodes:
    """One-time codes linking a panel member to whoever redeems them.

    Kept in memory on purpose: these live for minutes, and a code that does not
    survive a restart is a code that cannot be redeemed by someone who found it
    in an old chat.
    """

    def __init__(self, ttl: float = BIND_CODE_TTL) -> None:
        self._ttl = ttl
        self._codes: dict[str, tuple[str, str, float]] = {}

    def issue(self, user_id: str, username: str) -> tuple[str, int]:
        self._sweep()
        # Re-issuing replaces the previous code for that member, so a forgotten
        # code cannot be redeemed later by someone else.
        for code, (uid, _, _) in list(self._codes.items()):
            if uid == user_id:
                self._codes.pop(code, None)
        code = "".join(secrets.choice(BIND_ALPHABET) for _ in range(BIND_CODE_LEN))
        self._codes[code] = (user_id, username, time.time() + self._ttl)
        return code, int(self._ttl)

    def redeem(self, code: str) -> tuple[str, str] | None:
        self._sweep()
        entry = self._codes.pop((code or "").strip().upper(), None)
        if not entry:
            return None
        user_id, username, _ = entry
        return user_id, username

    def _sweep(self) -> None:
        now = time.time()
        for code, (_, _, expires) in list(self._codes.items()):
            if expires <= now:
                self._codes.pop(code, None)

    def pending(self) -> int:
        self._sweep()
        return len(self._codes)


class TelegramBot:
    """Long-polling bot bound to the panel's member records."""

    def __init__(self, config_provider: Any, members: Any) -> None:
        self._config = config_provider
        self._members = members
        self.codes = BindCodes()
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._last_error = ""
        self._last_poll_at = 0.0
        self._started_at = 0.0

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
            "pending_bind_codes": self.codes.pending(),
            "started_at": int(self._started_at) or None,
        }

    # -- transport ------------------------------------------------------------

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    timeout: float = 20) -> dict[str, Any] | None:
        token = self._token()
        if not token:
            return None
        url = f"{API_ROOT}/bot{token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload or {})
            body = r.json()
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            # The token is in the URL, so the exception text is not safe to keep.
            self._last_error = f"{type(exc).__name__}: 请求失败"
            return None
        if not body.get("ok"):
            self._last_error = str(body.get("description") or "Telegram 拒绝了请求")
            return None
        self._last_error = ""
        return body.get("result")

    async def verify(self) -> dict[str, Any]:
        """Check the token by asking who the bot is. Never echoes the token."""
        me = await self._call("getMe", timeout=15)
        if not me:
            return {"ok": False, "error": self._last_error or "无法连接 Telegram"}
        return {
            "ok": True,
            "username": me.get("username", ""),
            "name": me.get("first_name", ""),
            "id": me.get("id"),
        }

    async def send(self, chat_id: str | int, text: str,
                   keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
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

    # -- menus ----------------------------------------------------------------

    @staticmethod
    def guest_menu() -> list[list[dict[str, str]]]:
        """Someone with no linked account can only usefully do one thing."""
        return [
            [{"text": "🔗 绑定账号", "callback_data": "bind"}],
            [{"text": "❓ 使用说明", "callback_data": "help"},
             {"text": "📮 联系管理员", "callback_data": "contact"}],
        ]

    @staticmethod
    def member_menu() -> list[list[dict[str, str]]]:
        return [
            [{"text": "👤 我的账号", "callback_data": "me"},
             {"text": "⏳ 有效期", "callback_data": "expiry"}],
            [{"text": "📺 我的设备", "callback_data": "devices"},
             {"text": "📊 观看统计", "callback_data": "usage"}],
            [{"text": "🔄 刷新", "callback_data": "home"},
             {"text": "🚫 解绑", "callback_data": "unbind"}],
        ]

    def _member_for_chat(self, tg_user_id: str) -> dict[str, Any] | None:
        return self._members.find_by_telegram(str(tg_user_id))

    def _home(self, tg_user_id: str, tg_name: str) -> tuple[str, list[list[dict[str, str]]]]:
        member = self._member_for_chat(tg_user_id)
        if not member:
            body = (
                f"👋 你好，{tg_name}\n\n"
                "这个账号还没有绑定影视库成员。\n"
                "绑定后可以查询有效期、设备和观看统计。\n\n"
                "<i>绑定码请向管理员索取。</i>"
            )
            return body, self.guest_menu()
        body = (
            f"🎬 <b>{member.get('username') or '成员'}</b>\n"
            f"状态：{self._status_label(member)}\n"
            f"有效期：{_fmt_expiry(member.get('expires_at'))}\n\n"
            "选择要查看的内容："
        )
        return body, self.member_menu()

    @staticmethod
    def _status_label(member: dict[str, Any]) -> str:
        return {
            "active": "✅ 正常", "suspended": "⛔ 已停用",
            "expired": "⌛ 已过期", "exhausted": "📵 已超额",
            "pending": "🕓 待开通",
        }.get(str(member.get("status") or ""), str(member.get("status") or "未知"))

    # -- update handling ------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        from_user = message.get("from") or {}
        tg_user_id = str(from_user.get("id") or "")
        tg_name = from_user.get("first_name") or from_user.get("username") or "朋友"
        text = str(message.get("text") or "").strip()
        if not chat_id or not tg_user_id:
            return

        # A bare code is the common case during binding, so accept it without
        # requiring a command prefix that a phone keyboard makes awkward.
        if text and not text.startswith("/") and self._looks_like_code(text):
            await self._try_bind(chat_id, tg_user_id, from_user, text)
            return

        body, keyboard = self._home(tg_user_id, tg_name)
        await self.send(chat_id, body, keyboard)

    @staticmethod
    def _looks_like_code(text: str) -> bool:
        candidate = text.strip().upper()
        return (len(candidate) == BIND_CODE_LEN
                and all(ch in BIND_ALPHABET for ch in candidate))

    async def _try_bind(self, chat_id: Any, tg_user_id: str,
                        from_user: dict[str, Any], code: str) -> None:
        if self._member_for_chat(tg_user_id):
            await self.send(chat_id, "这个 Telegram 已经绑定过账号了，先解绑再重新绑定。",
                            self.member_menu())
            return
        redeemed = self.codes.redeem(code)
        if not redeemed:
            await self.send(chat_id, "❌ 绑定码无效或已过期，请向管理员重新索取。",
                            self.guest_menu())
            return
        user_id, username = redeemed
        self._members.bind_telegram(
            user_id, tg_user_id,
            str(from_user.get("username") or ""), actor="telegram")
        await self.send(
            chat_id,
            f"✅ 已绑定到 <b>{username}</b>\n\n以后可以直接在这里查询账号状态。",
            self.member_menu())

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

        member = self._member_for_chat(tg_user_id)

        # Every branch below re-reads the binding state: a member could have
        # been unbound from the panel while this keyboard sat on their screen.
        if data == "bind":
            await self._edit(chat_id,
                             message_id,
                             "🔗 <b>绑定账号</b>\n\n"
                             "向管理员索取 6 位绑定码，然后直接把它发到这个对话里。\n"
                             "<i>绑定码 10 分钟内有效，且只能使用一次。</i>",
                             self.guest_menu())
            return
        if data == "help":
            await self._edit(chat_id, message_id,
                             "❓ <b>使用说明</b>\n\n"
                             "绑定后可以查询：账号状态、有效期、在用设备、观看统计。\n"
                             "有效期临近时会主动提醒你。\n\n"
                             "遇到问题请联系管理员。",
                             self.guest_menu() if not member else self.member_menu())
            return
        if data == "contact":
            await self._edit(chat_id, message_id,
                             "📮 <b>联系管理员</b>\n\n请直接联系邀请你的人。",
                             self.guest_menu())
            return
        if data == "home":
            body, keyboard = self._home(tg_user_id, tg_name)
            await self._edit(chat_id, message_id, body, keyboard)
            return

        if not member:
            await self._edit(chat_id, message_id,
                             "这个 Telegram 还没有绑定账号。", self.guest_menu())
            return

        if data == "me":
            await self._edit(
                chat_id, message_id,
                f"👤 <b>{member.get('username') or '-'}</b>\n\n"
                f"状态：{self._status_label(member)}\n"
                f"用户组：{member.get('group_name') or member.get('group_id') or '默认'}\n"
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
                lines = []
                for d in devices[:8]:
                    seen = d.get("last_seen_at")
                    when = time.strftime("%m-%d %H:%M", time.localtime(seen)) if seen else "—"
                    flag = "🚫 " if d.get("blocked") else ""
                    lines.append(f"{flag}{d.get('device_name') or d.get('device_id')} · {when}")
                text = "📺 <b>我的设备</b>\n\n" + "\n".join(lines)
            await self._edit(chat_id, message_id, text, self.member_menu())
            return
        if data == "usage":
            used = member.get("traffic_used_bytes") or 0
            await self._edit(
                chat_id, message_id,
                f"📊 <b>观看统计</b>\n\n本周期用量：{_fmt_bytes(used)}\n"
                f"最近活跃：{self._fmt_seen(member.get('last_seen_at'))}",
                self.member_menu())
            return
        if data == "unbind":
            self._members.unbind_telegram(
                str(member.get("emby_user_id")), actor="telegram")
            await self._edit(chat_id, message_id,
                             "已解绑。需要时可以用新的绑定码重新绑定。",
                             self.guest_menu())
            return

    @staticmethod
    def _fmt_seen(ts: int | None) -> str:
        if not ts:
            return "—"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

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
        """Warn linked members whose access ends soon. Unlinked ones are skipped."""
        sent = 0
        for member in members:
            if not member.get("tg_user_id"):
                continue
            ok = await self.notify_member(
                member,
                f"⏳ <b>有效期提醒</b>\n\n"
                f"账号 <b>{member.get('username') or '-'}</b> "
                f"{_fmt_expiry(member.get('expires_at'))}。\n"
                "需要续期请联系管理员。")
            sent += 1 if ok else 0
        return sent
