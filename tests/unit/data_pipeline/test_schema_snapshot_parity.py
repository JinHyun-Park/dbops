"""E-4 copy parity.

There is no Lambda layer in this repo: cross-asset sharing is verbatim file
copies plus a parity test (the engine_family.py / metric_filters.py convention).
Two families of copy exist here and BOTH matter for a different reason:

  schema_diff_util.py x3. The READER (get_schema_diff) computes its diff live
  while the PRODUCER stores diff_from_previous_json that get_schema_history and
  diagnose_root_cause replay. If the two computations drift, the same DDL event
  is described two different ways depending on which tool the agent called.

  schema_snapshot.py x2. One Aurora-MySQL collector serves RDS MySQL through
  MySQLDataApiAdapter, exactly as mysql_table_stats.py already does.

Byte-identity is asserted, and so is IDENTICAL RESULT on real inputs, because a
byte check alone would pass on three copies that are all equally wrong.
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

_UTIL_COPIES = {
    "canonical": _ROOT / "mcp-servers" / "mcp_servers" / "operations" / "schema_diff_util.py",
    "etl": _ROOT / "data-pipeline" / "etl_collector" / "collectors" / "schema_diff_util.py",
    "rds_direct": _ROOT / "data-pipeline" / "rds_direct_collector" / "schema_diff_util.py",
    # FOURTH copy: the dashboard schema-changes panel derives created/dropped
    # from schema_snapshots and needs the same compute_diff, so a rename is a
    # rename_candidate in the panel exactly as it is in get_schema_diff. api/
    # cannot import mcp_servers, hence a copy.
    # NOTE: the canonical file's own docstring still says "COPIES (edit all three
    # together)" and lists three paths. This dict, not that docstring, is the
    # enforcement point; the wording is worth a one-line fix by whoever owns
    # mcp-servers/mcp_servers/operations/schema_diff_util.py next.
    "api_dashboard": _ROOT / "api" / "dashboard" / "schema_diff_util.py",
}

_SNAPSHOT_COPIES = {
    "etl": _ROOT / "data-pipeline" / "etl_collector" / "collectors" / "schema_snapshot.py",
    "rds_direct": _ROOT / "data-pipeline" / "rds_direct_collector" / "schema_snapshot.py",
}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schema_diff_util_copies_are_byte_identical():
    texts = {k: p.read_text() for k, p in _UTIL_COPIES.items()}
    canonical = texts["canonical"]
    drift = [k for k, t in texts.items() if t != canonical]
    assert not drift, (
        f"schema_diff_util.py copies drifted from the canonical: {drift}. "
        "Copy mcp-servers/mcp_servers/operations/schema_diff_util.py over them."
    )


def test_schema_snapshot_copies_are_byte_identical():
    texts = {k: p.read_text() for k, p in _SNAPSHOT_COPIES.items()}
    assert texts["etl"] == texts["rds_direct"], (
        "schema_snapshot.py drifted between etl_collector/collectors/ and "
        "rds_direct_collector/. One Aurora-MySQL collector serves RDS MySQL "
        "verbatim; copy one over the other."
    )


def test_every_copy_computes_the_same_diff():
    """Result parity, not just text parity. Real DDL: an add, a drop, an ALTER
    ADD COLUMN, and a rename pair."""
    before = {"users": ["email", "id"], "old_audit": ["id", "ts"], "legacy": ["k"]}
    after = {"users": ["email", "id", "phone"], "audit": ["id", "ts"], "brand_new": ["id"]}
    expected = {
        "added": ["brand_new"],
        "dropped": ["legacy"],
        "modified": [{"table": "users", "added_columns": ["phone"], "dropped_columns": []}],
        "rename_candidates": [{"from": "old_audit", "to": "audit"}],
    }
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_util_{name}")
        assert mod.compute_diff(before, after) == expected, f"{name} disagrees"
        # The blob the Data API actually hands back is a STRING.
        assert mod.parse_tables('{"t": ["b", "a"]}') == {"t": ["a", "b"]}, name
        assert mod.diff_is_empty(mod.compute_diff(before, before)) is True, name


def test_reader_and_producer_agree_on_the_same_event():
    """The point of the copies: get_schema_diff's live computation and the diff
    the collector stores for get_schema_history must be the same object."""
    import sys

    sys.path.insert(0, str(_ROOT / "mcp-servers"))
    from mcp_servers.operations.tools.schema_diff import _compute_diff as reader_diff

    producer = _load(_UTIL_COPIES["etl"], "_parity_producer")
    before = {"orders": ["amount", "id"]}
    after = {"orders": ["amount", "currency", "id"]}
    assert reader_diff(before, after) == producer.compute_diff(before, after)
