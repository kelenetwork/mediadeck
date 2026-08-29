"""Plans — the templates that define what a member may do.

A plan answers three separate questions, and keeping them separate is what
makes the rest of the system tractable:

1. **How is access paid for?**  (billing_type)
2. **What are the hard limits?** (streams, bitrate, devices, libraries)
3. **What does it cost?**       (price, currency)

Billing types are deliberately explicit rather than "quota=0 means unlimited"
style overloading, because an operator reading a plan list must be able to see
at a glance whether an account can expire.  A silent 0 that means "infinite" is
exactly how people accidentally give away permanent accounts.

Nothing here touches Emby.  A plan is data; enforcement.py is what makes it
real.
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError, ConflictError

# unlimited      : never expires, no traffic ceiling (staff, family)
# traffic        : capped bytes per period, no end date
# duration       : ends on a date, unmetered until then
# traffic_duration: both -- whichever runs out first stops access
BILLING_TYPES = ("unlimited", "traffic", "duration", "traffic_duration")
TRAFFIC_PERIODS = ("daily", "weekly", "monthly", "total")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
GIB = 1024 ** 3


def needs_traffic(billing_type: str) -> bool:
    return billing_type in ("traffic", "traffic_duration")


def needs_duration(billing_type: str) -> bool:
    return billing_type in ("duration", "traffic_duration")


DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "id": "trial", "name": "体验",
        "description": "新用户试用：7 天，50 GiB，单路 1080p",
        "billing_type": "traffic_duration",
        "traffic_quota_bytes": 50 * GIB, "traffic_period": "total",
        "duration_days": 7,
        "max_streams": 1, "max_bitrate_kbps": 8000, "max_devices": 1,
        "allow_transcode": 0, "allow_download": 0, "allow_sync": 0,
        "price_cents": 0, "priority": 10,
    },
    {
        "id": "monthly", "name": "月付",
        "description": "每月 500 GiB，两路并发，允许转码",
        "billing_type": "traffic_duration",
        "traffic_quota_bytes": 500 * GIB, "traffic_period": "monthly",
        "duration_days": 30,
        "max_streams": 2, "max_bitrate_kbps": 0, "max_devices": 3,
        "allow_transcode": 1, "allow_download": 0, "allow_sync": 0,
        "price_cents": 1500, "priority": 20,
    },
    {
        "id": "yearly", "name": "年付",
        "description": "每月 2 TiB，四路并发，允许转码与下载",
        "billing_type": "traffic_duration",
        "traffic_quota_bytes": 2048 * GIB, "traffic_period": "monthly",
        "duration_days": 365,
        "max_streams": 4, "max_bitrate_kbps": 0, "max_devices": 5,
        "allow_transcode": 1, "allow_download": 1, "allow_sync": 1,
        "price_cents": 12000, "priority": 30,
    },
    {
        "id": "staff", "name": "内部",
        "description": "不限流量不过期，供管理员与家人使用",
        "billing_type": "unlimited",
        "traffic_quota_bytes": 0, "traffic_period": "total",
        "duration_days": 0,
        "max_streams": 5, "max_bitrate_kbps": 0, "max_devices": 0,
        "allow_transcode": 1, "allow_download": 1, "allow_sync": 1,
        "price_cents": 0, "priority": 90, "is_default": 1,
    },
]


class PlanService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- seeding -------------------------------------------------------------
    def seed_defaults(self) -> int:
        """Create a starter set once, so the page is never an empty void.

        Only runs when there are no plans at all: an operator who deletes a
        default plan must not have it reappear on the next restart.
        """
        if self._db.one("SELECT COUNT(*) AS n FROM plans")["n"]:
            return 0
        now = int(time.time())
        created = 0
        for spec in DEFAULT_PLANS:
            payload = dict(spec)
            payload.setdefault("is_default", 0)
            payload.setdefault("libraries", [])
            self.create(payload, now=now)
            created += 1
        return created

    # -- validation ----------------------------------------------------------
    @staticmethod
    def _validate(payload: dict[str, Any], existing: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
        base = dict(existing or {})

        plan_id = str(payload.get("id", base.get("id", ""))).strip().lower()
        if not ID_RE.match(plan_id):
            raise ConfigError("套餐 ID 只能是小写字母、数字、下划线或连字符（1–40 字符）")

        name = str(payload.get("name", base.get("name", ""))).strip()
        if not name:
            raise ConfigError("套餐名称不能为空")
        if len(name) > 60:
            raise ConfigError("套餐名称过长（最多 60 字符）")

        billing = str(payload.get("billing_type", base.get("billing_type", "unlimited")))
        if billing not in BILLING_TYPES:
            raise ConfigError(f"计费类型必须是 {'/'.join(BILLING_TYPES)} 之一")

        period = str(payload.get("traffic_period", base.get("traffic_period", "monthly")))
        if period not in TRAFFIC_PERIODS:
            raise ConfigError(f"流量周期必须是 {'/'.join(TRAFFIC_PERIODS)} 之一")

        def as_int(key: str, default: int, label: str, lo: int, hi: int) -> int:
            raw = payload.get(key, base.get(key, default))
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ConfigError(f"{label}必须是整数") from None
            if not lo <= value <= hi:
                raise ConfigError(f"{label}必须在 {lo}–{hi} 之间")
            return value

        quota = as_int("traffic_quota_bytes", 0, "流量配额", 0, 1 << 60)
        days = as_int("duration_days", 0, "有效期天数", 0, 3650)
        streams = as_int("max_streams", 1, "并发路数", 1, 100)
        bitrate = as_int("max_bitrate_kbps", 0, "码率上限", 0, 1_000_000)
        devices = as_int("max_devices", 0, "设备数上限", 0, 100)
        price = as_int("price_cents", 0, "价格", 0, 100_000_000)
        priority = as_int("priority", 0, "排序权重", 0, 1000)

        # A metered plan with no meter is the failure mode that silently hands
        # out unlimited access, so it is rejected rather than defaulted.
        if needs_traffic(billing) and quota <= 0:
            raise ConfigError("流量计费套餐必须设置大于 0 的流量配额")
        if needs_duration(billing) and days <= 0:
            raise ConfigError("到期计费套餐必须设置大于 0 的有效期天数")
        if not needs_traffic(billing):
            quota = 0
        if not needs_duration(billing):
            days = 0

        libraries = payload.get("libraries", None)
        if libraries is None:
            libraries = base.get("libraries", [])
        if not isinstance(libraries, list):
            raise ConfigError("媒体库列表格式错误")
        libraries = [str(x) for x in libraries if str(x).strip()]

        return {
            "id": plan_id,
            "name": name,
            "description": str(payload.get("description", base.get("description", "")))[:500],
            "billing_type": billing,
            "traffic_quota_bytes": quota,
            "traffic_period": period,
            "duration_days": days,
            "max_streams": streams,
            "max_bitrate_kbps": bitrate,
            "max_devices": devices,
            "allow_transcode": 1 if payload.get(
                "allow_transcode", base.get("allow_transcode", 0)) else 0,
            "allow_download": 1 if payload.get(
                "allow_download", base.get("allow_download", 0)) else 0,
            "allow_sync": 1 if payload.get(
                "allow_sync", base.get("allow_sync", 0)) else 0,
            "libraries": libraries,
            "price_cents": price,
            "currency": str(payload.get("currency", base.get("currency", "CNY")))[:8] or "CNY",
            "priority": priority,
            "is_default": 1 if payload.get("is_default", base.get("is_default", 0)) else 0,
        }

    # -- crud ----------------------------------------------------------------
    @staticmethod
    def _row_to_plan(row: dict[str, Any]) -> dict[str, Any]:
        import json
        out = dict(row)
        try:
            out["libraries"] = json.loads(out.pop("libraries_json", "[]") or "[]")
        except ValueError:
            out["libraries"] = []
        for key in ("allow_transcode", "allow_download", "allow_sync", "is_default"):
            out[key] = bool(out.get(key))
        return out

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM plans ORDER BY priority ASC, name ASC")
        plans = [self._row_to_plan(r) for r in rows]
        counts = {
            r["plan_id"]: r["n"] for r in self._db.query(
                "SELECT plan_id, COUNT(*) AS n FROM members "
                "WHERE plan_id IS NOT NULL GROUP BY plan_id")
        }
        for p in plans:
            p["member_count"] = counts.get(p["id"], 0)
        return plans

    def get(self, plan_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        return self._row_to_plan(row) if row else None

    def default_plan_id(self) -> str | None:
        row = self._db.one(
            "SELECT id FROM plans WHERE is_default=1 ORDER BY priority LIMIT 1")
        return row["id"] if row else None

    def create(self, payload: dict[str, Any], now: int | None = None) -> dict[str, Any]:
        import json
        plan = self._validate(payload)
        now = now or int(time.time())
        if self._db.one("SELECT id FROM plans WHERE id=?", (plan["id"],)):
            raise ConfigError(f"套餐 ID 已存在: {plan['id']}")
        with self._db.write() as conn:
            if plan["is_default"]:
                conn.execute("UPDATE plans SET is_default=0")
            conn.execute(
                "INSERT INTO plans (id,name,description,billing_type,"
                "traffic_quota_bytes,traffic_period,duration_days,max_streams,"
                "max_bitrate_kbps,max_devices,allow_transcode,allow_download,"
                "allow_sync,libraries_json,price_cents,currency,priority,"
                "is_default,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan["id"], plan["name"], plan["description"], plan["billing_type"],
                 plan["traffic_quota_bytes"], plan["traffic_period"],
                 plan["duration_days"], plan["max_streams"], plan["max_bitrate_kbps"],
                 plan["max_devices"], plan["allow_transcode"], plan["allow_download"],
                 plan["allow_sync"], json.dumps(plan["libraries"]), plan["price_cents"],
                 plan["currency"], plan["priority"], plan["is_default"], now, now),
            )
        return self.get(plan["id"]) or plan

    def update(self, plan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json
        existing = self.get(plan_id)
        if not existing:
            raise KeyError(plan_id)
        payload = dict(payload)
        payload.setdefault("id", plan_id)
        plan = self._validate(payload, existing=existing)
        if plan["id"] != plan_id and self.get(plan["id"]):
            raise ConfigError(f"套餐 ID 已存在: {plan['id']}")
        now = int(time.time())
        with self._db.write() as conn:
            if plan["is_default"]:
                conn.execute("UPDATE plans SET is_default=0")
            conn.execute(
                "UPDATE plans SET id=?,name=?,description=?,billing_type=?,"
                "traffic_quota_bytes=?,traffic_period=?,duration_days=?,"
                "max_streams=?,max_bitrate_kbps=?,max_devices=?,allow_transcode=?,"
                "allow_download=?,allow_sync=?,libraries_json=?,price_cents=?,"
                "currency=?,priority=?,is_default=?,updated_at=? WHERE id=?",
                (plan["id"], plan["name"], plan["description"], plan["billing_type"],
                 plan["traffic_quota_bytes"], plan["traffic_period"],
                 plan["duration_days"], plan["max_streams"], plan["max_bitrate_kbps"],
                 plan["max_devices"], plan["allow_transcode"], plan["allow_download"],
                 plan["allow_sync"], json.dumps(plan["libraries"]), plan["price_cents"],
                 plan["currency"], plan["priority"], plan["is_default"], now, plan_id),
            )
            if plan["id"] != plan_id:
                conn.execute("UPDATE members SET plan_id=? WHERE plan_id=?",
                             (plan["id"], plan_id))
                conn.execute("UPDATE invites SET plan_id=? WHERE plan_id=?",
                             (plan["id"], plan_id))
        return self.get(plan["id"]) or plan

    def delete(self, plan_id: str) -> bool:
        if not self.get(plan_id):
            raise KeyError(plan_id)
        # Deleting a plan out from under live members would leave them with
        # limits nobody can explain, so it is blocked rather than cascaded.
        row = self._db.one(
            "SELECT COUNT(*) AS n FROM members WHERE plan_id=?", (plan_id,))
        if row and row["n"]:
            raise ConflictError(f"仍有 {row['n']} 个用户在使用该套餐，请先转移他们")
        self._db.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        self._db.execute("DELETE FROM invites WHERE plan_id=?", (plan_id,))
        return True
