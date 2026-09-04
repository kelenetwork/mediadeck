"""Access rules for the playback edge: who gets a signed URL, and who does not.

Two kinds of rule, both evaluated against the request that is asking for media:

- **client** — matched against the User-Agent. This is what stops a scraper or
  a repackaged client that pretends to be a browser, and it is the rule type
  the operator actually reaches for when someone shows up with an odd client.
- **network** — matched against the caller's address, as a single address or a
  CIDR. Country-level blocking is deliberately not offered here: it needs a
  GeoIP database this host does not have, and a rule that silently matches
  nothing is worse than no rule at all.

Three properties matter more than the matching itself:

**Fail-open.** A rule that throws, a pattern that will not compile, an empty
rule set — none of these may deny playback. The panel sits on the media path;
if it becomes a way to break playback, it is worse than not being there. Every
uncertain outcome resolves to "allow" and is visible in the log.

**Never lock the operator out.** Excluded users bypass every rule. Without this
one bad regex typed at 3am ends access for everyone including the person who
would have to fix it.

**Deny is recorded, not silent.** A blocked request that leaves no trace is
indistinguishable from a broken node, and the operator ends up debugging the
wrong thing.
"""
from __future__ import annotations

import ipaddress
import re
import time
from typing import Any

RULE_KINDS = ("client", "network")
RULE_ACTIONS = ("deny", "allow")

# Compiled patterns are cached: this runs on every playback start, and
# recompiling a handful of regexes per request is pure waste.
_PATTERN_CACHE: dict[str, re.Pattern | None] = {}


def compile_pattern(pattern: str) -> re.Pattern | None:
    """Compile a client pattern, or None if it will never be usable.

    Returning None rather than raising is intentional: a stored rule that no
    longer compiles must not take down the playback path. It is refused at save
    time instead, where a person is present to read the error.
    """
    if pattern in _PATTERN_CACHE:
        return _PATTERN_CACHE[pattern]
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        compiled = None
    _PATTERN_CACHE[pattern] = compiled
    return compiled


def validate_pattern(kind: str, pattern: str) -> str:
    """Check a rule before it is stored. Raises ValueError with a reason."""
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("规则内容不能为空")
    if kind == "client":
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}") from None
    elif kind == "network":
        try:
            ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            raise ValueError("网段格式无效，应形如 203.0.113.0/24 或单个地址") from None
    else:
        raise ValueError(f"规则类型必须是 {'/'.join(RULE_KINDS)} 之一")
    return pattern


def strip_port(ip: str) -> str:
    """Emby reports 'addr:port' for v4 and '[addr]:port' for v6."""
    raw = (ip or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        return raw[1:].split("]")[0]
    if raw.count(":") == 1:
        return raw.split(":")[0]
    return raw


class AccessRules:
    """Rule storage and evaluation for the playback edge."""

    def __init__(self, db: Any) -> None:
        self._db = db

    # -- rule management ------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM access_rules ORDER BY kind, id")

    def add(self, kind: str, pattern: str, action: str = "deny",
            note: str = "", enabled: bool = True) -> dict[str, Any]:
        if action not in RULE_ACTIONS:
            raise ValueError(f"动作必须是 {'/'.join(RULE_ACTIONS)} 之一")
        pattern = validate_pattern(kind, pattern)
        now = int(time.time())
        self._db.execute(
            "INSERT INTO access_rules(kind,pattern,action,note,enabled,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (kind, pattern, action, note.strip(), 1 if enabled else 0, now))
        return {"kind": kind, "pattern": pattern, "action": action}

    def remove(self, rule_id: int) -> bool:
        if not self._db.one("SELECT 1 AS x FROM access_rules WHERE id=?", (rule_id,)):
            return False
        self._db.execute("DELETE FROM access_rules WHERE id=?", (rule_id,))
        return True

    def set_enabled(self, rule_id: int, enabled: bool) -> bool:
        if not self._db.one("SELECT 1 AS x FROM access_rules WHERE id=?", (rule_id,)):
            return False
        self._db.execute("UPDATE access_rules SET enabled=? WHERE id=?",
                         (1 if enabled else 0, rule_id))
        return True

    # -- evaluation -----------------------------------------------------------

    def evaluate(self, user_agent: str = "", remote_ip: str = "",
                 username: str = "", excluded: set[str] | None = None
                 ) -> dict[str, Any]:
        """Decide whether this request may be served.

        Always returns a verdict; never raises. The caller is on the playback
        path, and an exception here would turn a rule problem into an outage.
        """
        verdict: dict[str, Any] = {"allowed": True, "rule_id": None, "reason": ""}
        try:
            if username and excluded and username in excluded:
                verdict["reason"] = "excluded"
                return verdict

            rules = [r for r in self.list() if r.get("enabled")]
            if not rules:
                return verdict

            address = strip_port(remote_ip)
            parsed = None
            if address:
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    parsed = None

            # An explicit allow wins over any deny, so one exception can be
            # carved out without rewriting the deny that surrounds it.
            matches = []
            for rule in rules:
                if self._matches(rule, user_agent, parsed):
                    matches.append(rule)

            for rule in matches:
                if rule.get("action") == "allow":
                    verdict["reason"] = "allow-rule"
                    verdict["rule_id"] = rule.get("id")
                    return verdict

            for rule in matches:
                if rule.get("action") == "deny":
                    verdict["allowed"] = False
                    verdict["reason"] = f"{rule.get('kind')}:{rule.get('pattern')}"
                    verdict["rule_id"] = rule.get("id")
                    return verdict
        except Exception:  # noqa: BLE001 - fail open, deliberately
            verdict["allowed"] = True
            verdict["reason"] = "error"
        return verdict

    @staticmethod
    def _matches(rule: dict[str, Any], user_agent: str,
                 parsed: Any) -> bool:
        kind = rule.get("kind")
        pattern = str(rule.get("pattern") or "")
        if kind == "client":
            if not user_agent:
                # No User-Agent means no evidence, and absence of evidence must
                # not be read as a match.
                return False
            compiled = compile_pattern(pattern)
            return bool(compiled and compiled.search(user_agent))
        if kind == "network":
            if parsed is None:
                return False
            try:
                return parsed in ipaddress.ip_network(pattern, strict=False)
            except ValueError:
                return False
        return False

    # -- blocked attempts -----------------------------------------------------

    def record_block(self, *, username: str, user_agent: str, remote_ip: str,
                     reason: str, rule_id: int | None, item_id: str = "") -> None:
        self._db.execute(
            "INSERT INTO access_blocks"
            "(username,user_agent,remote_ip,reason,rule_id,item_id,blocked_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (username, user_agent[:300], strip_port(remote_ip), reason,
             rule_id, item_id, int(time.time())))

    def blocks(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM access_blocks ORDER BY blocked_at DESC LIMIT ?",
            (max(1, min(limit, 500)),))
