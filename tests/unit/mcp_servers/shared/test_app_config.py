"""Tests for the shared app_config get_config helper."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_PKG_ROOT = Path(__file__).resolve().parents[4] / "mcp-servers"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import mcp_servers.shared.app_config as app_config  # noqa: E402


def setup_function():
    app_config._CACHE.clear()  # isolate cache between tests


def _table_with(value):
    t = MagicMock()
    t.get_item.return_value = {"Item": {"config_key": "K", "value": value}} if value is not None else {}
    return t


def test_db_value_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    with patch.object(app_config, "_table", return_value=_table_with("db")):
        assert app_config.get_config("K", "default") == "db"


def test_env_fallback_when_no_row(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    with patch.object(app_config, "_table", return_value=_table_with(None)):
        assert app_config.get_config("K", "default") == "env"


def test_default_when_no_row_no_env(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.delenv("K", raising=False)
    with patch.object(app_config, "_table", return_value=_table_with(None)):
        assert app_config.get_config("K", "default") == "default"


def test_ddb_error_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("APP_CONFIG_TABLE", "t")
    monkeypatch.setenv("K", "env")
    boom = MagicMock()
    boom.get_item.side_effect = RuntimeError("ddb down")
    with patch.object(app_config, "_table", return_value=boom):
        assert app_config.get_config("K", "default") == "env"


def test_no_table_env_uses_fallback(monkeypatch):
    monkeypatch.delenv("APP_CONFIG_TABLE", raising=False)
    monkeypatch.setenv("K", "env")
    assert app_config.get_config("K", "default") == "env"
