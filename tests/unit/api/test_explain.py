"""Tests for the explain API handler — _build_explain_sql plan-only mode."""

import importlib.util
from pathlib import Path

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "explain" / "handler.py"
_spec = importlib.util.spec_from_file_location("explain_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def test_pg_default_uses_analyze():
    sql = handler._build_explain_sql("SELECT 1", "aurora-postgresql")
    assert "ANALYZE" in sql and "FORMAT JSON" in sql


def test_pg_plan_only_omits_analyze():
    sql = handler._build_explain_sql("SELECT 1", "aurora-postgresql", analyze=False)
    assert "ANALYZE" not in sql
    assert "BUFFERS" in sql and "FORMAT JSON" in sql


def test_mysql_never_analyzes_regardless():
    assert "ANALYZE" not in handler._build_explain_sql("SELECT 1", "aurora-mysql", analyze=True)
