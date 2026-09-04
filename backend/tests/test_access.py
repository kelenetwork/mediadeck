"""Access rules sit on the playback path, so most of these assert non-denial.

The panel intercepts media requests. If a rule bug can deny playback, the panel
is worse than not being there at all. Every uncertain outcome must resolve to
"allow": an empty rule set, a pattern that stopped compiling, a malformed
address, an exception inside evaluation.

The second theme is that a refusal must leave a trace. A blocked request that
logs nothing is indistinguishable from a broken node, and the operator ends up
debugging the wrong machine.
"""
from __future__ import annotations

import time

from app.modules.access import AccessRules, strip_port, validate_pattern


class _FakeDb:
    """Minimal stand-in: rules live in a list, inserts are captured."""

    def __init__(self, rules: list[dict] | None = None) -> None:
        self._rules = rules or []
        self.blocks: list[tuple] = []

    def query(self, sql: str, params: tuple = ()):
        if "access_blocks" in sql:
            return []
        return list(self._rules)

    def one(self, sql: str, params: tuple = ()):
        rid = params[0] if params else None
        return {"x": 1} if any(r.get("id") == rid for r in self._rules) else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "access_blocks" in sql:
            self.blocks.append(params)
        elif sql.startswith("DELETE"):
            self._rules = [r for r in self._rules if r.get("id") != params[0]]
        elif sql.startswith("INSERT INTO access_rules"):
            self._rules.append({
                "id": len(self._rules) + 1, "kind": params[0],
                "pattern": params[1], "action": params[2], "note": params[3],
                "enabled": params[4], "created_at": params[5],
            })


def _rule(rid: int, kind: str, pattern: str, action: str = "deny",
          enabled: int = 1) -> dict:
    return {"id": rid, "kind": kind, "pattern": pattern, "action": action,
            "note": "", "enabled": enabled, "created_at": int(time.time())}


# -- fail open --------------------------------------------------------------

def test_no_rules_allows_everything() -> None:
    rules = AccessRules(_FakeDb([]))
    assert rules.evaluate(user_agent="anything", remote_ip="203.0.113.1")["allowed"]


def test_disabled_rules_do_not_deny() -> None:
    """Toggling a rule off must take effect without deleting it."""
    rules = AccessRules(_FakeDb([_rule(1, "client", "BadBot", enabled=0)]))
    assert rules.evaluate(user_agent="BadBot/1.0")["allowed"]


def test_a_missing_user_agent_is_not_a_match() -> None:
    """Absence of evidence must not be read as evidence of a match."""
    rules = AccessRules(_FakeDb([_rule(1, "client", ".*")]))
    assert rules.evaluate(user_agent="", remote_ip="203.0.113.1")["allowed"]


def test_a_malformed_address_does_not_deny() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "network", "203.0.113.0/24")]))
    assert rules.evaluate(user_agent="Emby", remote_ip="not-an-address")["allowed"]


def test_a_stored_pattern_that_stopped_compiling_does_not_deny() -> None:
    """Validation happens at save time; a bad stored rule must not break play."""
    rules = AccessRules(_FakeDb([_rule(1, "client", "([unclosed")]))
    assert rules.evaluate(user_agent="anything")["allowed"]


def test_an_exception_during_evaluation_still_allows() -> None:
    class _Exploding:
        def query(self, *a, **k):
            raise RuntimeError("db is down")

    verdict = AccessRules(_Exploding()).evaluate(user_agent="x", remote_ip="y")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "error"


def test_excluded_user_bypasses_every_rule() -> None:
    """One bad regex at 3am must not lock out the person who has to fix it."""
    rules = AccessRules(_FakeDb([_rule(1, "client", ".*"),
                                 _rule(2, "network", "0.0.0.0/0")]))
    verdict = rules.evaluate(user_agent="BadBot", remote_ip="203.0.113.9",
                             username="kele", excluded={"kele"})
    assert verdict["allowed"] is True
    assert verdict["reason"] == "excluded"


# -- deny where it is meant to ----------------------------------------------

def test_client_rule_denies_a_matching_agent() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "client", "BadBot")]))
    verdict = rules.evaluate(user_agent="Mozilla BadBot/2.1", remote_ip="203.0.113.1")
    assert verdict["allowed"] is False
    assert verdict["rule_id"] == 1


def test_client_matching_is_case_insensitive() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "client", "badbot")]))
    assert rules.evaluate(user_agent="BADBOT/1")["allowed"] is False


def test_network_rule_denies_inside_the_range_only() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "network", "203.0.113.0/24")]))
    assert rules.evaluate(remote_ip="203.0.113.55:4444")["allowed"] is False
    assert rules.evaluate(remote_ip="198.51.100.55:4444")["allowed"] is True


def test_a_single_address_rule_works_without_a_prefix() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "network", "203.0.113.7")]))
    assert rules.evaluate(remote_ip="203.0.113.7:1")["allowed"] is False
    assert rules.evaluate(remote_ip="203.0.113.8:1")["allowed"] is True


def test_allow_rule_wins_over_a_surrounding_deny() -> None:
    """One exception can be carved out without rewriting the deny around it."""
    rules = AccessRules(_FakeDb([
        _rule(1, "network", "203.0.113.0/24", action="deny"),
        _rule(2, "network", "203.0.113.7", action="allow"),
    ]))
    assert rules.evaluate(remote_ip="203.0.113.7:1")["allowed"] is True
    assert rules.evaluate(remote_ip="203.0.113.8:1")["allowed"] is False


# -- ports and address shapes ------------------------------------------------

def test_port_is_stripped_from_both_address_families() -> None:
    assert strip_port("203.0.113.9:52344") == "203.0.113.9"
    assert strip_port("[2001:db8::5]:443") == "2001:db8::5"
    assert strip_port("2001:db8::5") == "2001:db8::5"
    assert strip_port("") == ""


def test_ipv6_rules_match() -> None:
    rules = AccessRules(_FakeDb([_rule(1, "network", "2001:db8::/32")]))
    assert rules.evaluate(remote_ip="[2001:db8:1::9]:443")["allowed"] is False
    assert rules.evaluate(remote_ip="[2001:dead::9]:443")["allowed"] is True


# -- validation happens before storage ---------------------------------------

def test_bad_patterns_are_refused_at_save_time() -> None:
    for kind, pattern in (("client", "([unclosed"), ("network", "not-an-ip"),
                          ("client", ""), ("bogus", "x")):
        try:
            validate_pattern(kind, pattern)
        except ValueError:
            continue
        raise AssertionError(f"{kind}/{pattern!r} should have been refused")


def test_valid_patterns_pass_validation() -> None:
    assert validate_pattern("client", "BadBot|Scraper")
    assert validate_pattern("network", "203.0.113.0/24")
    assert validate_pattern("network", "2001:db8::/32")


# -- blocked attempts are recorded -------------------------------------------

def test_a_block_is_recorded_with_enough_to_identify_it() -> None:
    db = _FakeDb([])
    rules = AccessRules(db)
    rules.record_block(username="someone", user_agent="BadBot/1",
                       remote_ip="203.0.113.9:5555", reason="client:BadBot",
                       rule_id=1, item_id="42")
    assert db.blocks, "the refusal should have been persisted"
    row = db.blocks[0]
    assert row[0] == "someone"
    assert row[2] == "203.0.113.9", "the port is noise, the address is not"
    assert row[3] == "client:BadBot"


def test_a_long_user_agent_is_truncated_not_rejected() -> None:
    db = _FakeDb([])
    AccessRules(db).record_block(username="", user_agent="A" * 5000,
                                remote_ip="203.0.113.1", reason="x",
                                rule_id=None)
    assert len(db.blocks[0][1]) <= 300


# -- rule management ---------------------------------------------------------

def test_add_and_remove_round_trip() -> None:
    db = _FakeDb([])
    rules = AccessRules(db)
    rules.add("client", "BadBot", "deny", "note")
    assert len(rules.list()) == 1
    assert rules.remove(1) is True
    assert rules.remove(999) is False


def test_add_refuses_an_unknown_action() -> None:
    rules = AccessRules(_FakeDb([]))
    try:
        rules.add("client", "BadBot", action="explode")
    except ValueError:
        return
    raise AssertionError("an unknown action should have been refused")
