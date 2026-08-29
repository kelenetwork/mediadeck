"""Invitation codes — how a member gets created without manual account work.

Without this, adding a user means the operator creates an Emby account, picks a
password, sends it over chat, and then remembers to enrol them in the panel.
Every one of those steps is a chance to hand out an unmetered account by
accident.

Redeeming a code does all of it atomically: create the Emby user, set their
password, enrol them on the code's plan, and apply the plan's limits.  If any
step fails the Emby account is removed again, because a half-created user with
no plan is exactly the unmetered account this exists to prevent.
"""
from __future__ import annotations

import re
import secrets
import string
import threading
import time
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError, ConflictError
from app.modules.members import MemberService
from app.modules.plans import PlanService

# Unambiguous alphabet: no O/0, I/1/l. Codes get read aloud and retyped.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z0-9-]{6,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{1,48}$")
MIN_PASSWORD = 6
# Brute-force guessing of invite codes: N attempts per (code, IP) window.
REDEEM_WINDOW_SECONDS = 60
REDEEM_MAX_ATTEMPTS = 8


def generate_code(groups: int = 3, size: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(size))
        for _ in range(groups)
    )


class InviteService:
    def __init__(self, db: Database, plans: PlanService, members: MemberService,
                 emby: Any) -> None:
        self._db = db
        self._plans = plans
        self._members = members
        self._emby = emby
        self._rate_lock = threading.Lock()
        # (code, ip) -> list[epoch]
        self._attempts: dict[tuple[str, str], list[float]] = {}

    # -- management ----------------------------------------------------------
    def list(self, include_used: bool = True) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM invites ORDER BY created_at DESC LIMIT 500")
        now = int(time.time())
        out = []
        for r in rows:
            r = dict(r)
            r["expired"] = bool(r.get("expires_at") and now >= r["expires_at"])
            r["exhausted"] = r["used_count"] >= r["max_uses"]
            r["usable"] = not (r["revoked"] or r["expired"] or r["exhausted"])
            plan = self._plans.get(r["plan_id"])
            r["plan_name"] = plan["name"] if plan else "(套餐已删除)"
            if include_used or r["usable"]:
                out.append(r)
        return out

    def create(self, plan_id: str, max_uses: int = 1, valid_days: int = 7,
               note: str = "", actor: str = "operator", count: int = 1
               ) -> list[dict[str, Any]]:
        if not self._plans.get(plan_id):
            raise ConfigError(f"套餐不存在: {plan_id}")
        max_uses = int(max_uses)
        if not 1 <= max_uses <= 1000:
            raise ConfigError("可用次数必须在 1–1000 之间")
        valid_days = int(valid_days)
        if not 0 <= valid_days <= 3650:
            raise ConfigError("有效天数必须在 0–3650 之间")
        count = int(count)
        if not 1 <= count <= 100:
            raise ConfigError("一次最多生成 100 个邀请码")

        now = int(time.time())
        expires = now + valid_days * 86400 if valid_days else None
        created = []
        with self._db.write() as conn:
            for _ in range(count):
                # Retry on collision rather than trusting randomness blindly:
                # a duplicate primary key would abort the whole batch.
                for _attempt in range(10):
                    code = generate_code()
                    exists = conn.execute(
                        "SELECT 1 FROM invites WHERE code=?", (code,)).fetchone()
                    if not exists:
                        break
                else:
                    raise ConfigError("生成邀请码失败，请重试")
                conn.execute(
                    "INSERT INTO invites (code,plan_id,created_at,expires_at,"
                    "max_uses,used_count,note,created_by,revoked) "
                    "VALUES (?,?,?,?,?,0,?,?,0)",
                    (code, plan_id, now, expires, max_uses, note[:200], actor[:60]))
                created.append(code)
        self._members.audit(actor, "invite.create", plan_id,
                            f"{count} code(s), uses={max_uses}, days={valid_days}")
        return [r for r in self.list() if r["code"] in created]

    def revoke(self, code: str, actor: str = "operator") -> bool:
        row = self._db.one("SELECT 1 AS x FROM invites WHERE code=?", (code,))
        if not row:
            raise KeyError(code)
        self._db.execute("UPDATE invites SET revoked=1 WHERE code=?", (code,))
        self._members.audit(actor, "invite.revoke", code)
        return True

    def delete(self, code: str, actor: str = "operator") -> bool:
        if not self._db.one("SELECT 1 AS x FROM invites WHERE code=?", (code,)):
            raise KeyError(code)
        self._db.execute("DELETE FROM invites WHERE code=?", (code,))
        self._members.audit(actor, "invite.delete", code)
        return True

    # -- redemption ----------------------------------------------------------
    def check_rate(self, code: str, ip: str) -> None:
        """Refuse a burst of guesses against one code from one client."""
        key = ((code or "").strip().upper(), (ip or "").strip() or "unknown")
        now = time.time()
        with self._rate_lock:
            stamps = [t for t in self._attempts.get(key, []) if now - t < REDEEM_WINDOW_SECONDS]
            if len(stamps) >= REDEEM_MAX_ATTEMPTS:
                self._attempts[key] = stamps
                raise ConflictError("尝试过于频繁，请稍后再试")
            stamps.append(now)
            self._attempts[key] = stamps
            # Bound the map so a scanner cannot grow it forever.
            if len(self._attempts) > 4000:
                cutoff = now - REDEEM_WINDOW_SECONDS
                self._attempts = {
                    k: [t for t in v if t > cutoff]
                    for k, v in self._attempts.items()
                    if any(t > cutoff for t in v)
                }

    def _validate_code(self, code: str) -> dict[str, Any]:
        code = (code or "").strip().upper()
        if not CODE_RE.match(code):
            raise ConfigError("邀请码格式无效")
        row = self._db.one("SELECT * FROM invites WHERE code=?", (code,))
        if not row:
            raise ConfigError("邀请码不存在")
        if row["revoked"]:
            raise ConfigError("邀请码已作废")
        if row["expires_at"] and int(time.time()) >= row["expires_at"]:
            raise ConfigError("邀请码已过期")
        if row["used_count"] >= row["max_uses"]:
            raise ConfigError("邀请码使用次数已用完")
        if not self._plans.get(row["plan_id"]):
            raise ConfigError("邀请码对应的套餐已被删除")
        return row

    def preview(self, code: str) -> dict[str, Any]:
        """What a code grants, without consuming it."""
        row = self._validate_code(code)
        plan = self._plans.get(row["plan_id"]) or {}
        return {
            "code": row["code"],
            "plan": {
                "name": plan.get("name"),
                "billing_type": plan.get("billing_type"),
                "traffic_quota_bytes": plan.get("traffic_quota_bytes"),
                "traffic_period": plan.get("traffic_period"),
                "duration_days": plan.get("duration_days"),
                "max_streams": plan.get("max_streams"),
                "max_devices": plan.get("max_devices"),
                "allow_transcode": plan.get("allow_transcode"),
                "allow_download": plan.get("allow_download"),
            },
            "remaining_uses": row["max_uses"] - row["used_count"],
        }

    async def redeem(self, code: str, username: str, password: str,
                     enforcement: Any = None, actor: str = "invite"
                     ) -> dict[str, Any]:
        row = self._validate_code(code)
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise ConfigError("用户名只能包含字母、数字、点、下划线、@ 和连字符（2–49 字符）")
        if len(password or "") < MIN_PASSWORD:
            raise ConfigError(f"密码至少 {MIN_PASSWORD} 位")

        try:
            existing = {u["Name"].lower() for u in await self._emby.list_users()}
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"无法连接 Emby: {exc}") from None
        if username.lower() in existing:
            raise ConfigError("用户名已存在")

        created = await self._emby.create_user(username)
        user_id = str(created.get("Id") or "")
        if not user_id:
            raise ConfigError("Emby 未返回用户 ID")

        # From here on, any failure must not leave an Emby account behind that
        # nobody is metering.
        try:
            await self._emby.set_user_password(user_id, password)
            self._members.upsert(user_id, username,
                                 {"plan_id": row["plan_id"], "status": "active"},
                                 actor=actor)
            self._db.execute(
                "UPDATE invites SET used_count=used_count+1 WHERE code=?",
                (row["code"],))
            if enforcement:
                await enforcement.enforce_now(user_id, "invite redeemed")
        except Exception as exc:  # noqa: BLE001
            with_cleanup = False
            try:
                with_cleanup = await self._emby.delete_user(user_id)
            except Exception:  # noqa: BLE001
                with_cleanup = False
            self._members.audit(
                actor, "invite.redeem_failed", row["code"],
                f"user={username} rolled_back={with_cleanup} err={str(exc)[:200]}",
                ok=False)
            raise ConfigError(f"开通失败，已回滚: {exc}") from None

        self._members.audit(actor, "invite.redeem", user_id,
                            f"code={row['code']} plan={row['plan_id']} user={username}")
        return {
            "ok": True,
            "user_id": user_id,
            "username": username,
            "plan_id": row["plan_id"],
        }


def random_password(length: int = 12) -> str:
    """For operator-created accounts, so nobody reuses '123456'."""
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))
