import importlib.util
from pathlib import Path

_C = Path(__file__).resolve().parents[3] / "data-pipeline/report_generator/report_html.py"
_spec = importlib.util.spec_from_file_location("report_html", _C)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

_DATA = {
    "aas_avg": 1.2, "aas_max": 4.5,
    "aas_series": [{"ts": "2026-06-24T00:00:00Z", "value": 1.0},
                   {"ts": "2026-06-24T01:00:00Z", "value": 2.5}],
    "top_queries": [{"query_excerpt": "SELECT * FROM orders", "count": 9},
                    {"query_excerpt": "UPDATE <x> SET y", "count": 3}],
    "findings": [{"severity": "warning", "subject": "X", "recommendation": "do Y"}],
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


def test_summary_and_query_text_html_escaped():
    evil = "<script>alert(1)</script> & <b>"
    html = rh.build_report_html("c", "2026-06-24", "daily", evil,
                                {"top_queries": [{"query_excerpt": "<img src=x>", "count": 1}]})
    assert "<script>alert(1)</script>" not in html   # raw injection blocked
    assert "&lt;script&gt;" in html                  # escaped form present
    assert "<img src=x>" not in html


def test_empty_data_still_valid_html_no_crash():
    html = rh.build_report_html("c", "2026-06-24", "daily", "", {})
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "데이터 없음" in html   # placeholder for empty series


def test_chart_builders_tolerate_empty():
    assert "<svg" in rh.line_chart([])
    assert "<svg" in rh.bar_chart([])
