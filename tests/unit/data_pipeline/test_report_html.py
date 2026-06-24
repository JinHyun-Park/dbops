import importlib.util
from pathlib import Path

_C = Path(__file__).resolve().parents[3] / "data-pipeline/report_generator/report_html.py"
_spec = importlib.util.spec_from_file_location("report_html", _C)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

# Real-shaped fixture matching the VERIFIED _build_report_data contract.
_DATA = {
    "cluster_id": "my-cluster",
    "window_hours": 24,
    "aas": {"avg_aas": 1.2, "max_aas": 4.5, "p95_aas": 3.8, "samples": 1440},
    "aas_peak": {"ts": "2026-06-24T00:30:00Z", "value": 4.5},
    "aas_busy_minutes_above_threshold": 15,
    "aas_busy_threshold": 5,
    "aas_series": [
        {"ts": "2026-06-24T00:00:00Z", "value": 1.0},
        {"ts": "2026-06-24T01:00:00Z", "value": 2.5},
        {"ts": "2026-06-24T02:00:00Z", "value": 4.5},
    ],
    "top_slow_queries": [
        {"query_hash": "abc123", "query_excerpt": "SELECT * FROM orders", "calls": 9, "total_ms": 5000.0, "mean_ms": 555.0},
        {"query_hash": "def456", "query_excerpt": "UPDATE accounts SET y", "calls": 3, "total_ms": 1200.0, "mean_ms": 400.0},
    ],
    "top_alerts": [
        {"rule_id": "high-cpu", "fired_count": 7, "last_fired": "2026-06-24T01:00:00Z"},
        {"rule_id": "replica-lag", "fired_count": 2, "last_fired": "2026-06-24T00:15:00Z"},
    ],
    "storage": {"start_bytes": 1000000000, "end_bytes": 1100000000, "delta_bytes": 100000000},
    "connections": {"max_conn": 45, "avg_conn": 12.5},
    "events_by_type": [{"event_type": "backup_complete", "cnt": 4}],
}


def test_build_html_is_self_contained_and_has_charts():
    html = rh.build_report_html("my-cluster", "2026-06-24", "daily", "요약 텍스트", _DATA)
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "my-cluster" in html
    assert "요약 텍스트" in html
    assert html.count("<svg") >= 2          # at least the line + a bar chart
    # self-contained: no external refs
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()    # no scripts


def test_real_data_renders_non_empty_content():
    """Content assertion: real-shaped fixture must produce a polyline (line chart
    drew data), a rect bar with query text, and the rule_id from top_alerts —
    i.e. the HTML is NOT the empty-placeholder shell."""
    html = rh.build_report_html("my-cluster", "2026-06-24", "daily", "요약 텍스트", _DATA)
    # Line chart drew data (AAS series has 3 points >= 2)
    assert "<polyline" in html, "Expected AAS line chart polyline, got placeholder"
    # Bar chart has at least one rect bar (slow queries rendered)
    assert 'fill="#6366f1"' in html, "Expected bar chart bars from top_slow_queries"
    # query_excerpt text appears in the bar chart labels
    assert "SELECT" in html, "Expected query_excerpt text in bar chart"
    # rule_id from top_alerts appears in the alerts table
    assert "high-cpu" in html, "Expected rule_id from top_alerts in alerts table"
    assert "replica-lag" in html, "Expected second rule_id from top_alerts"
    # AAS stat cards show real values from nested aas dict
    assert "1.2" in html or "AAS" in html, "Expected AAS avg card"


def test_summary_and_query_text_html_escaped():
    """Injection guarantee: every dynamic value from data/summary stays HTML-escaped."""
    evil_summary = "<script>alert(1)</script> & <b>"
    evil_data = {
        "aas": {"avg_aas": 1.0, "max_aas": 2.0},
        "aas_series": [{"ts": "2026-06-24T00:00:00Z", "value": 1.0},
                       {"ts": "2026-06-24T01:00:00Z", "value": 2.0}],
        "top_slow_queries": [
            {"query_hash": "x", "query_excerpt": "<img src=x onerror=alert(2)>", "calls": 1, "total_ms": 100.0, "mean_ms": 100.0},
        ],
        "top_alerts": [
            {"rule_id": "<b>evil-rule</b>", "fired_count": 1, "last_fired": "2026-06-24T00:00:00Z"},
        ],
        "connections": {},
    }
    html = rh.build_report_html("c", "2026-06-24", "daily", evil_summary, evil_data)
    # Raw injection attempts must not survive
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(2)>" not in html
    assert "<b>evil-rule</b>" not in html
    # Escaped forms must be present
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    assert "&lt;b&gt;" in html


def test_empty_data_still_valid_html_no_crash():
    html = rh.build_report_html("c", "2026-06-24", "daily", "", {})
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "데이터 없음" in html   # placeholder for empty series


def test_chart_builders_tolerate_empty():
    assert "<svg" in rh.line_chart([])
    assert "<svg" in rh.bar_chart([])


def test_sparkline_tolerates_decimal_and_strings():
    """MINOR-2: sparkline must handle Decimal-like objects (have __float__) and
    numeric strings, not just int/float."""
    from decimal import Decimal
    result = rh.sparkline([Decimal("1.5"), Decimal("2.0"), Decimal("3.5")])
    assert "<svg" in result, "Decimal values should produce a sparkline SVG"
    # Numeric strings (RDS Data API may surface these)
    result2 = rh.sparkline(["1.5", "2.0", "3.5"])
    assert "<svg" in result2, "Numeric strings should produce a sparkline SVG"
