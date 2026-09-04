"""Detect one account being watched from several places at once.

The hard part is not finding concurrent sessions -- it is deciding which ones
mean anything. A household routinely has a TV and a phone playing at the same
time, and a phone that walks out of wifi onto mobile data changes address
mid-episode. Treating either as "account shared" would cry wolf until the
operator stops reading the alerts, which is worse than not detecting at all.

So the signal is *distinct networks playing simultaneously*, not session count:

- Sessions from the same address are one place, however many devices.
- Addresses inside the same /24 (or /48 for v6) are treated as one place too:
  a household behind CGNAT or a dual-stack router hands out neighbours.
- A network has to hold playback for a while before it counts, so a handover
  that briefly overlaps the old session does not register.

Findings are recorded for the operator to judge. Nothing is suspended
automatically: the cost of being wrong is locking out a paying member over a
VPN reconnect, and that decision belongs to a person.
"""
from __future__ import annotations

import ipaddress
import time
from typing import Any

# How long a network must be playing before it counts as a real second place.
# Below this, an address change is usually a handover rather than another
# viewer: the old session lingers for a few seconds after the new one starts.
MIN_NETWORK_SECONDS = 90.0

# Two networks are "the same place" if they share this prefix. Households sit
# behind one NAT, and CGNAT neighbours land nearby, so a /24 is the smallest
# unit that does not split one home into several.
V4_PREFIX = 24
V6_PREFIX = 48

# Do not re-report the same account every sampling tick.
FINDING_COOLDOWN = 3600.0


def network_key(ip: str) -> str:
    """Collapse an address to the network that identifies a place.

    Unparseable input keeps its raw form rather than being dropped: an address
    the panel cannot classify is still evidence that two sessions differ, and
    silently discarding it would hide exactly the case worth seeing.
    """
    raw = (ip or "").strip()
    if not raw:
        return ""
    # Emby reports "addr:port" for v4 and "[addr]:port" for v6.
    if raw.startswith("["):
        raw = raw[1:].split("]")[0]
    elif raw.count(":") == 1:
        raw = raw.split(":")[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    prefix = V4_PREFIX if addr.version == 4 else V6_PREFIX
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


class SharingDetector:
    """Watches live session state and records multi-network playback."""

    def __init__(self, db: Any, min_seconds: float = MIN_NETWORK_SECONDS,
                 cooldown: float = FINDING_COOLDOWN) -> None:
        self._db = db
        self._min_seconds = min_seconds
        self._cooldown = cooldown
        # user_id -> network -> first time this network was seen playing
        self._seen: dict[str, dict[str, float]] = {}
        self._last_reported: dict[str, float | None] = {}

    def observe(self, live_sessions: list[dict[str, Any]],
                now: float | None = None) -> list[dict[str, Any]]:
        """Fold one sampling tick into the detector, returning new findings.

        ``live_sessions`` is the sampler's own view: entries carrying at least
        ``user_id`` and ``remote_ip``. Sessions that are not actually playing
        should not be passed in -- an idle client parked on a second network
        is not a second viewer.
        """
        now = time.time() if now is None else now
        current: dict[str, dict[str, float]] = {}

        for session in live_sessions:
            user_id = str(session.get("user_id") or "")
            if not user_id:
                continue
            key = network_key(str(session.get("remote_ip") or ""))
            if not key:
                continue
            first_seen = self._seen.get(user_id, {}).get(key, now)
            current.setdefault(user_id, {})[key] = first_seen

        # Networks that stopped playing are forgotten, so a viewer who moves
        # house does not accumulate places forever.
        self._seen = current

        findings: list[dict[str, Any]] = []
        for user_id, networks in current.items():
            established = {
                key: since for key, since in networks.items()
                if now - since >= self._min_seconds
            }
            if len(established) < 2:
                continue
            # None means "never reported", which is not the same as "reported
            # at time zero". Defaulting to 0.0 only happened to work because a
            # real clock is a large number; it would silence the very first
            # finding for any caller with a small time base.
            last = self._last_reported.get(user_id)
            if last is not None and now - last < self._cooldown:
                continue
            self._last_reported[user_id] = now
            findings.append({
                "user_id": user_id,
                "networks": sorted(established),
                "network_count": len(established),
                "detected_at": int(now),
            })
        return findings

    def record(self, finding: dict[str, Any], username: str = "") -> None:
        """Persist a finding for the operator to review."""
        self._db.execute(
            "INSERT INTO sharing_findings"
            "(emby_user_id,username,networks,network_count,detected_at) "
            "VALUES(?,?,?,?,?)",
            (finding["user_id"], username, ",".join(finding["networks"]),
             finding["network_count"], finding["detected_at"]))

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM sharing_findings ORDER BY detected_at DESC LIMIT ?",
            (max(1, min(limit, 500)),))
        out = []
        for row in rows:
            entry = dict(row)
            entry["networks"] = [n for n in str(entry.get("networks") or "").split(",") if n]
            out.append(entry)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "tracked_accounts": len(self._seen),
            "multi_network_now": sum(
                1 for nets in self._seen.values() if len(nets) >= 2),
            "min_network_seconds": self._min_seconds,
        }
