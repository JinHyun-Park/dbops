"""No MCP handler may echo exception text into its RESPONSE.

The catch-all `except Exception as e: return {"error": str(e)}` shape used to
live in all four handlers. Exception text here carries SQL fragments, secret
ARNs, hostnames and internal paths, and the response goes straight into the
agent transcript the DBA reads. The performance and operations handlers were
cleaned first; this file covers ALL four so the class cannot come back in the
two that were missed (incident, simulation).

Diagnostics belong in CloudWatch via logger.exception, never in the payload."""

import json
import os
from unittest.mock import MagicMock, patch

# Every handler builds CacheClient() at import time, which reads these.
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.incident.handler as incident_handler  # noqa: E402
import mcp_servers.performance.handler as performance_handler  # noqa: E402
import mcp_servers.simulation.handler as simulation_handler  # noqa: E402

# A realistic leak: this is the shape of a real Data API failure, and it embeds
# both a secret ARN and SQL.
SECRET = (
    "An error occurred (BadRequestException) when calling ExecuteStatement: "
    'relation "cluster_meta" does not exist; secret '
    "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf"
)


class _Ctx:
    """AgentCore passes the tool name via client_context.custom."""

    def __init__(self, tool_name):
        self.client_context = type("cc", (), {"custom": {"tool_name": tool_name}})()


def _assert_no_leak(raw, tool_name):
    text = raw["content"][0]["text"]
    assert SECRET not in text
    assert "secretsmanager" not in text
    assert "cluster_meta" not in text
    assert "RuntimeError" not in text and "Traceback" not in text
    result = json.loads(text)
    assert result["status"] == "tool_error"
    assert result["tool"] == tool_name
    assert result["reason"]
    return result


def test_incident_handler_raising_tool_returns_static_reason():
    tool = "get_health_status"
    spy = MagicMock(side_effect=RuntimeError(SECRET))
    with patch.dict(incident_handler.TOOLS[tool], {"impl": spy}):
        raw = incident_handler.lambda_handler({"cluster_id": "x"}, _Ctx(tool))
    _assert_no_leak(raw, tool)
    spy.assert_called_once()


def test_simulation_handler_raising_tool_returns_static_reason():
    tool = "check_upgrade_compatibility"
    spy = MagicMock(side_effect=RuntimeError(SECRET))
    # simulation gates on the engine family, so resolve to relational first.
    with patch.object(simulation_handler, "_resolve_family", lambda cid: "relational"), \
            patch.dict(simulation_handler.TOOLS[tool], {"impl": spy}):
        raw = simulation_handler.lambda_handler(
            {"cluster_id": "x", "target_version": "16.4"}, _Ctx(tool)
        )
    _assert_no_leak(raw, tool)
    spy.assert_called_once()


def test_performance_handler_still_clean():
    """Guards the already-fixed handler against regression."""
    tool = "get_top_queries"
    spy = MagicMock(side_effect=RuntimeError(SECRET))
    with patch.object(performance_handler, "_resolve_family", lambda cid: "relational"), \
            patch.dict(performance_handler.TOOLS[tool], {"impl": spy}):
        raw = performance_handler.lambda_handler({"cluster_id": "x"}, _Ctx(tool))
    _assert_no_leak(raw, tool)


def test_no_handler_source_contains_the_str_e_catch_all():
    """Belt and braces: the operations handler needs env/pymongo shims to import
    in this test process, so assert on the SOURCE for the whole family instead of
    importing it. Catches a reintroduction in any of the four."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "mcp-servers" / "mcp_servers"
    offenders = []
    for name in ("performance", "incident", "operations", "simulation"):
        src = (root / name / "handler.py").read_text(encoding="utf-8")
        if '{"error": str(e)}' in src or '"error": str(e)' in src:
            offenders.append(name)
    assert not offenders, f"str(e) returned to the response in: {offenders}"
