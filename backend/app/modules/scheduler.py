"""Load-aware streaming node scheduler (302 dispatch core).

Selection: among enabled+healthy nodes, pick the one with the lowest
normalized load = active_streams / weight.  Ties broken by egress headroom.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
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
    HISTORY_MAX = 720      # probe snapshots kept per node (~3h at 15s interval)
    DISPATCH_MAX = 1000    # recent dispatch decisions kept

    def __init__(self, nodes: list[StreamNode], probe: Any) -> None:
        self._states: dict[str, NodeState] = {n.name: NodeState(node=n) for n in nodes}
        self._probe = probe
        self._history: dict[str, deque[dict[str, Any]]] = {
            n.name: deque(maxlen=self.HISTORY_MAX) for n in nodes
        }
        self._dispatch_log: deque[dict[str, Any]] = deque(maxlen=self.DISPATCH_MAX)

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
            self._history[st.node.name].append({
                "ts": st.last_probe_ts,
                "ok": st.ok,
                "active_streams": st.active_streams if st.ok else None,
                "egress_mbps": st.egress_mbps if st.ok else None,
            })

    def pick(self, record: bool = True, context: str = "") -> NodeState | None:
        candidates = [s for s in self._states.values() if s.available()]
        chosen = None
        if candidates:
            chosen = min(candidates, key=lambda s: (s.normalized_load(), s.egress_mbps))
        if record:
            self._dispatch_log.append({
                "ts": time.time(),
                "node": chosen.node.name if chosen else None,
                "normalized_load": chosen.normalized_load() if chosen else None,
                "candidates": len(candidates),
                "context": context[:200],
            })
        return chosen

    def history(self, name: str, limit: int = 240) -> list[dict[str, Any]]:
        entries = self._history.get(name)
        if entries is None:
            raise KeyError(name)
        return list(entries)[-max(1, min(limit, self.HISTORY_MAX)):]

    def dispatch_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._dispatch_log)[-max(1, min(limit, self.DISPATCH_MAX)):]

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
