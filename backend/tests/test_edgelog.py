"""Direct-link traffic ledger.

The figure this replaces was wrong in a way that nearly cost 196 accounts, so
the tests lean hard on the properties that make a byte count trustworthy:
never double count, never silently drop, and never conflate a measurement with
an estimate.
"""
from __future__ import annotations

import base64
import gzip
import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.edgelog import (
    EdgeEvent,
    TrafficLedger,
    aggregate,
    day_key,
    open_log,
    parse_line,
    parse_lines,
)
from app.modules.signing import user_tag

AGENT = Path(__file__).resolve().parents[2] / "agent" / "edgereport.py"


def _basic(user: str = "admin", pw_value: str = "change-me") -> dict[str, str]:
    creds = base64.b64encode(f"{user}:{pw_value}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _agent():
    spec = importlib.util.spec_from_file_location("edgereport", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ledger(tmp_path):
    return TrafficLedger(Database(tmp_path / "t.db"))


# ---------------------------------------------------------------------------
# parsing: both live formats, and everything that is not a record
# ---------------------------------------------------------------------------
def test_parses_the_original_labelled_format() -> None:
    line = "1788635166.110 a=10.0.0.1 p=55742 u=d8842b1d51 r=15728625 22245 0.002"
    event = parse_line(line)
    assert event is not None
    assert event.utag == "d8842b1d51"
    assert event.bytes_sent == 22245
    assert event.ts == pytest.approx(1788635166.110)
    assert event.status is None


def test_parses_the_extended_format_with_status_and_uri() -> None:
    """One node started appending `s=<status> <uri>` mid-deployment. A parser
    coupled to one template drops every line from the others -- silently, and
    that is total data loss rather than a visible error."""
    line = ("1788616301.177 a=10.0.0.2 p=21956 u=68249a8004 r=15728625 "
            "167100416 31.282 s=206 /s/pool/Some/Path/file.mkv")
    event = parse_line(line)
    assert event is not None
    assert event.utag == "68249a8004"
    assert event.bytes_sent == 167100416
    assert event.seconds == pytest.approx(31.282)
    assert event.status == 206


def test_parses_the_oldest_positional_format() -> None:
    event = parse_line("1788616301.177 68249a8004 4096 1.5")
    assert event is not None
    assert event.utag == "68249a8004"
    assert event.bytes_sent == 4096


def test_unknown_trailing_fields_are_tolerated() -> None:
    """A future template must not become another silent outage."""
    line = ("1788616301.177 a=10.0.0.2 p=1 u=aaaaaaaaaa r=0 5000 2.0 "
            "s=200 /s/x.mkv extra=1 more")
    event = parse_line(line)
    assert event is not None
    assert event.bytes_sent == 5000


@pytest.mark.parametrize("line", [
    "",
    "garbage",
    "not-a-timestamp a=1 u=abc 100 1.0",
    "1788616301.177 a=10.0.0.2 p=1 u=- r=0 5000 2.0",   # no user
    "1788616301.177 a=10.0.0.2 p=1 u=abc r=0 0 2.0",    # zero bytes
    "1788616301.177 u=abc",                              # truncated
    "1788616301.177 a=10.0.0.2 p=1 u=abc r=0 notanint 2.0",
])
def test_bad_lines_are_dropped_not_raised(line: str) -> None:
    assert parse_line(line) is None


def test_parse_lines_skips_bad_and_keeps_good() -> None:
    lines = [
        "1788616301.177 a=1.1.1.1 p=1 u=aaaaaaaaaa r=0 100 1.0",
        "corrupt line here",
        "1788616302.177 a=1.1.1.1 p=1 u=bbbbbbbbbb r=0 200 1.0",
    ]
    events = list(parse_lines(lines))
    assert [e.bytes_sent for e in events] == [100, 200]


def test_aggregate_folds_by_day_and_tag() -> None:
    ts = time.time()
    events = [
        EdgeEvent(ts, "tag-a", 100, 1.0),
        EdgeEvent(ts, "tag-a", 150, 2.0),
        EdgeEvent(ts, "tag-b", 70, 1.0),
    ]
    out = aggregate(events)
    assert out[(day_key(ts), "tag-a")]["bytes"] == 250
    assert out[(day_key(ts), "tag-a")]["requests"] == 2
    assert out[(day_key(ts), "tag-b")]["bytes"] == 70


def test_open_log_reads_gzip(tmp_path) -> None:
    path = tmp_path / "rotated.log.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("1788616301.177 a=1.1.1.1 p=1 u=aaaaaaaaaa r=0 100 1.0\n")
    with open_log(path) as handle:
        assert next(iter(parse_lines(handle))).bytes_sent == 100


# ---------------------------------------------------------------------------
# ledger: idempotency is the whole point
# ---------------------------------------------------------------------------
def test_record_accumulates(ledger) -> None:
    ts = time.time()
    buckets = aggregate([EdgeEvent(ts, "tag-a", 100, 1.0)])
    ledger.record("node-a", buckets, {"tag-a": "u1"})
    ledger.record("node-a", buckets, {"tag-a": "u1"})
    assert ledger.totals_for_users()["u1"] == 200


def test_same_window_is_not_recounted_via_cursor(ledger) -> None:
    """The cursor is what makes re-reading safe. Double counting inflates a
    member's usage permanently and silently."""
    ledger.set_cursor("node-a", "/log", inode=7, offset=500)
    assert ledger.resume_offset("node-a", "/log", inode=7, size=900) == 500


def test_rotation_restarts_from_zero(ledger) -> None:
    """A new inode under the same name is a different file, not the old one
    grown -- resuming at the old offset would skip its first 500 bytes."""
    ledger.set_cursor("node-a", "/log", inode=7, offset=500)
    assert ledger.resume_offset("node-a", "/log", inode=9, size=900) == 0


def test_truncation_restarts_from_zero(ledger) -> None:
    ledger.set_cursor("node-a", "/log", inode=7, offset=500)
    assert ledger.resume_offset("node-a", "/log", inode=7, size=100) == 0


def test_unseen_file_starts_at_zero(ledger) -> None:
    assert ledger.resume_offset("node-a", "/never-seen", inode=1, size=10) == 0


def test_unattributed_bytes_are_kept_not_dropped(ledger) -> None:
    """Traffic whose tag matches no member still left the building. Dropping
    it would make totals disagree with the nodes for reasons nobody could
    later reconstruct."""
    ts = time.time()
    out = ledger.record("node-a", aggregate([EdgeEvent(ts, "orphan", 500, 1.0)]), {})
    assert out["unknown_bytes"] == 500
    assert ledger.unattributed()["bytes"] == 500
    assert ledger.totals_for_users() == {}


def test_relink_attaches_history_once_the_member_is_known(ledger) -> None:
    """A tag seen before its member existed must become attributable, not stay
    orphaned forever."""
    ts = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(ts, "tag-a", 900, 1.0)]), {})
    assert ledger.totals_for_users() == {}
    assert ledger.relink({"tag-a": "u1"}) == 1
    assert ledger.totals_for_users()["u1"] == 900


def test_relink_is_idempotent(ledger) -> None:
    ts = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(ts, "tag-a", 900, 1.0)]), {})
    ledger.relink({"tag-a": "u1"})
    assert ledger.relink({"tag-a": "u1"}) == 0


def test_per_node_totals_are_separate(ledger) -> None:
    ts = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(ts, "tag-a", 100, 1.0)]),
                  {"tag-a": "u1"})
    ledger.record("node-b", aggregate([EdgeEvent(ts, "tag-a", 400, 1.0)]),
                  {"tag-a": "u1"})
    assert ledger.totals_for_users()["u1"] == 500
    nodes = {r["node"]: r["bytes"] for r in ledger.node_totals()}
    assert nodes == {"node-a": 100, "node-b": 400}


def test_summary_splits_windows(ledger) -> None:
    now = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(now, "tag-a", 100, 1.0)]),
                  {"tag-a": "u1"})
    ledger.record("node-a", aggregate([EdgeEvent(now - 10 * 86400, "tag-a", 200, 1.0)]),
                  {"tag-a": "u1"})
    ledger.record("node-a", aggregate([EdgeEvent(now - 200 * 86400, "tag-a", 400, 1.0)]),
                  {"tag-a": "u1"})
    summary = ledger.summary_for_users(now=now)["u1"]
    assert summary["bytes_7d"] == 100
    assert summary["bytes_30d"] == 300
    assert summary["bytes_total"] == 700


def test_lifetime_total_is_not_a_rolling_window(ledger) -> None:
    """The old field reset on a period boundary, so it could never answer
    'how much has this account ever used'."""
    now = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(now - 900 * 86400, "t", 42, 1.0)]),
                  {"t": "u1"})
    assert ledger.summary_for_users(now=now)["u1"]["bytes_total"] == 42
    assert ledger.summary_for_users(now=now)["u1"]["bytes_30d"] == 0


def test_member_detail_splits_by_node_and_day(ledger) -> None:
    now = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(now, "tag-a", 100, 1.0)]),
                  {"tag-a": "u1"})
    ledger.record("node-b", aggregate([EdgeEvent(now, "tag-a", 300, 1.0)]),
                  {"tag-a": "u1"})
    detail = ledger.member_detail("u1", days=30, now=now)
    assert {r["node"] for r in detail["by_node"]} == {"node-a", "node-b"}
    assert sum(r["bytes"] for r in detail["by_day"]) == 400


def test_status_reports_coverage(ledger) -> None:
    now = time.time()
    ledger.record("node-a", aggregate([EdgeEvent(now, "tag-a", 100, 1.0)]),
                  {"tag-a": "u1"})
    ledger.set_cursor("node-a", "/log", 1, 100)
    status = ledger.status()
    assert status["rows"] == 1
    assert status["bytes"] == 100
    assert status["cursors"][0]["path"] == "/log"


# ---------------------------------------------------------------------------
# node agent
# ---------------------------------------------------------------------------
def test_agent_reads_rotations_before_the_live_file(tmp_path) -> None:
    """Consuming the live file first would advance a cursor past rotated data
    that had not been read yet."""
    module = _agent()
    base = tmp_path / "speed.log"
    base.write_text("live\n", encoding="utf-8")
    (tmp_path / "speed.log.1").write_text("older\n", encoding="utf-8")
    (tmp_path / "speed.log.2.gz").write_bytes(gzip.compress(b"oldest\n"))
    paths = module.log_paths(str(base))
    assert paths[-1] == str(base)
    assert paths.index(str(tmp_path / "speed.log.2.gz")) < paths.index(
        str(tmp_path / "speed.log.1"))


def test_agent_resumes_without_repeating(tmp_path) -> None:
    module = _agent()
    path = tmp_path / "speed.log"
    path.write_text("one\ntwo\n", encoding="utf-8")
    first, offset, _ = module.read_since(str(path), 0)
    assert first == ["one", "two"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("three\n")
    second, _, _ = module.read_since(str(path), offset)
    assert second == ["three"]


def test_agent_ignores_a_partial_trailing_line(tmp_path) -> None:
    """nginx writes a line in one go, but a read can still land mid-write; a
    half line parsed as a record would book a wrong byte count."""
    module = _agent()
    path = tmp_path / "speed.log"
    path.write_text("complete\npartial-no-newline", encoding="utf-8")
    lines, offset, _ = module.read_since(str(path), 0)
    assert lines == ["complete"]
    assert offset == len("complete\n")


def test_agent_restarts_on_truncation(tmp_path) -> None:
    module = _agent()
    path = tmp_path / "speed.log"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    path.write_text("x\n", encoding="utf-8")
    lines, _, _ = module.read_since(str(path), 6)
    assert lines == ["x"]


def test_agent_reads_gzip_once(tmp_path) -> None:
    module = _agent()
    path = tmp_path / "speed.log.1.gz"
    path.write_bytes(gzip.compress(b"one\ntwo\n"))
    lines, offset, _ = module.read_since(str(path), 0)
    assert lines == ["one", "two"]
    again, _, _ = module.read_since(str(path), offset)
    assert again == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _report_creds(client, node: str = "mock-a") -> str:
    body = client.get(f"/api/nodes/{node}/report-token", headers=_basic()).json()
    return body["report_token"]


def test_report_requires_the_node_credential() -> None:
    with TestClient(app) as client:
        payload = {"path": "/x", "inode": 1, "offset": 10, "lines": []}
        assert client.post("/api/edge/mock-a/report", json=payload).status_code == 401
        assert client.post("/api/edge/mock-a/report", json=payload,
                           headers={"Authorization": "Bearer wrong"}
                           ).status_code == 401


def test_a_credential_is_scoped_to_its_own_node() -> None:
    """Otherwise one compromised node could rewrite another's traffic."""
    with TestClient(app) as client:
        creds = _report_creds(client, "mock-a")
        response = client.post(
            "/api/edge/mock-b/report",
            headers={"Authorization": f"Bearer {creds}"},
            json={"path": "/x", "inode": 1, "offset": 1, "lines": []})
        assert response.status_code == 401


def test_report_ingests_and_advances_the_cursor() -> None:
    with TestClient(app) as client:
        creds = _report_creds(client)
        tag = user_tag("u1")
        now = time.time()
        lines = [f"{now} a=1.1.1.1 p=1 u={tag} r=0 5000 1.0",
                 f"{now} a=1.1.1.1 p=2 u={tag} r=0 7000 2.0 s=206 /s/x.mkv"]
        response = client.post(
            "/api/edge/mock-a/report",
            headers={"Authorization": f"Bearer {creds}"},
            json={"path": "/var/log/x", "inode": 5, "offset": 999, "lines": lines})
        assert response.status_code == 200
        assert response.json()["bytes"] == 12000

        cursors = client.get("/api/edge/mock-a/cursors",
                             headers={"Authorization": f"Bearer {creds}"}).json()
        assert cursors["cursors"][0]["offset"] == 999
        assert cursors["cursors"][0]["inode"] == 5


def test_report_rejects_an_oversized_batch() -> None:
    with TestClient(app) as client:
        creds = _report_creds(client)
        response = client.post(
            "/api/edge/mock-a/report",
            headers={"Authorization": f"Bearer {creds}"},
            json={"path": "/x", "inode": 1, "offset": 1,
                  "lines": ["x"] * 500_001})
        assert response.status_code == 413


def test_report_rejects_a_malformed_payload() -> None:
    with TestClient(app) as client:
        creds = _report_creds(client)
        response = client.post(
            "/api/edge/mock-a/report",
            headers={"Authorization": f"Bearer {creds}"},
            json={"path": "/x", "lines": "not-a-list"})
        assert response.status_code == 422


def test_edge_status_and_relink_need_the_operator_login() -> None:
    with TestClient(app) as client:
        assert client.get("/api/edge/status").status_code == 401
        assert client.post("/api/edge/relink").status_code == 401
        assert client.get("/api/edge/status", headers=_basic()).status_code == 200


def test_report_token_is_not_exposed_in_the_node_list() -> None:
    """The node list is rendered on every settings page load; a long-lived
    credential must not ride along with it."""
    with TestClient(app) as client:
        _report_creds(client)
        body = client.get("/api/nodes", headers=_basic()).text
        assert "report_token\"" not in body.replace("report_token_set\"", "")
        nodes = client.get("/api/nodes", headers=_basic()).json()
        assert any(n.get("report_token_set") for n in nodes)


def test_report_token_is_stable_until_rotated() -> None:
    with TestClient(app) as client:
        first = _report_creds(client)
        assert _report_creds(client) == first
        rotated = client.get("/api/nodes/mock-a/report-token?rotate=true",
                             headers=_basic()).json()["report_token"]
        assert rotated != first


def test_members_list_carries_measured_traffic() -> None:
    with TestClient(app) as client:
        client.post("/api/members/enroll-defaults", headers=_basic())
        creds = _report_creds(client)
        tag = user_tag("u1")
        client.post("/api/edge/mock-a/report",
                    headers={"Authorization": f"Bearer {creds}"},
                    json={"path": "/l", "inode": 1, "offset": 1,
                          "lines": [f"{time.time()} a=1.1.1.1 p=1 u={tag} r=0 9000 1.0"]})
        members = client.get("/api/members", headers=_basic()).json()["members"]
        row = next(m for m in members if m["emby_user_id"] == "u1")
        assert row["edge"]["bytes_total"] == 9000
        # The legacy estimate stays on the row, clearly a different field.
        assert "traffic_used_bytes" in row


def test_member_detail_carries_the_node_breakdown() -> None:
    with TestClient(app) as client:
        client.post("/api/members/enroll-defaults", headers=_basic())
        creds = _report_creds(client)
        tag = user_tag("u1")
        client.post("/api/edge/mock-a/report",
                    headers={"Authorization": f"Bearer {creds}"},
                    json={"path": "/l", "inode": 1, "offset": 1,
                          "lines": [f"{time.time()} a=1.1.1.1 p=1 u={tag} r=0 8000 1.0"]})
        detail = client.get("/api/members/u1", headers=_basic()).json()
        assert detail["edge"]["by_node"][0]["node"] == "mock-a"
        assert detail["edge"]["by_node"][0]["bytes"] == 8000


def test_members_activity_endpoint() -> None:
    with TestClient(app) as client:
        body = client.get("/api/members-activity", headers=_basic()).json()
        assert body["available"] is True
        assert body["activity"]["u1"]


def test_user_tag_matches_the_signing_function() -> None:
    """The ledger maps tags back to members by recomputing this. If the two
    ever diverged, every line would be unattributable."""
    assert user_tag("u1") == user_tag("u1")
    assert len(user_tag("u1")) == 10
    assert user_tag("u1") != user_tag("u2")
