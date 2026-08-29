"""Enforcement — project member state onto the Emby account.

A plan is only a promise until something writes it into Emby.  This module is
that something.  It maps every limit onto the Emby policy field that actually
enforces it, so limits hold even when the panel is down:

    max_streams      -> SimultaneousStreamLimit
    max_bitrate_kbps -> RemoteClientBitrateLimit
    allow_transcode  -> Enable{Video,Audio}PlaybackTranscoding + Remuxing
    allow_download   -> EnableContentDownloading
    allow_sync       -> EnableSyncTranscoding
    libraries        -> EnableAllFolders / EnabledFolders
    expired/exhausted-> IsDisabled

Three rules keep this safe on a server with hundreds of pre-existing accounts:

**Membership is opt-in.**  Reconciliation iterates member rows, never Emby's
user list.  An account the operator never enrolled is untouched, so no bug here
can disable the whole server.

**Administrators are never disabled.**  Locking the operator out of their own
server via a quota rounding error is not an acceptable failure mode.

**Writes are fingerprinted.**  The desired policy is hashed; if it matches what
was last applied, nothing is sent.  Otherwise a nightly reconcile would rewrite
hundreds of policies and bury real changes in noise.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.core.db import Database
from app.modules.members import MemberService

# States in which the account must not be able to play.
BLOCKING_STATES = ("expired", "exhausted", "suspended", "pending")

# Policy keys this module owns. Anything outside this set is left exactly as
# the operator configured it in Emby -- the panel manages access, not every
# preference on the account.
MANAGED_KEYS = (
    "IsDisabled",
    "SimultaneousStreamLimit",
    "RemoteClientBitrateLimit",
    "EnableVideoPlaybackTranscoding",
    "EnableAudioPlaybackTranscoding",
    "EnablePlaybackRemuxing",
    "EnableContentDownloading",
    "EnableSyncTranscoding",
    "EnableMediaConversion",
    "EnableAllFolders",
    "EnabledFolders",
)


def desired_policy(member: dict[str, Any]) -> dict[str, Any]:
    """The Emby policy fields implied by this member's plan and state."""
    plan = member.get("plan") or {}
    state = member.get("state", "active")
    blocked = state in BLOCKING_STATES

    policy: dict[str, Any] = {"IsDisabled": blocked}

    if not plan:
        # Enrolled but no plan: manage nothing except the block flag, so an
        # operator can park an account without inheriting arbitrary limits.
        return policy

    policy["SimultaneousStreamLimit"] = int(plan.get("max_streams") or 1)
    # Emby treats 0 as "no limit" for bitrate, which matches our convention.
    policy["RemoteClientBitrateLimit"] = int(plan.get("max_bitrate_kbps") or 0) * 1000

    transcode = bool(plan.get("allow_transcode"))
    policy["EnableVideoPlaybackTranscoding"] = transcode
    policy["EnableAudioPlaybackTranscoding"] = transcode
    policy["EnablePlaybackRemuxing"] = transcode

    policy["EnableContentDownloading"] = bool(plan.get("allow_download"))
    sync = bool(plan.get("allow_sync"))
    policy["EnableSyncTranscoding"] = sync
    policy["EnableMediaConversion"] = sync

    libraries = list(plan.get("libraries") or [])
    if libraries:
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = libraries
    else:
        policy["EnableAllFolders"] = True
        policy["EnabledFolders"] = []
    return policy


def fingerprint(policy: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


class EnforcementService:
    def __init__(self, db: Database, members: MemberService, emby: Any) -> None:
        self._db = db
        self._members = members
        self._emby = emby

    async def reconcile(self, apply: bool = False, user_id: str | None = None,
                        force: bool = False) -> dict[str, Any]:
        """Bring Emby in line with member state.

        Defaults to dry-run: this writes to live accounts, so producing the
        plan and applying it are separate decisions.
        """
        rows = ([self._members.get(user_id)] if user_id
                else self._members.list(limit=5000))
        rows = [r for r in rows if r]

        # One Emby read for the whole pass rather than one per member: on a
        # 300-account server that is the difference between a second and a
        # minute, and reconcile runs on a timer.
        try:
            emby_users = {u["Id"]: u for u in await self._emby.list_users()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法读取 Emby 用户列表: {exc}",
                    "planned": 0, "applied": 0}

        planned, applied, skipped, errors = [], 0, [], []
        for member in rows:
            uid = member["emby_user_id"]
            emby_user = emby_users.get(uid)
            if not emby_user:
                # The Emby account is gone; flag it rather than deleting the
                # member row, so the operator decides what happened.
                skipped.append({"user_id": uid, "reason": "emby_user_missing",
                                "username": member.get("username")})
                continue

            current = emby_user.get("Policy") or {}
            if current.get("IsAdministrator"):
                skipped.append({"user_id": uid, "reason": "administrator",
                                "username": member.get("username")})
                continue

            want = desired_policy(member)
            fp = fingerprint(want)
            # Already applied and unchanged. Still verify Emby agrees on the
            # one field that matters most, so drift caused by someone editing
            # Emby directly is not invisible.
            unchanged = not force and fp == member.get("applied_fingerprint")
            if unchanged and bool(current.get("IsDisabled")) == bool(want["IsDisabled"]):
                continue

            diff = {k: v for k, v in want.items()
                    if _normalise(current.get(k)) != _normalise(v)}
            if not diff and not force:
                if fp != member.get("applied_fingerprint"):
                    self._db.execute(
                        "UPDATE members SET applied_fingerprint=?,applied_at=? "
                        "WHERE emby_user_id=?", (fp, int(time.time()), uid))
                continue

            planned.append({
                "user_id": uid,
                "username": member.get("username"),
                "state": member.get("state"),
                "plan": member.get("plan_name"),
                "changes": diff,
            })

            if not apply:
                continue

            try:
                ok = await self._emby.apply_policy(uid, want)
            except Exception as exc:  # noqa: BLE001
                ok = False
                errors.append({"user_id": uid, "error": str(exc)[:200]})
            if ok:
                applied += 1
                self._db.execute(
                    "UPDATE members SET applied_fingerprint=?,applied_at=? "
                    "WHERE emby_user_id=?", (fp, int(time.time()), uid))
                self._members.audit(
                    "system", "enforce.apply", uid,
                    f"state={member.get('state')} changes={sorted(diff)}")
            else:
                self._members.audit(
                    "system", "enforce.fail", uid,
                    f"state={member.get('state')}", ok=False)

        return {
            "ok": True,
            "dry_run": not apply,
            "considered": len(rows),
            "planned": len(planned),
            "applied": applied,
            "changes": planned[:200],
            "skipped": skipped[:100],
            "errors": errors[:50],
        }

    async def enforce_now(self, user_id: str, reason: str = "") -> bool:
        """Apply one member immediately.

        Used when quota runs out mid-stream: waiting for the next timed pass
        would let a user keep watching well past their limit.
        """
        member = self._members.get(user_id)
        if not member:
            return False
        want = desired_policy(member)
        try:
            ok = await self._emby.apply_policy(user_id, want)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._db.execute(
                "UPDATE members SET applied_fingerprint=?,applied_at=? "
                "WHERE emby_user_id=?",
                (fingerprint(want), int(time.time()), user_id))
        self._members.audit("system", "enforce.immediate", user_id,
                            reason or member.get("state", ""), ok=ok)
        return ok

    async def terminate_sessions(self, user_id: str, reason: str = "配额已用尽"
                                 ) -> int:
        """Stop this member's active playback.

        Disabling an account does not end sessions that already hold a stream,
        so a user who exhausts their quota would otherwise finish the film.
        """
        stopped = 0
        try:
            sessions = await self._emby.active_sessions_raw()
        except Exception:  # noqa: BLE001
            return 0
        failures = 0
        for s in sessions:
            if s.get("UserId") != user_id:
                continue
            try:
                if await self._emby.stop_session(s.get("Id"), reason):
                    stopped += 1
            except Exception:  # noqa: BLE001
                # One unstoppable session must not prevent stopping the rest;
                # the count is audited below so failures stay visible.
                failures += 1
        if failures:
            self._members.audit("system", "enforce.terminate_partial", user_id,
                                f"{failures} session(s) could not be stopped", ok=False)
        if stopped:
            self._members.audit("system", "enforce.terminate", user_id,
                                f"{stopped} session(s): {reason}")
        return stopped


def _normalise(value: Any) -> Any:
    """Compare policy values the way Emby stores them.

    Emby returns lists in arbitrary order and bools sometimes as ints; without
    this every reconcile would report spurious differences and rewrite policies
    that are already correct.
    """
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if value is None:
        return None
    return str(value)
