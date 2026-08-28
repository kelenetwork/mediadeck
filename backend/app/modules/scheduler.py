"""Load-aware streaming node scheduler (302 dispatch core).

Selection: among enabled+healthy nodes, pick the one with the lowest
normalized load = active_streams / weight.  Ties broken by egress headroom.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import StreamNode


@dataclass
class NodeState:
    node: StreamNode
    ok: bool = False
    active_streams: int = 0
    egress_mbps: float = 0.0
    last_probe_ts: float = 0.0
    consecutive_failures: int = 0
    manually_disabled: bool = False

    def normalized_load(self) -> float:
        w = max(self.node.weight, 0.01)
        return self.active_streams / w

    def available(self) -> bool:
        return (
            self.node.enabled
            and not self.manually_disabled
            and self.ok
            and self.consecutive_failures < 3
        )


class Scheduler:
    def __init__(self, nodes: list[StreamNode], probe: Any) -> None:
        self._states: dict[str, NodeState] = {n.name: NodeState(node=n) for n in nodes}
        self._probe = probe

    async def refresh(self) -> None:
        for st in self._states.values():
            data = await self._probe.load(st.node.probe_url)
            st.last_probe_ts = time.time()
            if data.get("ok"):
                st.ok = True
                st.consecutive_failures = 0
                st.active_streams = int(data.get("active_streams", 0))
                st.egress_mbps = float(data.get("egress_mbps", 0.0))
            else:
                st.ok = False
                st.consecutive_failures += 1

    def pick(self) -> NodeState | None:
        candidates = [s for s in self._states.values() if s.available()]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (s.normalized_load(), s.egress_mbps))

    def set_disabled(self, name: str, disabled: bool) -> bool:
        st = self._states.get(name)
        if not st:
            return False
        st.manually_disabled = disabled
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.node.name,
                "base_url": s.node.base_url,
                "weight": s.node.weight,
                "ok": s.ok,
                "available": s.available(),
                "manually_disabled": s.manually_disabled,
                "active_streams": s.active_streams,
                "egress_mbps": s.egress_mbps,
                "normalized_load": round(s.normalized_load(), 2),
                "last_probe_ts": s.last_probe_ts,
            }
            for s in self._states.values()
        ]
