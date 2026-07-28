"""E-4 engine gate: the schema-snapshot readers must REFUSE a family that has no
schema, and must fail closed on a cluster whose registry lookup fails.

The gate reuses the existing `sql` capability, which is already exactly the right
predicate. What must never happen is the alternative: a dynamodb or elasticache
cluster falling through to an empty table and getting a clean zero-diff, i.e. the
tool telling a DBA "no schema changes" about a thing that has no schema.

The impl is replaced by a spy in every case, so a refusal that still ran the
query would fail here.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.operations.handler as handler  # noqa: E402

_TOOLS = ["get_schema_diff", "get_schema_history"]


class _Ctx:
    def __init__(self, tool_name):
        self.client_context = MagicMock()
        self.client_context.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


def _invoke(tool_name, event):
    return json.loads(handler.lambda_handler(event, _Ctx(tool_name))["content"][0]["text"])


@pytest.mark.parametrize("tool", _TOOLS)
def test_both_tools_are_gated(tool):
    assert handler._ENGINE_GATED_TOOLS[tool] == "sql"


def test_cap_label_exists_so_the_refusal_is_readable():
    """Without a _CAP_LABEL entry the Korean refusal interpolates the raw key
    ("sql는 ... 전용입니다"), which is not a sentence a DBA should be shown."""
    assert "sql" in handler._CAP_LABEL
    assert handler._CAP_LABEL["sql"] != "sql"


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("family", ["dynamodb", "documentdb", "elasticache"])
def test_schemaless_family_refused_not_answered_empty(tool, family):
    spy = MagicMock()
    with patch.object(handler, "_resolve_family", lambda cid: family), \
            patch.dict(handler.TOOLS[tool], {"impl": spy}):
        result = _invoke(tool, {"cluster_id": f"{family}-1"})
    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] == family
    spy.assert_not_called()


@pytest.mark.parametrize("tool", _TOOLS)
def test_unresolvable_cluster_fails_closed(tool):
    """Registry lookup failure / unregistered cluster: family is None. That must
    be a refusal, NOT a query against an empty table that answers "no changes"."""
    spy = MagicMock()
    with patch.object(handler, "_resolve_family", lambda cid: None), \
            patch.dict(handler.TOOLS[tool], {"impl": spy}):
        result = _invoke(tool, {"cluster_id": "ghost-cluster"})
    assert result["status"] == "unsupported_engine"
    assert result["engine_family"] is None
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("family", ["relational", "rds_instance"])
def test_sql_families_pass_through(tool, family):
    """relational and rds_instance both have `sql`, so the gate must let them in
    and leave the honest-empty-state handling to the reader itself."""
    spy = MagicMock(return_value={"status": "not_collected"})
    with patch.object(handler, "_resolve_family", lambda cid: family), \
            patch.dict(handler.TOOLS[tool], {"impl": spy}):
        result = _invoke(tool, {"cluster_id": "prod-1"})
    assert result["status"] == "not_collected"
    spy.assert_called_once()
