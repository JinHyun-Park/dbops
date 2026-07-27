"""`GET /api/dashboard/{id}/anomalies` must say WHY the list is empty.

An empty `anomalies` list has three meanings and only one of them is good news:

  * baselines exist, nothing crossed the threshold  -> genuinely quiet
  * no baseline exists for this bucket              -> NOTHING was scored
  * the lookup failed                               -> unknown

Before this, `_anomalies` returned only {cluster_id, hours, threshold,
anomalies}, so the panel rendered "최근 4시간 동안 이상 징후 없음" for all three.
The reviewer reproduced the bad state on real PostgreSQL: 0 `metric_baselines`
rows, 0 metrics scored, and a DocumentDB operator told there were no anomalies.

`baseline_mode` + `total_checked` close it, derived exactly the way
detect_anomalies_impl derives them, off the SAME (verbatim-copied) SQL.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_anomalies", _DASHBOARD_DIR / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _query(rows):
    """Records the SQL it was handed and replays canned rows."""
    calls = []

    def query(sql, params=None):
        calls.append((sql, params or {}))
        return rows

    query.calls = calls
    return query


def _row(metric, z, mode, **kw):
    # Data API hands numerics back as strings (PG numeric -> stringValue), which
    # is exactly why the threshold comparison has to coerce.
    return {
        "metric_type": metric,
        "recent_max": "100",
        "recent_avg": "80",
        "baseline_mean": "10",
        "baseline_stddev": "2",
        "z_score": str(z),
        "mode": mode,
        "sample_count": kw.get("sample_count"),
    }


# ---------------------------------------------------------------------------
# Verbatim-copy contract
# ---------------------------------------------------------------------------


def _grab_sql(path):
    m = re.search(r'_ANOMALY_SQL = """(.*?)"""', path.read_text(encoding="utf-8"), re.S)
    assert m, f"_ANOMALY_SQL not found in {path}"
    return m.group(1)


def test_the_scoring_sql_is_byte_identical_to_the_mcp_tools():
    """api/ cannot import mcp_servers, so the SQL is duplicated. If the two
    drift, the dashboard and the chat agent disagree about whether a metric is
    anomalous AND about whether the cluster has a trained baseline at all."""
    assert _grab_sql(_DASHBOARD_DIR / "handler.py") == _grab_sql(
        _ROOT / "mcp-servers/mcp_servers/performance/tools/detect_anomalies.py"
    )


def test_scoring_sql_does_not_filter_by_threshold_and_keeps_both_dim_filters():
    """The threshold filter must NOT be in SQL: rows below it are the evidence
    that scoring happened at all. And both metric_snapshots reads stay
    cluster-level only (strict dimension filter)."""
    q = _query([])
    handler._anomalies(q, "c1", 4, 2.5)
    sql, params = q.calls[0]
    assert ":threshold" not in sql
    assert sql.count("dimensions IS NULL OR dimensions::text = '{}'") == 2
    assert params == {"cluster_id": "c1", "hours": "4"}


# ---------------------------------------------------------------------------
# baseline_mode
# ---------------------------------------------------------------------------


def test_no_baseline_at_all_reports_mode_none_not_a_clean_bill_of_health():
    """The reviewer's reproduced state: 0 metric_baselines rows, nothing scored."""
    out = handler._anomalies(_query([]), "docdb-1", 4, 2.5)
    assert out["baseline_mode"] == "none"
    assert out["total_checked"] == 0
    assert out["anomalies"] == []


def test_quiet_cluster_with_a_trained_baseline_is_distinguishable_from_mode_none():
    """THE regression this exists for: every scored metric is below threshold, so
    `anomalies` is empty, but a seasonal baseline demonstrably exists. Deriving
    baseline_mode from the FILTERED list would report 'none' here and the panel
    would tell the operator no baseline is trained yet."""
    rows = [_row("cpu", 0.4, "seasonal"), _row("aas", 1.1, "seasonal")]
    out = handler._anomalies(_query(rows), "c1", 4, 2.5)
    assert out["anomalies"] == []
    assert out["baseline_mode"] == "seasonal"
    assert out["total_checked"] == 2


def test_only_flat_rows_report_mode_flat():
    out = handler._anomalies(_query([_row("cpu", 0.2, "flat")]), "c1", 4, 2.5)
    assert out["baseline_mode"] == "flat"


def test_any_seasonal_row_wins_over_flat_rows():
    rows = [_row("cpu", 3.0, "flat"), _row("aas", 0.1, "seasonal")]
    assert handler._anomalies(_query(rows), "c1", 4, 2.5)["baseline_mode"] == "seasonal"


# ---------------------------------------------------------------------------
# Threshold filtering moved to Python
# ---------------------------------------------------------------------------


def test_threshold_filters_on_absolute_z_and_counts_everything_checked():
    rows = [
        _row("cpu", 4.0, "seasonal"),
        _row("aas", -3.1, "seasonal"),   # negative anomaly still an anomaly
        _row("conn", 2.5, "seasonal"),   # exactly at threshold: included
        _row("iops", 1.0, "seasonal"),   # below: counted, not reported
    ]
    out = handler._anomalies(_query(rows), "c1", 4, 2.5)
    assert [a["metric_type"] for a in out["anomalies"]] == ["cpu", "aas", "conn"]
    assert out["total_checked"] == 4


def test_unparseable_z_score_is_not_reported_as_an_anomaly():
    out = handler._anomalies(_query([_row("cpu", "NaN-ish", "seasonal")]), "c1", 4, 2.5)
    assert out["anomalies"] == []
    assert out["baseline_mode"] == "seasonal"  # it WAS scored, just unreadable


def test_baseline_mode_matches_the_mcp_tool_row_for_row():
    """Same rows through both readers must yield the same baseline_mode. This is
    the contract the byte-identical SQL exists to serve."""
    sys.path.insert(0, str(_ROOT / "mcp-servers"))
    from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl

    class _Result:
        def __init__(self, rows):
            self.rows = rows

    class _Cache:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            return _Result(self._rows)

    for rows in (
        [],
        [_row("cpu", 0.4, "seasonal")],
        [_row("cpu", 0.4, "flat")],
        [_row("cpu", 9.0, "flat"), _row("aas", 0.1, "seasonal")],
    ):
        mine = handler._anomalies(_query(rows), "c1", 4, 2.5)
        theirs = detect_anomalies_impl(_Cache(rows), "c1", 4, 2.5)
        assert mine["baseline_mode"] == theirs["baseline_mode"], rows
        assert mine["total_checked"] == theirs["total_checked"], rows
        assert [a["metric_type"] for a in mine["anomalies"]] == [
            a["metric_type"] for a in theirs["anomalies"]
        ], rows
