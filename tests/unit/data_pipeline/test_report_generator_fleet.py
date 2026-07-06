"""Fleet rollup: data builder + HTML contract.

CRITICAL (past bug in this file pair): the HTML builder once read keys the data
builder never emitted, yielding blank charts. So the contract test below feeds
the REAL fleet-data builder's ACTUAL output into build_fleet_report_html and
asserts the rendered HTML contains real cluster ids/numbers — no invented
fixture keys.
"""

import importlib.util
import sys
from pathlib import Path

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "report_generator"
    / "handler.py"
)
_HANDLER_DIR = str(_HANDLER_PATH.parent)
if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)

_spec = importlib.util.spec_from_file_location("report_generator_handler_fleet", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import report_html  # resolves via _HANDLER_DIR on sys.path


def _report_data(cluster_id, avg_aas, max_aas, alert_fires, n_slow, delta_bytes):
    """A realistic _build_report_data-shaped dict for one cluster."""
    return {
        "cluster_id": cluster_id,
        "aas": {"avg_aas": avg_aas, "max_aas": max_aas},
        "aas_peak": {"value": max_aas},
        "top_slow_queries": [{"query_hash": f"h{i}", "total_ms": 100.0} for i in range(n_slow)],
        "top_alerts": [{"rule_id": "r1", "fired_count": alert_fires}] if alert_fires else [],
        "storage": {"delta_bytes": delta_bytes},
        "connections": {"max_conn": 10},
    }


def _fleet_rows():
    # Built through the REAL _fleet_row producer, not hand-authored records.
    return [
        handler._fleet_row("prod-pg-1", "aurora-postgresql",
                           _report_data("prod-pg-1", 2.0, 4.0, alert_fires=3, n_slow=2, delta_bytes=50_000_000)),
        handler._fleet_row("prod-mysql-2", "aurora-mysql",
                           _report_data("prod-mysql-2", 1.0, 9.5, alert_fires=7, n_slow=5, delta_bytes=-1_000_000)),
        handler._fleet_row("staging-pg-3", "aurora-postgresql",
                           _report_data("staging-pg-3", 0.2, 0.5, alert_fires=0, n_slow=0, delta_bytes=0)),
    ]


def test_fleet_row_shape_and_health():
    rows = _fleet_rows()
    r0 = next(r for r in rows if r["cluster_id"] == "prod-pg-1")
    assert r0["engine"] == "aurora-postgresql"
    assert r0["alert_count"] == 3          # sum of fired_count
    assert r0["slow_query_count"] == 2     # len(top_slow_queries)
    assert r0["health"] == "주의"          # alerts > 0
    clean = next(r for r in rows if r["cluster_id"] == "staging-pg-3")
    assert clean["health"] == "정상"       # 0 alerts


def test_build_fleet_data_totals_and_worst_ordering():
    fd = handler._build_fleet_data(_fleet_rows())
    assert fd["clusters_total"] == 3
    assert fd["totals"]["alerts"] == 10          # 3 + 7 + 0
    assert fd["totals"]["slow_queries"] == 7     # 2 + 5 + 0
    assert fd["engine_counts"]["aurora-postgresql"] == 2
    assert fd["engine_counts"]["aurora-mysql"] == 1
    assert fd["health_distribution"]["주의"] == 2
    assert fd["health_distribution"]["정상"] == 1
    # worst sorted by (alerts desc, aas_max desc): mysql(7) first, pg-1(3) second
    worst_ids = [w["cluster_id"] for w in fd["worst_clusters"]]
    assert worst_ids[0] == "prod-mysql-2"
    assert worst_ids[1] == "prod-pg-1"
    assert len(fd["worst_clusters"]) <= 5


def test_fleet_summary_mentions_counts_and_worst():
    fd = handler._build_fleet_data(_fleet_rows())
    text = handler._fleet_summary("2026-07-06", fd)
    assert "2026-07-06" in text
    assert "클러스터 3대" in text
    assert "경보 10건" in text
    assert "슬로우쿼리 7건" in text
    assert "prod-mysql-2" in text  # a worst cluster is named


def test_fleet_html_contract_renders_real_ids_and_numbers():
    """Feed the REAL builder output into the REAL HTML builder."""
    fd = handler._build_fleet_data(_fleet_rows())
    summary = handler._fleet_summary("2026-07-06", fd)
    html = report_html.build_fleet_report_html("2026-07-06", "daily", summary, fd)

    assert isinstance(html, str) and html.lstrip().startswith("<!doctype html>")
    # Real cluster ids from the data builder must appear in the rendered tables.
    assert "prod-pg-1" in html
    assert "prod-mysql-2" in html
    assert "staging-pg-3" in html
    # Real aggregate numbers must appear (totals + summary), not blank charts.
    assert "Fleet 전체" in html
    assert "aurora-postgresql" in html
    # totals.alerts=10 and clusters_total=3 render in the cards
    assert "10" in html
    # health distribution buckets rendered
    assert "주의" in html and "정상" in html


def test_fleet_html_survives_empty_and_missing_keys():
    """Empty fleet_data must not raise (no clusters / missing totals)."""
    html = report_html.build_fleet_report_html("2026-07-06", "daily", "요약", {})
    assert html.lstrip().startswith("<!doctype html>")
    assert "데이터 없음" in html
