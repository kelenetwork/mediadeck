"""Node agent regression tests.

The agent ships as a standalone single file to the nodes, so it is loaded by
path here rather than imported as a package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent" / "loadprobe.py"


def _load():
    spec = importlib.util.spec_from_file_location("loadprobe", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parses_the_labelled_format_nodes_actually_write() -> None:
    """Regression: live speed was permanently blank on every node.

    Nodes write ``<msec> u=<tag> r=<rate> <bytes> <secs>``, but the parser
    read fields positionally, so ``int("r=10000000")`` raised on *every*
    line and every line was discarded. user_speeds stayed empty forever, the
    panel silently fell back to its estimate, and the dashboard showed a
    speed belonging to nobody.

    These are verbatim production lines.
    """
    parse = _load().SpeedLog.parse
    assert parse("1788035116.082 u=5e51160de9 r=10000000 246314907 5.383") == (
        1788035116.082, "5e51160de9", 246314907, 5.383)
    assert parse("1788037051.094 u=diagprobe r=0 1201318553 27.776") == (
        1788037051.094, "diagprobe", 1201318553, 27.776)


def test_still_parses_the_original_positional_format() -> None:
    """Nodes provisioned before the rate rollout keep the older log shape.

    The agent updates independently of each node's nginx template, so it must
    read both rather than break whichever it was not written for.
    """
    parse = _load().SpeedLog.parse
    assert parse("1788035116.082 5e51160de9 246314907 5.383") == (
        1788035116.082, "5e51160de9", 246314907, 5.383)


def test_unattributable_or_empty_requests_are_ignored() -> None:
    """Anonymous and zero-byte requests must not invent a user or a rate."""
    parse = _load().SpeedLog.parse
    assert parse("1788035116.082 u= r=0 12345 1.0") is None      # no user tag
    assert parse("1788035116.082 - 12345 1.0") is None           # nginx placeholder
    assert parse("1788035116.082 u=abc r=0 0 1.0") is None       # nothing sent
    assert parse("hello world") is None
    assert parse("") is None


def test_speeds_attribute_bytes_to_the_right_user() -> None:
    """A parsed line must reach the per-user aggregate, not just parse."""
    module = _load()
    log = module.SpeedLog("")  # empty path: no tailing thread, feed manually
    import time
    now = time.time()
    log._ingest(f"{now:.3f} u=alice r=10000000 100000000 5.0")
    log._ingest(f"{now:.3f} u=bob r=0 20000000 5.0")
    speeds = log.speeds()
    assert set(speeds) == {"alice", "bob"}
    # alice moved 5x bob's bytes over the same interval.
    assert speeds["alice"] > speeds["bob"] * 4
