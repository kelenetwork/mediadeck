"""Node pool management.

The operation this supports is an incident action: pulling a node out of
rotation because it is misbehaving. So the tests care most about two things —
that a disabled node is genuinely never dispatched to, and that editing four
fields cannot damage the rest of a node's configuration.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.core.config import NodePool, StreamNode
from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.main import app
from app.modules.scheduler import Scheduler
from app.modules.settings import SettingsService


def _basic(user: str = "admin", pw_value: str = "change-me") -> dict[str, str]:
    creds = base64.b64encode(f"{user}:{pw_value}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


class StubProbe:
    def __init__(self, ok: bool = True, streams: int = 0, egress: float = 0.0):
        self.ok, self.streams, self.egress = ok, streams, egress

    async def load(self, probe_url: str) -> dict:
        return {"ok": self.ok, "active_streams": self.streams,
                "egress_mbps": self.egress}


def _service(tmp_path, nodes=None):
    store = SettingsStore(tmp_path / "settings.json")
    service = SettingsService(store)
    pools = [NodePool(name="main", emby_prefix="/media", url_prefix="/s/main")]
    store.set("nodes", [n.model_dump() for n in (nodes or [
        StreamNode(name="n1", base_url="https://n1.example",
                   probe_url="http://127.0.0.1:9800/load", capacity=50,
                   bandwidth_mbps=500, pools=list(pools), sign_secret="s1"),
        StreamNode(name="n2", base_url="https://n2.example",
                   probe_url="http://127.0.0.1:9801/load", capacity=100,
                   bandwidth_mbps=1000, pools=list(pools), sign_secret="s2"),
    ])])
    return service, store


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_updates_the_four_dispatch_fields(tmp_path) -> None:
    service, _ = _service(tmp_path)
    out = service.update_node_pool("n1", {
        "enabled": False, "weight": 80, "bandwidth_mbps": 250})
    assert out["node"]["enabled"] is False
    assert out["node"]["capacity"] == 80
    assert out["node"]["bandwidth_mbps"] == 250
    assert set(out["changed"]) == {"enabled", "capacity", "bandwidth_mbps"}


def test_reports_only_what_actually_changed(tmp_path) -> None:
    """The audit entry must describe the change, not the submission: a form
    that posts every field would otherwise log an edit that did nothing."""
    service, _ = _service(tmp_path)
    out = service.update_node_pool("n1", {
        "enabled": True, "weight": 50, "bandwidth_mbps": 500})
    assert out["changed"] == {}


def test_partial_edit_leaves_other_configuration_alone(tmp_path) -> None:
    """This is why the endpoint is narrow rather than reusing update_node,
    which rebuilds a node from the payload and blanks whatever was omitted.
    Losing a node's media roots or signing key would 404 or 403 every stream
    it serves."""
    service, _ = _service(tmp_path)
    service.update_node_pool("n1", {"enabled": False})
    node = service.node("n1")
    assert node.enabled is False
    assert [p.emby_prefix for p in node.pools] == ["/media"]
    assert node.sign_secret == "s1"
    assert node.base_url == "https://n1.example"
    assert node.capacity == 50
    assert node.bandwidth_mbps == 500


@pytest.mark.parametrize("payload", [
    {"weight": 0},
    {"weight": -5},
    {"weight": "abc"},
    {"capacity": 0},
    {"capacity": 200000},
    {"bandwidth_mbps": -1},
    {"bandwidth_mbps": "fast"},
])
def test_illegal_values_are_refused(tmp_path, payload) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigError):
        service.update_node_pool("n1", payload)


def test_a_refused_edit_changes_nothing(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigError):
        service.update_node_pool("n1", {"weight": -1})
    assert service.node("n1").capacity == 50


def test_unknown_node_raises(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(KeyError):
        service.update_node_pool("nope", {"enabled": False})


def test_bandwidth_zero_is_allowed(tmp_path) -> None:
    """0 means 'ceiling unknown' and restores stream-count-only load, which is
    a real choice rather than a missing value."""
    service, _ = _service(tmp_path)
    out = service.update_node_pool("n1", {"bandwidth_mbps": 0})
    assert out["node"]["bandwidth_mbps"] == 0


# ---------------------------------------------------------------------------
# the change reaches the running scheduler
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disabling_a_node_removes_it_from_dispatch(tmp_path) -> None:
    service, _ = _service(tmp_path)
    scheduler = Scheduler(service.nodes(), StubProbe(), policy="least-load")
    service.bind_scheduler(scheduler)
    await scheduler.refresh()
    assert {s["name"] for s in scheduler.snapshot() if s["available"]} == {"n1", "n2"}

    service.update_node_pool("n1", {"enabled": False})
    await scheduler.refresh()

    # Positive: the survivor still gets picked, every time.
    for _ in range(20):
        assert scheduler.pick(record=False, context="/media/x.mkv").node.name == "n2"
    # Negative: the disabled node is not merely deprioritised.
    assert not [s for s in scheduler.snapshot()
                if s["name"] == "n1" and s["available"]]


@pytest.mark.asyncio
async def test_re_enabling_puts_a_node_back(tmp_path) -> None:
    service, _ = _service(tmp_path)
    scheduler = Scheduler(service.nodes(), StubProbe())
    service.bind_scheduler(scheduler)
    service.update_node_pool("n1", {"enabled": False})
    await scheduler.refresh()
    service.update_node_pool("n1", {"enabled": True})
    await scheduler.refresh()
    assert {s["name"] for s in scheduler.snapshot() if s["available"]} == {"n1", "n2"}


@pytest.mark.asyncio
async def test_disabling_every_node_dispatches_nowhere(tmp_path) -> None:
    """Rather than silently falling back to a disabled node: playback then
    fails over to the origin, which is the documented safe behaviour."""
    service, _ = _service(tmp_path)
    scheduler = Scheduler(service.nodes(), StubProbe())
    service.bind_scheduler(scheduler)
    service.update_node_pool("n1", {"enabled": False})
    service.update_node_pool("n2", {"enabled": False})
    await scheduler.refresh()
    assert scheduler.pick(record=False, context="/media/x.mkv") is None


@pytest.mark.asyncio
async def test_capacity_change_takes_effect_without_restart(tmp_path) -> None:
    service, _ = _service(tmp_path)
    scheduler = Scheduler(service.nodes(), StubProbe(streams=25))
    service.bind_scheduler(scheduler)
    await scheduler.refresh()
    before = {s["name"]: s["utilisation"] for s in scheduler.snapshot()}
    assert before["n1"] == 0.5          # 25 / 50

    service.update_node_pool("n1", {"weight": 100})
    await scheduler.refresh()
    after = {s["name"]: s["utilisation"] for s in scheduler.snapshot()}
    assert after["n1"] == 0.25          # 25 / 100


@pytest.mark.asyncio
async def test_probe_history_survives_an_edit(tmp_path) -> None:
    """reconfigure() keeps live state; rebuilding it would blank every node's
    health the moment anyone touched a slider."""
    service, _ = _service(tmp_path)
    scheduler = Scheduler(service.nodes(), StubProbe())
    service.bind_scheduler(scheduler)
    await scheduler.refresh()
    service.update_node_pool("n1", {"bandwidth_mbps": 900})
    n1 = next(s for s in scheduler.snapshot() if s["name"] == "n1")
    assert n1["ok"] is True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_pool_endpoints_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/nodes/pool").status_code == 401
        assert client.put("/api/nodes/mock-a/pool", json={}).status_code == 401


def test_pool_overview_joins_config_with_live_state() -> None:
    with TestClient(app) as client:
        rows = client.get("/api/nodes/pool", headers=_basic()).json()
        assert rows
        row = rows[0]
        for key in ("name", "enabled", "capacity", "bandwidth_mbps",
                    "ok", "active_streams", "egress_mbps", "share",
                    "last_probe_ts"):
            assert key in row, key


def test_pool_shares_sum_to_one_over_enabled_nodes() -> None:
    with TestClient(app) as client:
        rows = client.get("/api/nodes/pool", headers=_basic()).json()
        total = sum(r["share"] for r in rows if r["enabled"])
        assert total == pytest.approx(1.0, abs=0.01)


def test_disabled_node_reports_zero_share() -> None:
    """Counting a disabled node in the denominator would make every other
    node's share read too small."""
    with TestClient(app) as client:
        client.put("/api/nodes/mock-a/pool", headers=_basic(),
                   json={"enabled": False})
        rows = {r["name"]: r for r in
                client.get("/api/nodes/pool", headers=_basic()).json()}
        assert rows["mock-a"]["share"] == 0.0
        assert rows["mock-b"]["share"] == pytest.approx(1.0, abs=0.01)


def test_pool_edit_is_audited() -> None:
    with TestClient(app) as client:
        client.put("/api/nodes/mock-a/pool", headers=_basic(),
                   json={"enabled": False, "weight": 42})
        entries = client.get("/api/audit?limit=50",
                             headers=_basic()).json()["items"]
        rows = [e for e in entries
                if e["action"] == "node.pool" and e["subject"] == "mock-a"]
        assert rows
        assert "42" in rows[0]["detail"]


def test_a_no_op_edit_is_not_audited() -> None:
    """An audit log full of edits that changed nothing is one nobody reads."""
    with TestClient(app) as client:
        before = client.get("/api/audit?limit=200",
                            headers=_basic()).json()["total"]
        current = {r["name"]: r for r in
                   client.get("/api/nodes/pool", headers=_basic()).json()}
        client.put("/api/nodes/mock-a/pool", headers=_basic(),
                   json={"enabled": current["mock-a"]["enabled"],
                         "weight": current["mock-a"]["capacity"]})
        after = client.get("/api/audit?limit=200",
                           headers=_basic()).json()["total"]
        assert after == before


def test_pool_edit_rejects_bad_values_over_http() -> None:
    with TestClient(app) as client:
        response = client.put("/api/nodes/mock-a/pool", headers=_basic(),
                              json={"weight": -3})
        assert response.status_code == 422


def test_pool_edit_unknown_node_is_404() -> None:
    with TestClient(app) as client:
        assert client.put("/api/nodes/nope/pool", headers=_basic(),
                          json={"enabled": True}).status_code == 404


def test_pool_page_is_served_and_reachable() -> None:
    with TestClient(app) as client:
        index = client.get("/", headers=_basic()).text
        assert "/static/nodepool.js" in index
        assert client.get("/static/nodepool.js", headers=_basic()).status_code == 200
