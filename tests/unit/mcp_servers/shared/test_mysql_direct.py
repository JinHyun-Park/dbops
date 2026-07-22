"""Tests for mysql_direct — pymysql connect + Data-API-shape adapter.

Mirrors data-pipeline/rds_direct_collector's field-mapping test cases, plus
the two things that collector adapter deliberately lacks (positional-only):
columnMetadata synthesis and numberOfRecordsUpdated for write statements.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from mcp_servers.shared.mysql_direct import MySQLDataApiAdapter, connect


def test_adapter_maps_python_types_to_data_api_fields():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("s",), ("i",), ("f",), ("n",), ("b",), ("dt",)]
    cur.fetchall.return_value = [
        ("abc", 42, 3.14, None, b"bin", datetime(2026, 7, 22, 1, 2, 3)),
    ]
    out = MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    row = out["records"][0]
    assert row[0] == {"stringValue": "abc"}
    assert row[1] == {"longValue": 42}
    assert row[2] == {"doubleValue": 3.14}
    assert row[3] == {"isNull": True}
    assert row[4] == {"stringValue": "bin"}
    assert row[5] == {"stringValue": "2026-07-22 01:02:03"}


def test_adapter_bool_before_int():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("flag",)]
    cur.fetchall.return_value = [(True,)]
    out = MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    assert out["records"][0][0] == {"booleanValue": True}


def test_adapter_decimal_maps_to_double():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("amt",)]
    cur.fetchall.return_value = [(Decimal("7.5"),)]
    out = MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    assert out["records"][0][0] == {"doubleValue": 7.5}


def test_adapter_synthesizes_column_metadata_from_description():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("id",), ("name",)]
    cur.fetchall.return_value = [(1, "a")]
    out = MySQLDataApiAdapter(conn).execute_statement(sql="SELECT id, name FROM t")
    assert out["columnMetadata"] == [{"name": "id"}, {"name": "name"}]


def test_adapter_surfaces_rowcount_when_description_is_none():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = None
    cur.rowcount = 3
    out = MySQLDataApiAdapter(conn).execute_statement(sql="UPDATE t SET x=1")
    assert out == {"records": [], "columnMetadata": [], "numberOfRecordsUpdated": 3}
    cur.fetchall.assert_not_called()


def test_adapter_rejects_parameters():
    conn = MagicMock()
    with pytest.raises(ValueError):
        MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1", parameters=[{"name": "x"}])


def test_connect_fails_closed_when_ca_bundle_missing(monkeypatch):
    import mcp_servers.shared.mysql_direct as mysql_direct

    monkeypatch.setattr(mysql_direct, "_CA_BUNDLE_PATH", "/nonexistent/global-bundle.pem")
    with pytest.raises(RuntimeError):
        connect("host", 3306, "db", "user", "pw")
