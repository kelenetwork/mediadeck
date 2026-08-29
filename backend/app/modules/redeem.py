"""Renewal codes — mutate an existing member without a new invite.

Invites create accounts. These codes assume the Emby user already exists and
either switch their plan, extend the term, or add traffic to the current
period. The three kinds are explicit so a code can never silently do two
things at once.

Uses are consumed with an atomic `used_count < max_uses` update so two
concurrent redemptions of a single-use code cannot both succeed.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError, ConflictError, NotConfigured
from app.modules.members import (
    MemberService,
    encode_audit_detail,
    parse_overrides,
)
from app.modules.plans import PlanService, needs_duration

KINDS = ("plan", "extend_days", "add_traffic")
REDEEM_WINDOW_SECONDS = 60
REDEEM_MAX_ATTEMPTS = 8
CODE_BYTES = 16  # token_urlsafe(16) is 22 chars, well above the 16-char floor


def generate_code() -> str:
    """URL-safe token, no ambiguous punctuation besides - and _."""
    return secrets.token_urlsafe(CODE_BYTES)


def generate_batch_id() -> str:
    return "b-" + secrets.token_urlsafe(8)


class RedeemService:
    def __init__(self, db: Database, plans: PlanService, members: MemberService,
                 emby: Any) -> None:
        self._db = db
        self._plans = plans
        self._members = members
        self._emby = emby
        self._rate_lock = threading.Lock()
        self._attempts: dict[tuple[str, str], list[float]] = {}

    # -- rate limit (public redeem) ------------------------------------------
    def check_rate(self, ip: str) -> None:
        """Per-IP, not per-code: guessing many codes from one client is the risk."""
        key = ("public", (ip or "").strip() or "unknown")
        now = time.time()
        with self._rate_lock:
            stamps = [t for t in self._attempts.get(key, []) if now - t < REDEEM_WINDOW_SECONDS]
            if len(stamps) >= REDEEM_MAX_ATTEMPTS:
                self._attempts[key] = stamps
                raise ConflictError("尝试过于频繁，请稍后再试")
            stamps.append(now)
            self._attempts[key] = stamps
            if len(self._attempts) > 4000:
                cutoff = now - REDEEM_WINDOW_SECONDS
                self._attempts = {
                    k: [t for t in v if t > cutoff]
                    for k, v in self._attempts.items()
                    if any(t > cutoff for t in v)
                }

    # -- management ----------------------------------------------------------
    def list(self, batch_id: str | None = None, status: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM redeem_codes"
        params: list[Any] = []
        if batch_id:
            sql += " WHERE batch_id=?"
            params.append(batch_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        now = int(time.time())
        out = []
        for row in self._db.query(sql, tuple(params)):
            item = self._decorate(row, now)
            if status and item["status"] != status:
                continue
            out.append(item)
        return out

    def batches(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT batch_id, kind, COUNT(*) AS n, MIN(created_at) AS created_at, "
            "MAX(note) AS note, SUM(used_count) AS used, SUM(max_uses) AS capacity "
            "FROM redeem_codes GROUP BY batch_id ORDER BY created_at DESC LIMIT 200")
        now = int(time.time())
        out = []
        for r in rows:
            codes = self._db.query(
                "SELECT * FROM redeem_codes WHERE batch_id=?", (r["batch_id"],))
            usable = sum(1 for c in codes if self._decorate(c, now)["status"] == "usable")
            out.append({
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "count": r["n"],
                "created_at": r["created_at"],
                "note": r["note"] or "",
                "used": int(r["used"] or 0),
                "capacity": int(r["capacity"] or 0),
                "usable": usable,
            })
        return out

    def logs(self, code: str | None = None, user_id: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM redeem_log"
        clauses, params = [], []
        if code:
            clauses.append("code=?")
            params.append(code)
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return self._db.query(sql, tuple(params))

    def generate(self, payload: dict[str, Any], actor: str = "operator"
                 ) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip()
        if kind not in KINDS:
            raise ConfigError(f"续费码类型必须是 {'/'.join(KINDS)} 之一")

        count = payload.get("count", 1)
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ConfigError("生成数量必须是整数") from None
        if not 1 <= count <= 200:
            raise ConfigError("一次最多生成 200 个续费码")

        max_uses = payload.get("max_uses", 1)
        try:
            max_uses = int(max_uses)
        except (TypeError, ValueError):
            raise ConfigError("可用次数必须是整数") from None
        if not 1 <= max_uses <= 1000:
            raise ConfigError("可用次数必须在 1–1000 之间")

        valid_days = payload.get("valid_days", 0)
        try:
            valid_days = int(valid_days)
        except (TypeError, ValueError):
            raise ConfigError("有效天数必须是整数") from None
        if not 0 <= valid_days <= 3650:
            raise ConfigError("有效天数必须在 0–3650 之间")

        plan_id = str(payload.get("plan_id") or "").strip() or None
        try:
            extend_days = int(payload.get("extend_days") or 0)
        except (TypeError, ValueError):
            raise ConfigError("延期天数必须是整数") from None
        try:
            add_traffic = int(payload.get("add_traffic_bytes") or 0)
        except (TypeError, ValueError):
            raise ConfigError("额外流量必须是整数") from None
        if kind == "plan":
            if not plan_id or not self._plans.get(plan_id):
                raise ConfigError("套餐续费码必须指定存在的套餐")
            extend_days = 0
            add_traffic = 0
        elif kind == "extend_days":
            if extend_days <= 0:
                raise ConfigError("延期续费码必须设置大于 0 的天数")
            if extend_days > 3650:
                raise ConfigError("延期天数必须在 1–3650 之间")
            plan_id = None
            add_traffic = 0
        else:
            if add_traffic <= 0:
                raise ConfigError("流量续费码必须设置大于 0 的流量")
            if add_traffic > (1 << 60):
                raise ConfigError("额外流量超出范围")
            plan_id = None
            extend_days = 0

        now = int(time.time())
        expires = now + valid_days * 86400 if valid_days else None
        batch_id = str(payload.get("batch_id") or "").strip() or generate_batch_id()
        note = str(payload.get("note") or "")[:200]
        created: list[str] = []
        with self._db.write() as conn:
            for _ in range(count):
                for _attempt in range(12):
                    code = generate_code()
                    if len(code) < 16:
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM redeem_codes WHERE id=?", (code,)).fetchone()
                    if not exists:
                        break
                else:
                    raise ConfigError("生成续费码失败，请重试")
                conn.execute(
                    "INSERT INTO redeem_codes (id,batch_id,kind,plan_id,extend_days,"
                    "add_traffic_bytes,max_uses,used_count,expires_at,created_at,"
                    "created_by,note) VALUES (?,?,?,?,?,?,?,0,?,?,?,?)",
                    (code, batch_id, kind, plan_id, extend_days, add_traffic,
                     max_uses, expires, now, actor[:60], note))
                created.append(code)

        self._members.audit(actor, "redeem.generate", batch_id, encode_audit_detail({
            "kind": {"from": None, "to": kind},
            "count": {"from": 0, "to": count},
            "max_uses": {"from": None, "to": max_uses},
        }))
        now = int(time.time())
        codes = [self._decorate(self._db.one(
            "SELECT * FROM redeem_codes WHERE id=?", (c,)), now) for c in created]
        return {"batch_id": batch_id, "count": len(codes), "codes": codes}

    def delete(self, code: str, actor: str = "operator") -> bool:
        row = self._db.one("SELECT * FROM redeem_codes WHERE id=?", (code,))
        if not row:
            raise KeyError(code)
        if int(row["used_count"] or 0) >= int(row["max_uses"] or 1):
            raise ConflictError("已用完的续费码不能作废")
        self._db.execute("DELETE FROM redeem_codes WHERE id=?", (code,))
        self._members.audit(actor, "redeem.delete", code, encode_audit_detail({
            "code": {"from": code, "to": None},
            "used_count": {"from": row["used_count"], "to": None},
        }))
        return True

    # -- redemption ----------------------------------------------------------
    def _load_usable(self, code: str) -> dict[str, Any]:
        code = (code or "").strip()
        if len(code) < 16:
            raise ConfigError("续费码格式无效")
        row = self._db.one("SELECT * FROM redeem_codes WHERE id=?", (code,))
        if not row:
            raise ConfigError("续费码不存在")
        now = int(time.time())
        if row["expires_at"] and now >= int(row["expires_at"]):
            raise ConfigError("续费码已过期")
        if int(row["used_count"] or 0) >= int(row["max_uses"] or 1):
            raise ConfigError("续费码使用次数已用完")
        if row["kind"] == "plan" and not self._plans.get(row["plan_id"]):
            raise ConfigError("续费码对应的套餐已被删除")
        return dict(row)

    def _consume(self, code: str) -> dict[str, Any]:
        """Increment used_count only if the code is still within its cap."""
        now = int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE redeem_codes SET used_count=used_count+1 "
                "WHERE id=? AND used_count < max_uses "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (code, now))
            if cur.rowcount != 1:
                row = conn.execute(
                    "SELECT * FROM redeem_codes WHERE id=?", (code,)).fetchone()
                if row is None:
                    raise ConfigError("续费码不存在")
                row = dict(row)
                if row["expires_at"] and now >= int(row["expires_at"]):
                    raise ConfigError("续费码已过期")
                raise ConfigError("续费码使用次数已用完")
            row = conn.execute(
                "SELECT * FROM redeem_codes WHERE id=?", (code,)).fetchone()
            return dict(row)

    def _log(self, code: str, user_id: str, actor: str, detail: dict[str, Any],
             ok: bool = True) -> None:
        raw = json.dumps(detail, ensure_ascii=False, default=str)[:2000]
        self._db.execute(
            "INSERT INTO redeem_log (code,user_id,ts,actor,detail) VALUES (?,?,?,?,?)",
            (code, user_id, int(time.time()), actor[:60], raw))
        self._members.audit(
            actor, "redeem.use" if ok else "redeem.fail", user_id,
            encode_audit_detail(detail), ok=ok)

    def redeem(self, code: str, user_id: str, actor: str = "operator"
               ) -> dict[str, Any]:
        member = self._members.get(user_id)
        if not member:
            raise ConfigError("用户未纳入套餐管理，无法兑换")
        self._load_usable(code)
        row = self._consume(code)
        try:
            result = self._apply(row, member, actor)
        except Exception as exc:
            # Consumption already happened; record the failure rather than
            # silently refunding, which would make a flaky apply free to retry
            # past max_uses. Operator can issue a new code.
            self._log(row["id"], user_id, actor, {
                "ok": {"from": True, "to": False},
                "error": {"from": None, "to": str(exc)[:200]},
            }, ok=False)
            raise
        self._log(row["id"], user_id, actor, result["diff"])
        return {
            "ok": True,
            "code": row["id"],
            "kind": row["kind"],
            "member": self._members.get(user_id),
            "result": result["summary"],
        }

    async def redeem_public(self, code: str, username: str, password: str,
                            actor: str = "public") -> dict[str, Any]:
        username = (username or "").strip()
        if not username or not password:
            raise ConfigError("请填写用户名和密码")
        try:
            user = await self._emby.authenticate_user(username, password)
        except NotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"无法验证账号: {exc}") from None
        if not user or not user.get("Id"):
            raise ConfigError("用户名或密码错误")
        return self.redeem(code, str(user["Id"]), actor=actor)

    def _apply(self, row: dict[str, Any], member: dict[str, Any], actor: str
               ) -> dict[str, Any]:
        kind = row["kind"]
        user_id = member["emby_user_id"]
        now = int(time.time())
        if kind == "plan":
            plan = self._plans.get(row["plan_id"])
            if not plan:
                raise ConfigError("续费码对应的套餐已被删除")
            before_plan = member.get("plan_id")
            before_exp = member.get("expires_at")
            if needs_duration(plan["billing_type"]):
                days = int(plan["duration_days"] or 0)
                current = int(before_exp) if before_exp else 0
                base = max(now, current)
                new_expiry = base + days * 86400
            else:
                new_expiry = None
            ov = parse_overrides(member.get("overrides") or {})
            ov.pop("expires_at_override", None)
            self._db.execute(
                "UPDATE members SET plan_id=?,expires_at=?,overrides_json=?,"
                "status=CASE WHEN status IN ('expired','exhausted') THEN 'active' "
                "ELSE status END,updated_at=? WHERE emby_user_id=?",
                (plan["id"], new_expiry,
                 json.dumps(ov, ensure_ascii=False, sort_keys=True),
                 now, user_id))
            diff = {
                "plan_id": {"from": before_plan, "to": plan["id"]},
                "expires_at": {"from": before_exp, "to": new_expiry},
            }
            return {"diff": diff, "summary": {"kind": "plan", "plan_id": plan["id"],
                                              "expires_at": new_expiry}}

        if kind == "extend_days":
            days = int(row["extend_days"] or 0)
            if days <= 0:
                raise ConfigError("延期天数无效")
            before_exp = member.get("expires_at")
            current = int(before_exp) if before_exp else 0
            base = max(now, current)
            new_expiry = base + days * 86400
            ov = parse_overrides(member.get("overrides") or {})
            ov.pop("expires_at_override", None)
            self._db.execute(
                "UPDATE members SET expires_at=?,overrides_json=?,"
                "status=CASE WHEN status IN ('expired','exhausted') THEN 'active' "
                "ELSE status END,updated_at=? WHERE emby_user_id=?",
                (new_expiry, json.dumps(ov, ensure_ascii=False, sort_keys=True),
                 now, user_id))
            diff = {
                "expires_at": {"from": before_exp, "to": new_expiry},
                "days": {"from": None, "to": days},
            }
            return {"diff": diff, "summary": {"kind": "extend_days", "days": days,
                                              "expires_at": new_expiry}}

        extra = int(row["add_traffic_bytes"] or 0)
        if extra <= 0:
            raise ConfigError("额外流量无效")
        ov = parse_overrides(member.get("overrides") or {})
        before = int(ov.get("extra_traffic_bytes") or 0)
        ov["extra_traffic_bytes"] = before + extra
        self._members.set_overrides(user_id, ov, actor=actor)
        diff = {"extra_traffic_bytes": {"from": before, "to": before + extra}}
        return {"diff": diff, "summary": {"kind": "add_traffic", "added": extra}}

    @staticmethod
    def _decorate(row: dict[str, Any] | None, now: int) -> dict[str, Any]:
        if not row:
            return {}
        item = dict(row)
        expired = bool(item.get("expires_at") and now >= int(item["expires_at"]))
        exhausted = int(item.get("used_count") or 0) >= int(item.get("max_uses") or 1)
        if expired:
            status = "expired"
        elif exhausted:
            status = "exhausted"
        else:
            status = "usable"
        item["expired"] = expired
        item["exhausted"] = exhausted
        item["status"] = status
        return item
