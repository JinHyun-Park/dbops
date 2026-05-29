"""Tests for the timeline event_type → category normalizer.

Regression guard for a bug found via live verification: ack events are
written with event_type='alert_ack' (not 'ack'), and event_processor
writes 'alarm_ok' / arbitrary RDS detail_types — none of which the
original exact-match normalizer caught, so they fell through as raw
category strings the frontend couldn't color.
"""

import importlib.util
import sys
from pathlib import Path

_DASH = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASH))
_spec = importlib.util.spec_from_file_location("dashboard_handler", _DASH / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

_cat = handler._timeline_category


def test_alert_maps_to_alert():
    assert _cat("alert") == "alert"


def test_alert_ack_maps_to_ack():
    """The actual string slack_interactive writes."""
    assert _cat("alert_ack") == "ack"


def test_bare_ack_also_maps_to_ack():
    assert _cat("ack") == "ack"


def test_proactive_maps_to_proactive():
    assert _cat("proactive") == "proactive"


def test_cloudwatch_alarm_states_map_to_rds_event():
    assert _cat("alarm_ok") == "rds_event"
    assert _cat("alarm_alarm") == "rds_event"


def test_unknown_rds_detail_type_falls_through_to_rds_event():
    assert _cat("RDS-EVENT-0006") == "rds_event"
    assert _cat("failover") == "rds_event"


def test_empty_falls_through():
    assert _cat("") == "rds_event"
    assert _cat(None) == "rds_event"


def test_case_insensitive():
    assert _cat("ALERT") == "alert"
    assert _cat("Alert_Ack") == "ack"
