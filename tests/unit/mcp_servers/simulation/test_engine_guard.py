"""Simulation MCP handler engine guard (spec #5).

Simulation tools (upgrade/parameter/DDL/scaling) are Aurora-only. The handler
must refuse them for non-relational engines (documentdb/dynamodb) with a clear
`unsupported_engine` signal — mirroring the execute_sql guard (fix 9520191):
DEFAULT-PERMIT when the family is unknown / the cluster isn't in cluster_meta /
the cache lookup errors, so legacy and mock paths never false-positive.

The handler instantiates `cache = CacheClient()` at import, which reads env
vars — set dummy values BEFORE importing the handler (CacheClient.__init__ does
no AWS call, just reads env into attributes)."""

import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")

from mcp_servers.shared.models import QueryResult  # noqa: E402
from mcp_servers.simulation import handler  # noqa: E402


def _ctx(tool_name):
    """Fake AgentCore context exposing client_context.custom.bedrockAgentCoreToolName."""
    cc = MagicMock()
    cc.custom = {"bedrockAgentCoreToolName": tool_name}
    ctx = MagicMock()
    ctx.client_context = cc
    return ctx


def _run(tool_name, event, execute_return=None, execute_side_effect=None):
    """Invoke the handler with a patched module cache + a sentinel tool impl.
    Returns (parsed_body, sentinel_impl).

    `execute_return` is given as a list of row dicts for readability, and wrapped
    in a real QueryResult here because that is what CacheClient.execute actually
    returns. This wrapping is the whole point of the helper.

    Until 2026-08-02 the list was passed through raw, and that single detail made
    this entire test file false-green: the handler's `_resolve_family` did
    `isinstance(rows, list)` WITHOUT unwrapping `.rows`, so a list-shaped mock
    resolved the family correctly while production (a QueryResult) always resolved
    None. Every assertion below passed against a gate that was dead for all 9
    clusters in the deployed Lambda: three tools refused their own family and six
    Aurora-only tools answered for DynamoDB and Valkey. A fixture built from the
    handler's expectations rather than from the collaborator's real return type
    encodes the bug instead of catching it.
    """
    sentinel = MagicMock(return_value={"ok": True, "tool": tool_name})
    if isinstance(execute_return, list):
        execute_return = QueryResult(
            columns=["engine"],
            rows=execute_return,
            row_count=len(execute_return),
        )
    with patch.object(handler, "cache") as mock_cache, patch.dict(
        handler.TOOLS[tool_name], {"impl": sentinel}
    ):
        if execute_side_effect is not None:
            mock_cache.execute.side_effect = execute_side_effect
        else:
            mock_cache.execute.return_value = execute_return
        out = handler.lambda_handler(event, _ctx(tool_name))
    body = json.loads(out["content"][0]["text"])
    return body, sentinel


def test_blocks_dynamodb():
    body, impl = _run(
        "simulate_scaling", {"cluster_id": "ddb-abc123"},
        execute_return=[{"engine": "dynamodb"}],
    )
    assert body["status"] == "unsupported_engine"
    assert body["engine_family"] == "dynamodb"
    impl.assert_not_called()


def test_blocks_documentdb():
    body, impl = _run(
        "check_upgrade_compatibility",
        {"cluster_id": "dbops-docdb-test", "target_version": "5.0"},
        execute_return=[{"engine": "docdb"}],
    )
    assert body["status"] == "unsupported_engine"
    assert body["engine_family"] == "documentdb"
    impl.assert_not_called()


def test_allows_relational():
    body, impl = _run(
        "check_upgrade_compatibility",
        {"cluster_id": "prod-pg", "target_version": "16.4"},
        execute_return=[{"engine": "aurora-postgresql"}],
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()


def test_allows_unknown_cluster_default_permit():
    # cluster not in cluster_meta (empty rows) → permit (mirror execute_sql).
    body, impl = _run(
        "simulate_scaling", {"cluster_id": "not-registered"},
        execute_return=[],
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()


def test_ddb_capacity_cost_allows_dynamodb():
    # The DynamoDB-only tool uses a POSITIVE gate (ddb_cost_simulation): a
    # resolved dynamodb table must PASS where the Aurora tools refuse it.
    body, impl = _run(
        "simulate_dynamodb_capacity_cost", {"cluster_id": "ddb-abc123"},
        execute_return=[{"engine": "dynamodb"}],
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()


def test_ddb_capacity_cost_blocks_relational():
    # The same positive gate refuses Aurora (relational lacks ddb_cost_simulation).
    body, impl = _run(
        "simulate_dynamodb_capacity_cost", {"cluster_id": "prod-pg"},
        execute_return=[{"engine": "aurora-postgresql"}],
    )
    assert body["status"] == "unsupported_engine"
    assert body["engine_family"] == "relational"
    impl.assert_not_called()


def test_allows_when_cache_errors_default_permit():
    # cache lookup raises → permit (don't brick the tool on a transient cache error).
    body, impl = _run(
        "simulate_ddl_impact", {"cluster_id": "x", "ddl_sql": "ALTER TABLE t ADD COLUMN c int"},
        execute_side_effect=RuntimeError("cache down"),
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()


def test_allows_when_no_cluster_id_default_permit():
    # no cluster_id in event → can't resolve family → permit.
    body, impl = _run(
        "simulate_scaling", {},
        execute_return=[{"engine": "dynamodb"}],
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()


def test_elasticache_positive_gate_refuses_none_family():
    # elasticache_cost_simulation is a POSITIVE gate: only elasticache passes.
    # A None family (missing/error → _resolve_family=None) → .get(None,{}) → False → unsupported_engine.
    body, impl = _run(
        "simulate_elasticache_node_resize", {"cluster_id": "not-registered"},
        execute_return=[],  # empty rows → _resolve_family returns None
    )
    assert body["status"] == "unsupported_engine"
    assert body["engine_family"] is None
    impl.assert_not_called()


def test_elasticache_positive_gate_refuses_relational():
    # elasticache_cost_simulation POSITIVE gate: a resolved Aurora/MySQL cluster is refused.
    body, impl = _run(
        "simulate_elasticache_node_resize", {"cluster_id": "prod-aurora"},
        execute_return=[{"engine": "aurora-postgresql"}],
    )
    assert body["status"] == "unsupported_engine"
    assert body["engine_family"] == "relational"
    impl.assert_not_called()


def test_elasticache_positive_gate_allows_elasticache():
    # elasticache_cost_simulation POSITIVE gate: a resolved elasticache cluster is allowed.
    body, impl = _run(
        "simulate_elasticache_node_resize", {"cluster_id": "my-redis"},
        execute_return=[{"engine": "redis"}],
    )
    assert body.get("status") != "unsupported_engine"
    impl.assert_called_once()
