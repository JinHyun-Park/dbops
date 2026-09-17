"""Fleet rollup: data builder + HTML contract.

CRITICAL (past bug in this file pair): the HTML builder once read keys the data
builder never emitted, yielding blank charts. So the contract test below feeds
the REAL fleet-data builder's ACTUAL output into build_fleet_report_html and
asserts the rendered HTML contains real cluster ids/numbers, no invented
fixture keys.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_HANGUL = re.compile(r"[가-힣]")

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
    text = handler._fleet_summary("2026-07-06", fd, "ko")
    assert "2026-07-06" in text
    assert "클러스터 3대" in text
    assert "경보 10건" in text
    assert "슬로우쿼리 7건" in text
    assert "prod-mysql-2" in text  # a worst cluster is named


# ---------------------------------------------------------------------------
# The fleet summary follows the deployment locale.
#
# This one has NO Bedrock call in its path: the template IS the fleet row's
# summary on every run, not just on the days the model is unreachable. So a
# Korean-only version was wrong on an en deployment every single day, and the
# frontend's language label sat above it explaining a language it was not in.
#
# Asserted in BOTH DIRECTIONS, because a one-direction test ("the Korean summary
# says 클러스터") passes unchanged on the hardcoded Korean template this change
# exists to remove.
# ---------------------------------------------------------------------------


def test_the_fleet_summary_follows_the_locale_in_both_directions():
    fd = handler._build_fleet_data(_fleet_rows())
    ko = handler._fleet_summary("2026-07-06", fd, "ko")
    en = handler._fleet_summary("2026-07-06", fd, "en")
    assert ko != en
    assert "클러스터 3대, 경보 10건, 슬로우쿼리 7건." in ko
    assert "Clusters 3, alerts 10, slow queries 7." in en
    # Every number, every cluster id and the date survive the crossing verbatim.
    for text in (ko, en):
        assert "2026-07-06" in text
        assert "prod-mysql-2" in text and "prod-pg-1" in text
    # No Korean left in the English one. This is the assertion that scales: it
    # fails on the next hardcoded Korean fragment added to the template.
    assert not _HANGUL.search(en), en


def test_the_quiet_fleet_branch_follows_the_locale_too():
    """The other branch, reached whenever no cluster fired an alert, i.e. most
    days. Testing only the noisy branch leaves half the template hardcoded."""
    quiet = handler._build_fleet_data([
        handler._fleet_row("staging-pg-3", "aurora-postgresql",
                           _report_data("staging-pg-3", 0.2, 0.5, alert_fires=0,
                                        n_slow=0, delta_bytes=0)),
    ])
    ko = handler._fleet_summary("2026-07-06", quiet, "ko")
    en = handler._fleet_summary("2026-07-06", quiet, "en")
    assert "주의가 필요한 클러스터는 없습니다." in ko
    assert "No cluster needs attention." in en
    assert not _HANGUL.search(en), en


@pytest.mark.parametrize("loc", ["", None, "EN", "en-US", "fr", 7])
def test_an_unrecognised_locale_reads_korean(loc):
    """Same fail-closed default as handler._report_locale, which is the only
    producer of this argument: Korean is what every deployment ships, and a
    value nobody validated does not get to switch a stored summary's language."""
    text = handler._fleet_summary("2026-07-06", handler._build_fleet_data(_fleet_rows()), loc)
    assert "클러스터 3대" in text, loc


def test_fleet_html_contract_renders_real_ids_and_numbers():
    """Feed the REAL builder output into the REAL HTML builder."""
    fd = handler._build_fleet_data(_fleet_rows())
    summary = handler._fleet_summary("2026-07-06", fd, "ko")
    html = report_html.build_fleet_report_html("2026-07-06", "daily", summary, fd, "ko")

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
    html = report_html.build_fleet_report_html("2026-07-06", "daily", "요약", {}, "ko")
    assert html.lstrip().startswith("<!doctype html>")
    assert "데이터 없음" in html
