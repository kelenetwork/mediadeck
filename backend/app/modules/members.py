"""Members — the link between an Emby account and a user group.

The single most important rule in this file: **an Emby user with no member row
is invisible to the panel.**  This server has hundreds of accounts created long
before the panel existed.  If enforcement iterated over Emby's user list rather
than over member rows, one bad deploy could disable all of them at once.  So
membership is opt-in (or explicit bulk enroll), and every enforcement query
starts from `members`.

The second rule: state is derived, never guessed.  `status` is stored, but
`effective_state()` recomputes it from expiry and quota every time it is asked,
so a member whose group changed or whose month rolled over is correct
immediately rather than at the next sampler tick.

v0.14: plans (products someone buys) are gone.  A member belongs to exactly one
*group* — the operator's billing preset (see groups.py) — plus zero or more
*roles* (admin / uploader), which are job functions, not resource limits.
"""
from __future__ import annotations

import json
import secrets
import string
import time
from datetime import UTC, datetime
from typing import Any

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules.groups import GroupService, needs_duration, needs_traffic

# active    : normal
# suspended : operator disabled by hand; never auto-cleared
# expired   : past expires_at
# exhausted : traffic quota consumed
# pending   : created but not yet activated
MEMBER_STATES = ("active", "suspended", "expired", "exhausted", "pending")

# Operator-set states that automatic enforcement must never overwrite. Losing
# this distinction would mean a suspended user silently comes back the moment
# their quota resets.
MANUAL_STATES = ("suspended", "pending")

# Additive job functions. admin => may log into the panel with their Emby
# credentials; uploader => flagged for the future request-intake pipeline.
ROLES = ("admin", "uploader")

# Keys an operator may put in members.overrides_json. Anything else is rejected
# so a typo cannot silently invent a new limit that enforcement never reads.
OVERRIDE_KEYS = (
    "max_streams",
    "bandwidth_limit_kbps",
    "max_devices",
    "allow_transcode",
    "allow_download",
    "libraries_mode",
    "libraries",
    "expires_at_override",
    "extra_traffic_bytes",
)
LIBRARY_MODES = ("inherit", "replace", "extend")
AUDIT_DETAIL_MAX = 4000

# Panel and nginx speak different units. The stored column stays kbps so Emby
# RemoteClientBitrateLimit (bits/s = kbps * 1000) and historical rows keep
# working. The operator reads and types MB/s, matching the live speed column.
# nginx limit_rate wants bytes/second: kbps * 125.
BYTES_PER_KBPS = 125


def rate_bytes_per_sec(kbps: int) -> int:
    """Convert stored kbps to nginx limit_rate bytes/second.

    0 stays 0 (uncapped). The panel displays MB/s; this is the unit the
    node actually enforces.
    """
    kbps = int(kbps or 0)
    if kbps <= 0:
        return 0
    return kbps * BYTES_PER_KBPS


def random_password(length: int = 12) -> str:
    """For operator-reset accounts, so nobody reuses '123456'."""
    pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def period_start(now: int) -> int:
    """Start of the current calendar month (UTC), in epoch seconds.

    Traffic always resets on month boundaries (owner decision 2026-08-30) so
    every member's window is the same and support is never guesswork.
    """
    dt = datetime.fromtimestamp(now, UTC)
    return int(dt.replace(day=1, hour=0, minute=0, second=0,
                          microsecond=0).timestamp())


def parse_overrides(raw: Any) -> dict[str, Any]:
    """Decode stored JSON without raising on a corrupt row."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_roles(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")
    out = []
    for item in items:
        role = str(item).strip().lower()
        if role in ROLES and role not in out:
            out.append(role)
    return out


def _as_int(raw: Any, label: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是整数") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{label}必须在 {lo}–{hi} 之间")
    return value


def validate_overrides(payload: Any) -> dict[str, Any]:
    """Whitelist + type/range check. Empty object means inherit everything."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ConfigError("覆盖层必须是对象")
    unknown = [k for k in payload if k not in OVERRIDE_KEYS]
    if unknown:
        raise ConfigError(f"不支持的覆盖字段: {', '.join(sorted(unknown))}")

    out: dict[str, Any] = {}
    if "max_streams" in payload:
        out["max_streams"] = _as_int(payload["max_streams"], "并发路数", 0, 100)
    if "bandwidth_limit_kbps" in payload:
        out["bandwidth_limit_kbps"] = _as_int(
            payload["bandwidth_limit_kbps"], "带宽限速", 0, 10_000_000)
    if "max_devices" in payload:
        out["max_devices"] = _as_int(payload["max_devices"], "设备数上限", 0, 100)
    for flag in ("allow_transcode", "allow_download"):
        if flag in payload:
            out[flag] = 1 if payload[flag] else 0
    if "libraries_mode" in payload:
        mode = str(payload["libraries_mode"] or "inherit")
        if mode not in LIBRARY_MODES:
            raise ConfigError(f"媒体库模式必须是 {'/'.join(LIBRARY_MODES)} 之一")
        out["libraries_mode"] = mode
    if "libraries" in payload:
        libraries = payload["libraries"]
        if not isinstance(libraries, list):
            raise ConfigError("媒体库列表格式错误")
        out["libraries"] = [str(x) for x in libraries if str(x).strip()]
    if "expires_at_override" in payload:
        raw = payload["expires_at_override"]
        if raw is None or raw == "":
            out["expires_at_override"] = None
        else:
            out["expires_at_override"] = _as_int(
                raw, "到期时间覆盖", 0, 4_102_444_800)
    if "extra_traffic_bytes" in payload:
        out["extra_traffic_bytes"] = _as_int(
            payload["extra_traffic_bytes"], "额外流量", 0, 1 << 60)
    # libraries without an explicit mode is treated as replace: the operator
    # listed libraries, so they meant those libraries, not "also inherit".
    if "libraries" in out and "libraries_mode" not in out:
        out["libraries_mode"] = "replace"
    return out


def merge_effective(group: dict[str, Any] | None, overrides: dict[str, Any] | None,
                    member: dict[str, Any] | None = None) -> dict[str, Any]:
    """Group defaults with per-member overrides applied field-by-field.

    A key that is absent from overrides is inherited. Presence — even of a
    value equal to the group default — is an override, so the UI can show it
    as such.
    """
    group = group or {}
    ov = overrides or {}
    streams = ov["max_streams"] if "max_streams" in ov else int(
        group.get("max_streams") or 0)
    bandwidth = ov["bandwidth_limit_kbps"] if "bandwidth_limit_kbps" in ov else int(
        group.get("bandwidth_limit_kbps") or 0)
    devices = ov["max_devices"] if "max_devices" in ov else int(
        group.get("max_devices") or 0)
    transcode = ov["allow_transcode"] if "allow_transcode" in ov else group.get(
        "allow_transcode", 1)
    download = ov["allow_download"] if "allow_download" in ov else group.get(
        "allow_download", 0)

    # Groups carry no library restriction; per-member overrides may.
    ov_libs = list(ov.get("libraries") or [])
    mode = ov.get("libraries_mode") or "inherit"
    libraries = ov_libs if mode in ("replace", "extend") else []

    stored_expires = (member or {}).get("expires_at")
    if "expires_at_override" in ov:
        expires_at = ov.get("expires_at_override")
    else:
        expires_at = stored_expires

    extra = int(ov.get("extra_traffic_bytes") or 0)
    base_quota = int(group.get("traffic_quota_bytes") or 0)
    if group and needs_traffic(group.get("billing_mode") or "") and (
            base_quota or extra):
        quota = base_quota + extra
    else:
        quota = 0

    overridden = [k for k in OVERRIDE_KEYS if k in ov]
    return {
        "max_streams": int(streams),
        "bandwidth_limit_kbps": int(bandwidth),
        "max_devices": int(devices),
        "allow_transcode": bool(transcode),
        "allow_download": bool(download),
        "libraries": libraries,
        "expires_at": expires_at,
        "traffic_quota_bytes": quota,
        "overridden_keys": overridden,
    }


def audit_diff(before: dict[str, Any] | None, after: dict[str, Any] | None
               ) -> dict[str, dict[str, Any]]:
    """`{field: {from, to}}` for fields that actually changed."""
    before = before or {}
    after = after or {}
    keys = sorted(set(before) | set(after))
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        left, right = before.get(key, None), after.get(key, None)
        if left != right:
            out[key] = {"from": left, "to": right}
    return out


def encode_audit_detail(diff: dict[str, Any] | None, extra: str = "") -> str:
    payload: dict[str, Any] = {}
    if diff:
        payload.update(diff)
    if extra:
        payload["note"] = extra[:200]
    if not payload:
        return ""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return raw[:AUDIT_DETAIL_MAX]


class MemberService:
    def __init__(self, db: Database, groups: GroupService) -> None:
        self._db = db
        self._groups = groups

    # -- read ----------------------------------------------------------------
    def get(self, user_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM members WHERE emby_user_id=?", (user_id,))
        return self._decorate(row) if row else None

    def list(self, status: str | None = None, group_id: str | None = None,
             role: str | None = None, search: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM members"
        clauses, params = [], []
        if group_id:
            clauses.append("group_id=?")
            params.append(group_id)
        if search:
            clauses.append("(username LIKE ? OR note LIKE ? OR contact LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY username COLLATE NOCASE ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        rows = [self._decorate(r) for r in self._db.query(sql, tuple(params))]
        # Status/role are filtered after decoration: the *effective* state is
        # time-dependent and roles live in a comma-separated column.
        if status:
            rows = [r for r in rows if r["state"] == status]
        if role:
            rows = [r for r in rows if role in r["roles"]]
        return rows

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        group = self._groups.get(out.get("group_id") or "")
        out["group"] = group
        out["group_name"] = group["name"] if group else "(未分组)"
        out["billing_mode"] = group["billing_mode"] if group else "none"
        out["roles"] = parse_roles(out.pop("roles", ""))
        overrides = parse_overrides(out.pop("overrides_json", None))
        out["overrides"] = overrides
        effective = merge_effective(group, overrides, out)
        out["effective"] = effective
        out["overridden_keys"] = list(effective["overridden_keys"])
        # Flatten the fields the rest of the panel reads.
        out["max_streams"] = effective["max_streams"]
        out["bandwidth_limit_kbps"] = effective["bandwidth_limit_kbps"]
        out["max_devices"] = effective["max_devices"]
        out["allow_transcode"] = effective["allow_transcode"]
        out["allow_download"] = effective["allow_download"]
        out["libraries"] = list(effective["libraries"])
        out["expires_at_effective"] = effective["expires_at"]

        state, reason = self.effective_state(out, group)
        out["state"] = state
        out["state_reason"] = reason

        quota = int(effective.get("traffic_quota_bytes") or 0)
        used = int(out.get("traffic_used_bytes") or 0)
        out["traffic_quota_bytes"] = quota
        out["traffic_remaining_bytes"] = max(0, quota - used) if quota else None
        out["traffic_percent"] = round(used / quota * 100, 1) if quota else None

        expires = effective.get("expires_at")
        out["days_remaining"] = (
            max(0, int((expires - time.time()) // 86400)) if expires else None)
        out["device_count"] = self._db.one(
            "SELECT COUNT(*) AS n FROM devices WHERE emby_user_id=? AND blocked=0",
            (out["emby_user_id"],))["n"]
        return out

    def detail(self, user_id: str, *, audit_limit: int = 50) -> dict[str, Any] | None:
        """Drawer payload: member + devices + that member's audit trail."""
        member = self.get(user_id)
        if not member:
            return None
        return {
            "member": member,
            "devices": self.devices(user_id),
            "audit": self.audit_log(audit_limit, subject=user_id),
        }

    @staticmethod
    def effective_state(member: dict[str, Any], group: dict[str, Any] | None,
                        now: int | None = None) -> tuple[str, str]:
        """Recompute state from the facts, not from the stored label."""
        now = now or int(time.time())
        stored = str(member.get("status") or "active")
        if stored in MANUAL_STATES:
            return stored, "管理员手动设置"
        if not group:
            return "active", "未分组，不做计费"

        effective = member.get("effective")
        if not isinstance(effective, dict):
            effective = merge_effective(
                group, member.get("overrides") or parse_overrides(
                    member.get("overrides_json")), member)

        mode = str(group.get("billing_mode") or "none")
        expires = effective.get("expires_at", member.get("expires_at"))
        has_expiry_override = "expires_at_override" in (member.get("overrides") or {})
        if expires and now >= expires and (
                has_expiry_override or needs_duration(mode)):
            return "expired", "已过期"

        quota = int(effective.get("traffic_quota_bytes") or 0)
        used = int(member.get("traffic_used_bytes") or 0)
        if needs_traffic(mode) and quota and used >= quota:
            return "exhausted", "本月流量已用尽"
        return "active", "正常"

    # -- write ---------------------------------------------------------------
    def upsert(self, user_id: str, username: str, payload: dict[str, Any],
               actor: str = "system") -> dict[str, Any]:
        if not user_id:
            raise ConfigError("缺少 Emby 用户 ID")
        now = int(time.time())
        existing = self._db.one(
            "SELECT * FROM members WHERE emby_user_id=?", (user_id,))

        group_id = payload.get(
            "group_id", existing["group_id"] if existing else None)
        if group_id is not None:
            group_id = str(group_id) or None
        group = None
        if group_id:
            group = self._groups.get(group_id)
            if not group:
                raise ConfigError(f"用户组不存在: {group_id}")

        status = str(payload.get(
            "status", existing["status"] if existing else "active"))
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")

        roles = payload.get("roles", "__keep__")
        if roles == "__keep__":
            roles_csv = existing["roles"] if existing else ""
        else:
            roles_csv = ",".join(parse_roles(roles))

        # Expiry: explicit value wins; otherwise derive from the group when the
        # member is new or the group changed, so assigning a timed group never
        # silently leaves a member with no end date.
        expires_at = payload.get("expires_at", "__keep__")
        if expires_at == "__keep__":
            expires_at = existing["expires_at"] if existing else None
            group_changed = bool(existing) and existing["group_id"] != group_id
            if group and needs_duration(group["billing_mode"]) and (
                    not existing or group_changed or not expires_at):
                expires_at = now + int(group["duration_days"]) * 86400
            if group and not needs_duration(group["billing_mode"]):
                expires_at = None
        elif expires_at is not None:
            expires_at = int(expires_at)

        p_start = existing["traffic_period_start"] if existing else 0
        if not p_start:
            p_start = period_start(now)
        used = int(payload.get(
            "traffic_used_bytes",
            existing["traffic_used_bytes"] if existing else 0))

        row = (
            username or (existing["username"] if existing else ""),
            group_id,
            roles_csv,
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
                "UPDATE members SET username=?,group_id=?,roles=?,status=?,"
                "expires_at=?,traffic_used_bytes=?,traffic_period_start=?,"
                "note=?,contact=?,updated_at=? WHERE emby_user_id=?",
                row + (user_id,))
            self.audit(actor, "member.update", user_id, encode_audit_detail(audit_diff(
                {"group_id": existing.get("group_id"),
                 "roles": existing.get("roles"),
                 "status": existing.get("status"),
                 "expires_at": existing.get("expires_at")},
                {"group_id": group_id, "roles": roles_csv,
                 "status": status, "expires_at": expires_at},
            )))
        else:
            self._db.execute(
                "INSERT INTO members (username,group_id,roles,status,expires_at,"
                "traffic_used_bytes,traffic_period_start,note,contact,updated_at,"
                "emby_user_id,created_at,overrides_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row + (user_id, now, "{}"))
            self.audit(actor, "member.create", user_id, encode_audit_detail({
                "group_id": {"from": None, "to": group_id},
                "status": {"from": None, "to": status},
                "expires_at": {"from": None, "to": expires_at},
            }))
        return self.get(user_id)  # type: ignore[return-value]

    def enroll_defaults(self, emby_users: list[dict[str, Any]],
                        actor: str = "operator") -> int:
        """Give every unmanaged Emby account a member row in the default group.

        Explicitly operator-triggered (never automatic on list) so that one
        accidental page load cannot mass-create hundreds of billed accounts.
        """
        default_group = self._groups.default_group_id()
        if not default_group:
            raise ConfigError("没有默认用户组，先在用户组页设置一个")
        enrolled = 0
        for user in emby_users:
            user_id = str(user.get("Id") or "")
            if not user_id:
                continue
            if self._db.one("SELECT 1 AS x FROM members WHERE emby_user_id=?",
                            (user_id,)):
                continue
            self.upsert(user_id, str(user.get("Name") or ""),
                        {"group_id": default_group}, actor=actor)
            enrolled += 1
        return enrolled

    def set_roles(self, user_id: str, roles: Any,
                  actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        cleaned = parse_roles(roles)
        before = ",".join(member.get("roles") or [])
        after = ",".join(cleaned)
        if before != after:
            self._db.execute(
                "UPDATE members SET roles=?,updated_at=? WHERE emby_user_id=?",
                (after, int(time.time()), user_id))
            self.audit(actor, "member.roles", user_id, encode_audit_detail({
                "roles": {"from": before, "to": after},
            }))
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
        """Record a device, refusing new ones once the member's cap is hit.

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
            blocked = self._db.one(
                "SELECT blocked FROM devices WHERE emby_user_id=? AND device_id=?",
                (user_id, device_id))
            self._db.execute(
                "UPDATE devices SET device_name=?,client=?,app_version=?,"
                "last_ip=?,last_seen_at=? WHERE emby_user_id=? AND device_id=?",
                (device_name, client, app_version, last_ip, now, user_id, device_id))
            if blocked and blocked.get("blocked"):
                self.audit("system", "device.blocked", user_id,
                           encode_audit_detail({
                               "device_id": {"from": device_id, "to": device_id},
                           }), ok=False)
                return False
            return True

        member = self.get(user_id)
        cap = int((member or {}).get("max_devices") or 0)
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
        renewing early never costs the member the days they already have."""
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        group = member.get("group")
        default_days = int(group["duration_days"]) if group else 0
        add_days = int(days if days is not None else default_days)
        if add_days <= 0:
            raise ConfigError("续期天数必须大于 0")
        now = int(time.time())
        base = max(now, int(member.get("expires_at") or now))
        new_expiry = base + add_days * 86400
        self._db.execute(
            "UPDATE members SET expires_at=?,status=CASE WHEN status IN "
            "('expired','exhausted') THEN 'active' ELSE status END,updated_at=? "
            "WHERE emby_user_id=?", (new_expiry, now, user_id))
        self.audit(actor, "member.renew", user_id, encode_audit_detail({
            "expires_at": {"from": member.get("expires_at"), "to": new_expiry},
            "days": {"from": None, "to": add_days},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def reset_traffic(self, user_id: str, actor: str = "operator") -> dict[str, Any]:
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        now = int(time.time())
        used_before = int(member.get("traffic_used_bytes") or 0)
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
            "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
            "updated_at=? WHERE emby_user_id=?",
            (period_start(now), now, user_id))
        self.audit(actor, "member.reset_traffic", user_id, encode_audit_detail({
            "traffic_used_bytes": {"from": used_before, "to": 0},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def set_status(self, user_id: str, status: str, actor: str = "operator"
                   ) -> dict[str, Any]:
        if status not in MEMBER_STATES:
            raise ConfigError(f"状态必须是 {'/'.join(MEMBER_STATES)} 之一")
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        before = member.get("status")
        self._db.execute(
            "UPDATE members SET status=?,updated_at=? WHERE emby_user_id=?",
            (status, int(time.time()), user_id))
        self.audit(actor, "member.status", user_id, encode_audit_detail({
            "status": {"from": before, "to": status},
        }))
        return self.get(user_id)  # type: ignore[return-value]

    def roll_periods(self, now: int | None = None) -> int:
        """Monthly reset: zero usage, drop one-off extra traffic, unblock
        exhausted members. Manual states are never touched."""
        now = now or int(time.time())
        rolled = 0
        current = period_start(now)
        for member in self.list(limit=5000):
            group = member.get("group")
            if not group or not needs_traffic(group["billing_mode"]):
                continue
            if int(member.get("traffic_period_start") or 0) >= current:
                continue
            ov = dict(member.get("overrides") or {})
            extra_before = int(ov.get("extra_traffic_bytes") or 0)
            if extra_before:
                ov.pop("extra_traffic_bytes", None)
            self._db.execute(
                "UPDATE members SET traffic_used_bytes=0,traffic_period_start=?,"
                "status=CASE WHEN status='exhausted' THEN 'active' ELSE status END,"
                "overrides_json=?,updated_at=? WHERE emby_user_id=?",
                (current, json.dumps(ov, ensure_ascii=False, sort_keys=True),
                 now, member["emby_user_id"]))
            self.audit("system", "member.period_roll", member["emby_user_id"],
                       encode_audit_detail({
                           "extra_traffic_bytes": {"from": extra_before, "to": 0},
                       }))
            rolled += 1
        return rolled

    def add_traffic(self, user_id: str, delta_bytes: int) -> None:
        if delta_bytes <= 0:
            return
        self._db.execute(
            "UPDATE members SET traffic_used_bytes=traffic_used_bytes+?,"
            "last_seen_at=?,updated_at=? WHERE emby_user_id=?",
            (int(delta_bytes), int(time.time()), int(time.time()), user_id))

    def set_overrides(self, user_id: str, payload: dict[str, Any] | None,
                      actor: str = "operator") -> dict[str, Any]:
        """Replace the overlay wholesale. Empty object restores full inheritance."""
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        cleaned = validate_overrides(payload or {})
        before = member.get("overrides") or {}
        self._db.execute(
            "UPDATE members SET overrides_json=?,updated_at=? WHERE emby_user_id=?",
            (json.dumps(cleaned, ensure_ascii=False, sort_keys=True),
             int(time.time()), user_id))
        self.audit(actor, "member.overrides", user_id,
                   encode_audit_detail(audit_diff(before, cleaned)))
        return self.get(user_id)  # type: ignore[return-value]

    def add_extra_traffic(self, user_id: str, delta_bytes: int,
                          actor: str = "operator") -> dict[str, Any]:
        """Accumulate extra_traffic_bytes on the overlay (current month)."""
        if delta_bytes < 0:
            raise ConfigError("额外流量必须大于等于 0")
        member = self.get(user_id)
        if not member:
            raise KeyError(user_id)
        ov = dict(member.get("overrides") or {})
        before = int(ov.get("extra_traffic_bytes") or 0)
        ov["extra_traffic_bytes"] = before + int(delta_bytes)
        return self.set_overrides(user_id, ov, actor=actor)

    def set_device_blocked(self, user_id: str, device_id: str, blocked: bool,
                           actor: str = "operator") -> dict[str, Any]:
        row = self._db.one(
            "SELECT * FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id))
        if not row:
            raise KeyError(device_id)
        was = bool(row.get("blocked"))
        want = bool(blocked)
        if was != want:
            self._db.execute(
                "UPDATE devices SET blocked=? WHERE emby_user_id=? AND device_id=?",
                (1 if want else 0, user_id, device_id))
            self.audit(actor, "device.block" if want else "device.unblock",
                       user_id, encode_audit_detail({
                           "device_id": {"from": device_id, "to": device_id},
                           "blocked": {"from": was, "to": want},
                       }))
        return self._db.one(
            "SELECT * FROM devices WHERE emby_user_id=? AND device_id=?",
            (user_id, device_id)) or {}

    def devices(self, user_id: str) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM devices WHERE emby_user_id=? ORDER BY last_seen_at DESC",
            (user_id,))

    # -- audit ---------------------------------------------------------------
    def audit(self, actor: str, action: str, subject: str = "",
              detail: str = "", ok: bool = True) -> None:
        self._db.execute(
            "INSERT INTO audit_log (ts,actor,action,subject,detail,ok) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor[:60], action[:60], subject[:80],
             detail[:AUDIT_DETAIL_MAX], 1 if ok else 0))

    def audit_log(self, limit: int = 100, offset: int = 0,
                  subject: str | None = None, actor: str | None = None,
                  action: str | None = None) -> list[dict[str, Any]]:
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
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(limit, 1000)))
        params.append(max(0, int(offset)))
        return self._db.query(sql, tuple(params))

    def audit_count(self, subject: str | None = None, actor: str | None = None,
                    action: str | None = None) -> int:
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
        sql = "SELECT COUNT(*) AS n FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._db.one(sql, tuple(params))
        return int((row or {}).get("n") or 0)
