"""Members — the link between an Emby account and a plan.

The single most important rule in this file: **an Emby user with no member row
is invisible to the panel.**  This server has hundreds of accounts created long
before the panel existed.  If enforcement iterated over Emby's user list rather
than over member rows, one bad deploy could disable all of them at once.  So
membership is opt-in, and every enforcement query starts from `members`.

The second rule: state is derived, never guessed.  `status` is stored, but
`effective_state()` recomputes it from expiry and quota every time it is asked,
so a member whose plan changed or whose period rolled over is correct
immediately rather than at the next sampler tick.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules.plans import PlanService, needs_duration, needs_traffic

# active    : normal
# suspended : operator disabled by hand; never auto-cleared
# expired   : past expires_at
# exhausted : traffic quota consumed
# pending   : created but not yet activated (invite redeemed, awaiting payment)
MEMBER_STATES = ("active", "suspended", "expired", "exhausted", "pending")

# Operator-set states that automatic enforcement must never overwrite. Losing
# this distinction would mean a suspended user silently comes back the moment
# their quota resets.
MANUAL_STATES = ("suspended", "pending")


def period_start(period: str, now: int) -> int:
    """Start of the current accounting window, in epoch seconds (UTC).

    Anchored to calendar boundaries rather than to signup date so that "monthly
    500 GiB" means the same window for every member -- otherwise two users on
    the same plan get quota resets on different days and support becomes
    guesswork.
    """
    dt = datetime.fromtimestamp(now, UTC)
    if period == "daily":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        start = (dt - timedelta(days=dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # total -- never rolls over
        return 0
    return int(start.timestamp())


class MemberService:
    def __init__(self, db: Database, plans: PlanService) -> None:
        self._db = db
        self._plans = plans

    # -- read ----------------------------------------------------------------
    def get(self, user_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM members WHERE emby_user_id=?", (user_id,))
        return self._decorate(row) if row else None

    def list(self, status: str | None = None, plan_id: str | None = None,
             search: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM members"
        clauses, params = [], []
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        if search:
            clauses.append("(username LIKE ? OR note LIKE ? OR contact LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY username COLLATE NOCASE ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        rows = [self._decorate(r) for r in self._db.query(sql, tuple(params))]
        # Status is filtered after decoration because the *effective* state can
        # differ from the stored one (expiry/quota are time-dependent).
        if status:
            rows = [r for r in rows if r["state"] == status]
        return rows

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        plan = self._plans.get(out["plan_id"]) if out.get("plan_id") else None
        out["plan"] = plan
        out["plan_name"] = plan["name"] if plan else "(无套餐)"
        state, reason = self.effective_state(out, plan)
        out["state"] = state
        out["state_reason"] = reason

        quota = plan["traffic_quota_bytes"] if plan else 0
        used = int(out.get("traffic_used_bytes") or 0)
        out["traffic_quota_bytes"] = quota
        out["traffic_remaining_bytes"] = max(0, quota - used) if quota else None
        out["traffic_percent"] = round(used / quota * 100, 1) if quota else None

        expires = out.get("expires_at")
        out["days_remaining"] = (
            max(0, int((expires - time.time()) // 86400)) if expires else None)
        out["device_count"] = self._db.one(
            "SELECT COUNT(*) AS n FROM devices WHERE emby_user_id=? AND blocked=0",
            (out["emby_user_id"],))["n"]
        return out

    @staticmethod
    def effective_state(member: dict[str, Any], plan: dict[str, Any] | None,
                        now: int | None = None) -> tuple[str, str]:
        """Recompute state from the facts, not from the stored label."""
        now = now or int(time.time())
        stored = str(member.get("status") or "active")
        if stored in MANUAL_STATES:
            return stored, "管理员手动设置"
        if not plan:
            return "active", "未分配套餐，不做限制"

        expires = member.get("expires_at")
        if needs_duration(plan["billing_type"]) and expires and now >= expires:
            return "expired", "已过期"

        quota = int(plan.get("traffic_quota_bytes") or 0)
        used = int(member.get("traffic_used_bytes") or 0)
        if needs_traffic(plan["billing_type"]) and quota and used >= quota:
            return "exhausted", "流量已用尽"
        return "active", "正常"

    # -- write ---------------------------------------------------------------
    def upsert(self, user_id: str, username: str, payload: dict[str, Any],
               actor: str = "system") -> dict[str, Any]:
        if not user_id:
            raise ConfigError("缺少 Emby 用户 ID")
        now = int(time.time())
        existing = self._db.one(
            "SELECT * FROM members WHERE emby_user_id=?", (user_id,))

        plan_id = payload.get("plan_id", existing["plan_id"] if existing else None)
        plan = None
        if plan_id:
            plan = self._plans.get(str(plan_id))
            if not plan:
                raise ConfigError(f"套餐不存在: {plan_id}")

        status = str(payload.get(
            "status", existing["status"] if existing else "active"))
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")

        # Expiry: explicit value wins; otherwise derive from the plan when the
        # member is new or the plan changed, so assigning a 30-day plan does
        # not silently leave a member with no end date.
        expires_at = payload.get("expires_at", "__keep__")
        if expires_at == "__keep__":
            expires_at = existing["expires_at"] if existing else None
            plan_changed = bool(existing) and existing["plan_id"] != plan_id
            if plan and needs_duration(plan["billing_type"]) and (
                    not existing or plan_changed or not expires_at):
                expires_at = now + plan["duration_days"] * 86400
            if plan and not needs_duration(plan["billing_type"]):
                expires_at = None
        elif expires_at is not None:
            expires_at = int(expires_at)

        period = plan["traffic_period"] if plan else "monthly"
        p_start = existing["traffic_period_start"] if existing else 0
        if not p_start:
            p_start = period_start(period, now)
        used = int(payload.get(
            "traffic_used_bytes",
            existing["traffic_used_bytes"] if existing else 0))

        row = (
            username or (existing["username"] if existing else ""),
            plan_id,
            status,
            expires_at,
            max(0, used),
            p_start,
            str(payload.get("note", existing["note"] if existing else ""))[:500],
            str(payload.get("contact", existing["contact"] if existing else ""))[:200],
            now,
        )
        if existing:
            self._db.execute(
                "UPDATE members SET username=?,plan_id=?,status=?,expires_at=?,"
                "traffic_used_bytes=?,traffic_period_start=?,note=?,contact=?,"
                "updated_at=? WHERE emby_user_id=?", row + (user_id,))
            action = "member.update"
        else:
            self._db.execute(
                "INSERT INTO members (username,plan_id,status,expires_at,"
                "traffic_used_bytes,traffic_period_start,note,contact,updated_at,"
                "emby_user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row + (user_id, now))
            action = "member.create"

        self.audit(actor, action, user_id,
                   f"plan={plan_id} status={status} expires={expires_at}")
        return self.get(user_id)  # type: ignore[return-value]

    def delete(self, user_id: str, actor: str = "system") -> bool:
        if not self._db.one("SELECT 1 AS x FROM members WHERE emby_user_id=?", (user_id,)):
            raise KeyError(user_id)
        # Removing membership stops enforcement but must not touch the Emby
        # account: the operator may simply want it unmanaged again.
        self._db.execute("DELETE FROM members WHERE emby_user_id=?", (user_id,))
        self._db.execute("DELETE FROM devices WHERE emby_user_id=?", (user_id,))
        self.audit(actor, "member.delete", user_id, "membership removed; Emby account untouched")
        return True

    def register_device(self, user_id: str, device_id: str, *,
                        device_name: str = "", client: str = "",
                        app_version: str = "", last_ip: str = "",
                        now: int | None = None) -> bool:
        """Record a device, refusing new ones once the plan's cap is hit.

        Existing devices always refresh: kicking someone off a phone they
        already use because they opened a second app on it would be wrong.
        The cap only applies to *new* device ids.
        """
        if not device_id:
            return True
        now = now or int(time.time())
        existing = self._db.one(
            "SELECT 1 AS x FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id))
        if existing:
            self._db.execute(
                "UPDATE devices SET device_name=?,client=?,app_version=?,"
                "last_ip=?,last_seen_at=? WHERE emby_user_id=? AND device_id=?",
                (device_name, client, app_version, last_ip, now, user_id, device_id))
            return True

        member = self.get(user_id)
        plan = (member or {}).get("plan") or {}
        cap = int(plan.get("max_devices") or 0)
        if cap > 0:
            count = self._db.one(
                "SELECT COUNT(*) AS n FROM devices WHERE emby_user_id=? AND blocked=0",
                (user_id,))["n"]
            if count >= cap:
                self.audit("system", "device.refused", user_id,
                           f"device={device_id} cap={cap}", ok=False)
                return False

        self._db.execute(
            "INSERT INTO devices (emby_user_id,device_id,device_name,client,"
            "app_version,last_ip,first_seen_at,last_seen_at,blocked) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (user_id, device_id, device_name, client, app_version, last_ip, now, now))
        return True

    # -- lifecycle actions ---------------------------------------------------
    def renew(self, user_id: str, days: int | None = None,
              actor: str = "operator") -> dict[str, Any]:
        """Extend the term. Extends from the later of now and current expiry, so
        renewing early never costs the member the days they already paid for."""
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        plan = member.get("plan")
        if not plan:
            raise ConfigError("该用户未分配套餐，无法续期")
        add_days = int(days if days is not None else plan["duration_days"])
        if add_days <= 0:
            raise ConfigError("续期天数必须大于 0")
        now = int(time.time())
        base = max(now, int(member.get("expires_at") or now))
        new_expiry = base + add_days * 86400
        self._db.execute(
            "UPDATE members SET expires_at=?,status=CASE WHEN status IN "
            "('expired','exhausted') THEN 'active' ELSE status END,updated_at=? "
            "WHERE emby_user_id=?", (new_expiry, now, user_id))
        self.audit(actor, "member.renew", user_id, f"+{add_days}d -> {new_expiry}")
        return self.get(user_id)  # type: ignore[return-value]

    def reset_traffic(self, user_id: str, actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        now = int(time.time())
        plan = member.get("plan")
        period = plan["traffic_period"] if plan else "monthly"
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
            "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
            "updated_at=? WHERE emby_user_id=?",
            (period_start(period, now), now, user_id))
        self.audit(actor, "member.reset_traffic", user_id, "quota reset")
        return self.get(user_id)  # type: ignore[return-value]

    def set_status(self, user_id: str, status: str, actor: str = "operator"
                   ) -> dict[str, Any]:
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        self._db.execute(
            "UPDATE members SET status=?,updated_at=? WHERE emby_user_id=?",
            (status, int(time.time()), user_id))
        self.audit(actor, "member.status", user_id, f"-> {status}")
        return self.get(user_id)  # type: ignore[return-value]

    # -- periodic maintenance ------------------------------------------------
    def roll_periods(self, now: int | None = None) -> int:
        """Reset quotas whose accounting window has rolled over.

        Runs from the sampler rather than from a request so a member on a daily
        plan is not stuck at "exhausted" until someone opens the panel.
        """
        now = now or int(time.time())
        rolled = 0
        for member in self.list(limit=5000):
            plan = member.get("plan")
            if not plan or not needs_traffic(plan["billing_type"]):
                continue
            period = plan["traffic_period"]
            if period == "total":
                continue
            current = period_start(period, now)
            if int(member.get("traffic_period_start") or 0) >= current:
                continue
            self._db.execute(
                "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
                "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
                "updated_at=? WHERE emby_user_id=?",
                (current, now, member["emby_user_id"]))
            self.audit("system", "member.period_roll", member["emby_user_id"],
                       f"period={period}")
            rolled += 1
        return rolled

    def add_traffic(self, user_id: str, delta_bytes: int) -> None:
        if delta_bytes <= 0:
            return
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=traffic_used_bytes+?,"
            "last_seen_at=?,updated_at=? WHERE emby_user_id=?",
            (int(delta_bytes), int(time.time()), int(time.time()), user_id))

    # -- audit ---------------------------------------------------------------
    def audit(self, actor: str, action: str, subject: str = "",
              detail: str = "", ok: bool = True) -> None:
        self._db.execute(
            "INSERT INTO audit_log (ts,actor,action,subject,detail,ok) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor[:60], action[:60], subject[:80],
             detail[:500], 1 if ok else 0))

    def audit_log(self, limit: int = 100, subject: str | None = None,
                  actor: str | None = None, action: str | None = None
                  ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if action:
            clauses.append("action=?")
            params.append(action)
        sql = "SELECT * FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return self._db.query(sql, tuple(params))
