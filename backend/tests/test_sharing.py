"""Sharing detection is judged by what it refuses to report.

Finding concurrent sessions is easy; the value is in not crying wolf. A
household has a TV and a phone. A phone walks out of wifi onto mobile data
mid-episode. If either reads as "account shared", the operator stops reading
the alerts, which is worse than having none.

So most of these cases assert that nothing is reported.
"""
from __future__ import annotations

from app.modules.sharing import SharingDetector, network_key


class _FakeDb:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.rows.append(params)

    def query(self, sql: str, params: tuple = ()):
        return []


def _session(user_id: str, ip: str) -> dict:
    return {"user_id": user_id, "remote_ip": ip, "seconds": 10}


# -- network identity -------------------------------------------------------

def test_addresses_in_one_household_collapse_to_one_network() -> None:
    assert network_key("192.168.1.7:52344") == network_key("192.168.1.99:1")


def test_distant_addresses_stay_distinct() -> None:
    assert network_key("203.0.113.9:80") != network_key("198.51.100.9:80")


def test_ipv6_is_grouped_by_site_prefix() -> None:
    a = network_key("[2001:db8:1:2::5]:443")
    b = network_key("[2001:db8:1:9::7]:443")
    assert a == b == "2001:db8:1::/48"


def test_unparseable_address_is_kept_not_dropped() -> None:
    """An address the panel cannot classify is still evidence of difference."""
    assert network_key("garbage") == "garbage"
    assert network_key("") == ""


# -- what must NOT be reported ---------------------------------------------

def test_two_devices_in_one_home_are_not_sharing() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=60)
    live = [_session("u1", "192.168.1.10:1"), _session("u1", "192.168.1.55:2")]
    det.observe(live, now=0)
    assert det.observe(live, now=600) == []


def test_a_brief_handover_is_not_sharing() -> None:
    """Wifi to mobile: the old session lingers a few seconds past the new one."""
    det = SharingDetector(_FakeDb(), min_seconds=90)
    det.observe([_session("u1", "203.0.113.5:1")], now=0)
    # Both visible for 20s during the switch, then only the new one.
    det.observe([_session("u1", "203.0.113.5:1"),
                 _session("u1", "198.51.100.5:1")], now=20)
    assert det.observe([_session("u1", "198.51.100.5:1")], now=40) == []


def test_a_single_network_is_never_reported_however_long() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=60)
    live = [_session("u1", "203.0.113.5:1")]
    det.observe(live, now=0)
    assert det.observe(live, now=100000) == []


def test_sessions_without_an_address_are_ignored() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=1)
    det.observe([_session("u1", ""), _session("u1", "203.0.113.5:1")], now=0)
    assert det.observe([_session("u1", ""), _session("u1", "203.0.113.5:1")],
                       now=100) == []


def test_two_different_members_are_not_each_other_s_evidence() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=1)
    live = [_session("u1", "203.0.113.5:1"), _session("u2", "198.51.100.5:1")]
    det.observe(live, now=0)
    assert det.observe(live, now=100) == []


# -- what MUST be reported --------------------------------------------------

def test_two_established_networks_are_reported() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=60)
    live = [_session("u1", "203.0.113.5:1"), _session("u1", "198.51.100.5:1")]
    det.observe(live, now=0)
    found = det.observe(live, now=120)
    assert len(found) == 1
    assert found[0]["user_id"] == "u1"
    assert found[0]["network_count"] == 2
    assert len(found[0]["networks"]) == 2


def test_the_same_account_is_not_reported_every_tick() -> None:
    """Repeating the same finding each sample would bury the real ones."""
    det = SharingDetector(_FakeDb(), min_seconds=60, cooldown=3600)
    live = [_session("u1", "203.0.113.5:1"), _session("u1", "198.51.100.5:1")]
    det.observe(live, now=0)
    assert det.observe(live, now=120)
    assert det.observe(live, now=180) == []
    assert det.observe(live, now=200) == []


def test_it_reports_again_after_the_cooldown() -> None:
    det = SharingDetector(_FakeDb(), min_seconds=60, cooldown=600)
    live = [_session("u1", "203.0.113.5:1"), _session("u1", "198.51.100.5:1")]
    det.observe(live, now=0)
    assert det.observe(live, now=120)
    assert det.observe(live, now=1000)


def test_a_network_that_stops_playing_is_forgotten() -> None:
    """Someone who moves house must not accumulate places forever."""
    det = SharingDetector(_FakeDb(), min_seconds=60)
    det.observe([_session("u1", "203.0.113.5:1")], now=0)
    det.observe([_session("u1", "198.51.100.5:1")], now=200)
    # Only ever one network playing at a time.
    assert det.observe([_session("u1", "198.51.100.5:1")], now=400) == []
    assert det.status()["tracked_accounts"] == 1


# -- persistence ------------------------------------------------------------

def test_a_finding_is_recorded_with_the_member_name() -> None:
    db = _FakeDb()
    det = SharingDetector(db, min_seconds=1)
    live = [_session("u1", "203.0.113.5:1"), _session("u1", "198.51.100.5:1")]
    det.observe(live, now=0)
    found = det.observe(live, now=100)
    det.record(found[0], "someone")
    assert db.rows, "the finding should have been persisted"
    assert db.rows[0][0] == "u1"
    assert db.rows[0][1] == "someone"


def test_detection_never_suspends_anyone() -> None:
    """The module exposes no way to act on a finding, only to record it.

    Locking out a paying member over a VPN reconnect is the failure mode this
    guards against, so the decision stays with a person.
    """
    for forbidden in ("suspend", "block", "kick", "terminate", "enforce"):
        assert not hasattr(SharingDetector, forbidden)
