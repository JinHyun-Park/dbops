"""Tests for the report generator helpers.

These focus on the deterministic pieces — _template_summary fallback and
_build_summary_prompt construction. The Bedrock-invoke path is tested
implicitly via Bedrock-failure simulation: the public entry point
_write_nl_summary must fall back to _template_summary when bedrock-runtime
raises.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

# Multiple Lambda handlers in this project are named `handler.py`. If we
# do plain `sys.path.insert(...) + import handler`, the first test to run
# wins and subsequent tests get its cached module instead of theirs.
# Load this handler under a unique name so the module table stays sane
# regardless of test ordering.
_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "report_generator"
    / "handler.py"
)
import sys as _sys

_HANDLER_DIR = str(_HANDLER_PATH.parent)
if _HANDLER_DIR not in _sys.path:
    _sys.path.insert(0, _HANDLER_DIR)
_spec = importlib.util.spec_from_file_location("report_generator_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _sample_data():
    return {
        "cluster_id": "prod-pg-1",
        "window_hours": 24,
        "aas": {"avg_aas": 2.3, "max_aas": 8.1, "p95_aas": 6.7, "samples": 1440},
        "aas_peak": {"ts": "2026-05-28T03:14:00Z", "value": 8.1},
        "aas_busy_minutes_above_threshold": 42,
        "aas_busy_threshold": 5.0,
        "top_slow_queries": [
            {
                "query_hash": "abc123",
                "query_excerpt": "SELECT * FROM users WHERE ...",
                "calls": 1000,
                "total_ms": 50000.0,
                "mean_ms": 50.0,
            }
        ],
        "top_alerts": [{"rule_id": "high-cpu", "fired_count": 7, "last_fired": "2026-05-28T11:00:00Z"}],
        "storage": {"start_bytes": 1000000000, "end_bytes": 1100000000, "delta_bytes": 100000000},
        "connections": {"max_conn": 45, "avg_conn": 12.5},
        "events_by_type": [{"event_type": "backup_complete", "cnt": 4}],
    }


def test_template_summary_mentions_cluster_and_aas():
    text = handler._template_summary("prod-pg-1", "2026-05-28", _sample_data())
    assert "prod-pg-1" in text
    assert "2026-05-28" in text
    # AAS numbers should appear (rounded to 2dp).
    assert "2.30" in text or "2.3" in text or "AAS" in text
    # Busy minutes should appear.
    assert "42" in text


def test_template_summary_handles_empty_data():
    text = handler._template_summary(
        "c1",
        "2026-05-28",
        {"aas": {}, "top_slow_queries": [], "top_alerts": [], "aas_busy_threshold": 5},
    )
    # Should not crash on missing fields, and should still say *something*.
    assert "c1" in text
    assert "주목할" in text  # "주목할 만한 이벤트는 없었습니다"


def test_build_summary_prompt_includes_signals():
    prompt = handler._build_summary_prompt("prod-pg-1", "2026-05-28", _sample_data())
    # Domain expert framing.
    assert "DBA" in prompt or "시니어" in prompt
    # Cluster + date in header.
    assert "prod-pg-1" in prompt
    assert "2026-05-28" in prompt
    # Numbers present in their respective sections.
    assert "8.1" in prompt  # peak AAS
    assert "42" in prompt  # busy minutes
    assert "high-cpu" in prompt  # alert rule
    assert "SELECT" in prompt  # slow query excerpt


def test_build_summary_prompt_handles_no_slow_no_alerts():
    """Empty top_slow/top_alerts should render as "(none)" rather than crash."""
    data = _sample_data()
    data["top_slow_queries"] = []
    data["top_alerts"] = []
    prompt = handler._build_summary_prompt("prod-pg-1", "2026-05-28", data)
    assert "(none)" in prompt


@patch.object(handler, "boto3")
def test_write_nl_summary_falls_back_on_bedrock_error(mock_boto3):
    """If invoke_model throws, the public entry point must NOT raise — it
    falls back to the deterministic template so the report row always
    has a usable summary column."""
    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model.side_effect = RuntimeError("throttled")
    mock_boto3.client.return_value = mock_bedrock

    text = handler._write_nl_summary("prod-pg-1", "2026-05-28", _sample_data())
    assert text  # not empty
    assert "prod-pg-1" in text


@patch.object(handler, "boto3")
def test_write_nl_summary_uses_bedrock_text_when_ok(mock_boto3):
    """Happy path: Bedrock returns content[0].text, we use it verbatim."""
    mock_bedrock = MagicMock()
    body_stream = MagicMock()
    summary = "24시간 동안 안정적이었습니다."
    body_stream.read.return_value = ('{"content":[{"text":"' + summary + '"}]}').encode("utf-8")
    mock_bedrock.invoke_model.return_value = {"body": body_stream}
    mock_boto3.client.return_value = mock_bedrock

    text = handler._write_nl_summary("prod-pg-1", "2026-05-28", _sample_data())
    assert text == summary


@patch.object(handler, "boto3")
def test_write_nl_summary_falls_back_when_bedrock_returns_empty(mock_boto3):
    """If Bedrock returns an empty string (e.g. content filter), template
    summary should still render."""
    mock_bedrock = MagicMock()
    body_stream = MagicMock()
    body_stream.read.return_value = b'{"content":[{"text":""}]}'
    mock_bedrock.invoke_model.return_value = {"body": body_stream}
    mock_boto3.client.return_value = mock_bedrock

    text = handler._write_nl_summary("prod-pg-1", "2026-05-28", _sample_data())
    assert text  # not empty — template kicked in
    assert "prod-pg-1" in text
