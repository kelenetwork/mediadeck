"""Points are a currency, so the interesting tests are the ones about money
that must not appear or disappear.

Four properties carry the whole feature:

- **The balance is the ledger.** Not a column that happens to agree with it.
  If these ever diverge there is no way to tell which one is lying, so the
  balance is derived and the rows are the only truth.
- **Nobody goes into debt.** A member who cannot afford something is refused
  before anything is written. A negative balance is state the panel would then
  have to display, explain and collect.
- **A transfer is atomic.** Half a transfer -- taken from one side, never
  given to the other -- is the failure that ends trust in the feature, so it
  is pinned from the failing side as well as the happy one.
- **Nobody transfers to themselves.** It looks harmless and it is: it is also
  free money the moment a fee or a daily cap is involved.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.points import PointsService, reason_label

ADMIN = ("admin", "change-me")


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "points.db")


@pytest.fixture()
def points(db: Database) -> PointsService:
    return PointsService(db)


def _member(db: Database, user_id: str, username: str = "") -> None:
    db.execute(
        "INSERT INTO members(emby_user_id,username,created_at,updated_at) "
        "VALUES(?,?,0,0)", (user_id, username or user_id))


# -- balance is derived, never stored ----------------------------------------

def test_balance_is_the_sum_of_the_ledger(points: PointsService) -> None:
    assert points.balance("u1") == 0
    points.add("u1", 100, "checkin")
    points.add("u1", 50, "checkin")
    points.add("u1", -30, "shop.redeem")
    assert points.balance("u1") == 120


def test_balance_ignores_other_members(points: PointsService) -> None:
    points.add("u1", 100, "checkin")
    points.add("u2", 500, "checkin")
    assert points.balance("u1") == 100
    assert points.balance("u2") == 500


def test_a_member_with_no_rows_has_no_points(points: PointsService) -> None:
    assert points.balance("never-seen") == 0
    assert points.ledger("never-seen") == []


def test_balances_answers_the_whole_page_in_one_query(
        points: PointsService) -> None:
    points.add("u1", 10, "checkin")
    points.add("u2", 20, "checkin")
    everyone = points.balances()
    assert everyone == {"u1": 10, "u2": 20}
    # An explicit list gets zeros for members who have never earned anything,
    # because the member table shows every row whether or not it has points.
    asked = points.balances(["u1", "u3"])
    assert asked == {"u1": 10, "u3": 0}


# -- add, both directions ----------------------------------------------------

def test_add_returns_the_new_balance_and_records_the_witness(
        points: PointsService) -> None:
    assert points.add("u1", 100, "checkin") == 100
    assert points.add("u1", -40, "shop.redeem") == 60
    rows = points.ledger("u1")
    assert [r["delta"] for r in rows] == [-40, 100]
    # balance_after is a witness for audits, not the answer to "how much".
    assert [r["balance_after"] for r in rows] == [60, 100]


def test_spending_more_than_you_have_is_refused_and_writes_nothing(
        points: PointsService) -> None:
    points.add("u1", 50, "checkin")
    with pytest.raises(ValueError, match="积分不足"):
        points.add("u1", -51, "shop.redeem")
    assert points.balance("u1") == 50
    assert len(points.ledger("u1")) == 1


def test_spending_exactly_the_balance_is_allowed(points: PointsService) -> None:
    """The boundary is the interesting half: 0 is a legal balance, -1 is not."""
    points.add("u1", 50, "checkin")
    assert points.add("u1", -50, "shop.redeem") == 0


def test_a_zero_delta_is_refused_as_a_bug_not_stored_as_history(
        points: PointsService) -> None:
    with pytest.raises(ValueError):
        points.add("u1", 0, "checkin")
    assert points.ledger("u1") == []


def test_a_member_id_is_required(points: PointsService) -> None:
    with pytest.raises(ValueError):
        points.add("", 10, "checkin")


def test_the_ledger_records_why_and_who(points: PointsService) -> None:
    points.add("u1", 10, "checkin", ref="2026-09-05", actor="checkin")
    row = points.ledger("u1")[0]
    assert row["reason"] == "checkin"
    assert row["ref"] == "2026-09-05"
    assert row["actor"] == "checkin"
    assert row["reason_label"] == "每日签到"


def test_unknown_reasons_still_get_a_label(points: PointsService) -> None:
    assert reason_label("something.custom") == "something.custom"
    assert reason_label("") == "其他"


# -- transfer ----------------------------------------------------------------

def test_a_transfer_moves_exactly_what_it_took(points: PointsService) -> None:
    points.add("giver", 100, "checkin")
    result = points.transfer("giver", "taker", 40, actor="member")
    assert result["from_balance"] == 60
    assert result["to_balance"] == 40
    assert points.balance("giver") == 60
    assert points.balance("taker") == 40


def test_both_halves_of_a_transfer_reference_each_other(
        points: PointsService) -> None:
    """Either row alone is unexplainable; the pair is an audit trail."""
    points.add("giver", 100, "checkin")
    points.transfer("giver", "taker", 40)
    out = points.ledger("giver")[0]
    incoming = points.ledger("taker")[0]
    assert out["reason"] == "transfer.out" and out["ref"] == "to:taker"
    assert incoming["reason"] == "transfer.in" and incoming["ref"] == "from:giver"


def test_a_transfer_that_cannot_be_afforded_changes_nothing_on_either_side(
        points: PointsService) -> None:
    """The atomicity test: the debit succeeds only if the credit does too."""
    points.add("giver", 30, "checkin")
    points.add("taker", 5, "checkin")
    with pytest.raises(ValueError, match="积分不足"):
        points.transfer("giver", "taker", 40)
    assert points.balance("giver") == 30
    assert points.balance("taker") == 5
    assert len(points.ledger("giver")) == 1
    assert len(points.ledger("taker")) == 1


def test_a_failure_between_the_two_halves_rolls_back_the_first(
        points: PointsService, monkeypatch) -> None:
    """A debit that lands without its credit is the failure to prevent.

    The credit is sabotaged mid-transaction, which is the only way to reach
    the state where one half is written and the other is not.
    """
    points.add("giver", 100, "checkin")
    original = points._apply
    calls: list[int] = []

    def explode(conn: Any, user_id: str, delta: int, *args: Any,
                **kwargs: Any) -> int:
        calls.append(delta)
        if delta > 0:  # the credit half
            raise RuntimeError("storage went away")
        return original(conn, user_id, delta, *args, **kwargs)

    monkeypatch.setattr(points, "_apply", explode)
    with pytest.raises(RuntimeError):
        points.transfer("giver", "taker", 40)

    assert len(calls) == 2, "both halves should have been attempted"
    # The debit was written and then rolled back with the transaction.
    assert points.balance("giver") == 100
    assert points.balance("taker") == 0
    assert len(points.ledger("giver")) == 1


def test_transferring_to_yourself_is_refused(points: PointsService) -> None:
    points.add("u1", 100, "checkin")
    with pytest.raises(ValueError, match="自己"):
        points.transfer("u1", "u1", 10)
    assert points.balance("u1") == 100


def test_non_positive_amounts_are_refused(points: PointsService) -> None:
    points.add("u1", 100, "checkin")
    for bad in (0, -5):
        with pytest.raises(ValueError):
            points.transfer("u1", "u2", bad)
    assert points.balance("u2") == 0


def test_a_missing_counterparty_is_refused(points: PointsService) -> None:
    points.add("u1", 100, "checkin")
    with pytest.raises(ValueError):
        points.transfer("u1", "", 10)


def test_a_fee_is_destroyed_not_paid_to_anyone(points: PointsService) -> None:
    """The sender pays 100, the recipient gets 90, and 10 ceases to exist."""
    points.add("giver", 100, "checkin")
    result = points.transfer("giver", "taker", 100, fee=10)
    assert result["fee"] == 10
    assert result["received"] == 90
    assert points.balance("giver") == 0
    assert points.balance("taker") == 90


def test_a_fee_that_eats_the_whole_transfer_is_refused(
        points: PointsService) -> None:
    points.add("giver", 100, "checkin")
    with pytest.raises(ValueError, match="手续费"):
        points.transfer("giver", "taker", 10, fee=10)
    assert points.balance("giver") == 100


# -- ranking and reporting ---------------------------------------------------

def test_top_ranks_by_balance_richest_first(db: Database,
                                            points: PointsService) -> None:
    for uid, name, amount in (("u1", "alice", 50), ("u2", "bob", 300),
                              ("u3", "carol", 120)):
        _member(db, uid, name)
        points.add(uid, amount, "checkin")
    ranked = points.top(limit=10)
    assert [r["username"] for r in ranked] == ["bob", "carol", "alice"]
    assert [r["balance"] for r in ranked] == [300, 120, 50]


def test_top_leaves_out_members_who_have_nothing(db: Database,
                                                 points: PointsService) -> None:
    """A leaderboard of people who never earned anything is noise."""
    _member(db, "rich", "rich")
    _member(db, "broke", "broke")
    points.add("rich", 10, "checkin")
    points.add("broke", 10, "checkin")
    points.add("broke", -10, "shop.redeem")
    assert [r["emby_user_id"] for r in points.top()] == ["rich"]


def test_spent_since_counts_only_outgoing_rows_of_that_reason(
        points: PointsService) -> None:
    points.add("u1", 1000, "checkin")
    points.transfer("u1", "u2", 100)
    points.transfer("u1", "u2", 50)
    points.add("u1", -20, "shop.redeem")
    assert points.spent_since("u1", "transfer.out", 0) == 150
    # A window that starts in the future sees nothing.
    assert points.spent_since("u1", "transfer.out", 4_000_000_000) == 0


def test_the_ledger_is_newest_first_and_bounded(points: PointsService) -> None:
    for i in range(1, 31):
        points.add("u1", i, "checkin")
    rows = points.ledger("u1", limit=5)
    assert len(rows) == 5
    assert [r["delta"] for r in rows] == [30, 29, 28, 27, 26]


# -- API ---------------------------------------------------------------------

def _enroll(client: TestClient, user_id: str = "u1") -> str:
    """Enrol a mock Emby user so member-scoped routes have a subject."""
    client.put(f"/api/members/{user_id}", auth=ADMIN,
               json={"group_id": "standard", "username": "demo-user-1"})
    return user_id


def test_the_points_api_reports_balance_and_history() -> None:
    with TestClient(app) as client:
        uid = _enroll(client)
        body = client.get(f"/api/points/{uid}", auth=ADMIN).json()
        assert body["balance"] == 0
        assert body["ledger"] == []

        adjusted = client.post(f"/api/points/{uid}/adjust", auth=ADMIN,
                               json={"delta": 250, "reason": "补偿"}).json()
        assert adjusted["balance"] == 250
        after = client.get(f"/api/points/{uid}", auth=ADMIN).json()
        assert after["balance"] == 250
        assert after["ledger"][0]["reason"] == "admin.adjust"
        assert after["ledger"][0]["ref"] == "补偿"


def test_an_operator_can_take_points_away_but_not_below_zero() -> None:
    with TestClient(app) as client:
        uid = _enroll(client)
        client.post(f"/api/points/{uid}/adjust", auth=ADMIN, json={"delta": 100})
        assert client.post(f"/api/points/{uid}/adjust", auth=ADMIN,
                           json={"delta": -40}).json()["balance"] == 60
        refused = client.post(f"/api/points/{uid}/adjust", auth=ADMIN,
                              json={"delta": -100})
        assert refused.status_code == 400
        assert client.get(f"/api/points/{uid}", auth=ADMIN).json()["balance"] == 60


def test_adjusting_points_is_audited_because_it_creates_value_by_hand() -> None:
    with TestClient(app) as client:
        uid = _enroll(client)
        client.post(f"/api/points/{uid}/adjust", auth=ADMIN,
                    json={"delta": 70, "reason": "活动奖励"})
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "points.adjust" in body
        assert "活动奖励" in body


def test_bad_adjustments_and_unknown_members_are_refused() -> None:
    with TestClient(app) as client:
        uid = _enroll(client)
        assert client.post(f"/api/points/{uid}/adjust", auth=ADMIN,
                           json={"delta": "lots"}).status_code == 400
        assert client.post("/api/points/nobody/adjust", auth=ADMIN,
                           json={"delta": 10}).status_code == 404
        assert client.get("/api/points/nobody", auth=ADMIN).status_code == 404


def test_the_points_api_needs_authentication() -> None:
    with TestClient(app) as client:
        assert client.get("/api/points/top").status_code == 401
        assert client.get("/api/points/anyone").status_code == 401
        assert client.post("/api/points/anyone/adjust",
                           json={"delta": 1}).status_code == 401


def test_the_member_list_carries_balances_without_asking_per_row() -> None:
    with TestClient(app) as client:
        uid = _enroll(client)
        client.post(f"/api/points/{uid}/adjust", auth=ADMIN, json={"delta": 42})
        listing = client.get("/api/members", auth=ADMIN).json()
        row = next(m for m in listing["members"] if m["emby_user_id"] == uid)
        assert row["points"] == 42
        detail = client.get(f"/api/members/{uid}", auth=ADMIN).json()
        assert detail["points"] == 42
        assert detail["points_ledger"][0]["delta"] == 42
