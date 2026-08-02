"""Tests for the performance-handler POSITIVE FAIL-CLOSED engine gate.

Before the gate, the query tools ran their cache SQL for ANY engine: DynamoDB /
ElastiCache got an EMPTY LIST back, which a DBA reads as "no heavy queries", a
false empty state, not a refusal. explain_plan/recommend_index built PG-only
output unconditionally. Now every gated tool must answer `unsupported_engine`
for a family that lacks the capability, and an unresolvable cluster is REFUSED
(fail-closed), with the impl never reached."""

import json
import os
from unittest.mock import MagicMock, patch

# The performance handler instantiates CacheClient() at import time, which reads
# CACHE_DB_CLUSTER_ARN. Set a dummy before import so this file collects in
# isolation too (mirrors operations/test_operations_engine_gate.py). The cache is
# never queried, _resolve_family is monkeypatched in every test.
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.performance.handler as handler  # noqa: E402

# tool → (capability, a minimal valid event) and the families that must REFUSE.
_GATED = {
    "get_top_queries": {"cluster_id": "x"},
    "get_slow_queries": {"cluster_id": "x"},
    "detect_regressions": {"cluster_id": "x", "change_point": "2026-07-01T00:00:00Z"},
    "explain_plan": {"cluster_id": "x", "sql": "SELECT 1"},
    "recommend_index": {"cluster_id": "x"},
    # Performance Insights is an RDS/Aurora feature. Gated 2026-08-02 after a live
    # probe found it returning {"data_points": [], "count": 0} with no status and no
    # reason for DocumentDB, DynamoDB and ElastiCache, which have no PI series at
    # all: a silent empty that reads as "nothing happening" rather than "wrong
    # question for this engine".
    "get_pi_metrics": {"cluster_id": "x"},
}


class _Ctx:
    def __init__(self, tool_name):
        self.client_context = MagicMock()
        self.client_context.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


def _invoke(tool_name, event):
    result = handler.lambda_handler(event, _Ctx(tool_name))
    return json.loads(result["content"][0]["text"])


def _patch_family(fam):
    return patch.object(handler, "_resolve_family", lambda cid: fam)


def test_gate_map_matches_capability_keys():
    """Guards the wiring itself: every gated tool exists and its capability key
    is a real CAPABILITIES key (a typo would silently deny every engine)."""
    assert set(handler._ENGINE_GATED_TOOLS) == set(_GATED)
    known = set(handler.CAPABILITIES["relational"])
    for tool, cap in handler._ENGINE_GATED_TOOLS.items():
        assert tool in handler.TOOLS, tool
        assert cap in known, (tool, cap)
        assert cap in handler._CAP_LABEL, cap


def test_unsupported_families_get_unsupported_engine_not_empty_list():
    """DynamoDB / ElastiCache have no query_stats producer at all, so every gated
    tool must REFUSE them rather than hand back an empty result set. DocumentDB
    has a producer (the profiler-log collector) but no SQL surface, so it is
    refused for the SQL-shaped tools only."""
    for fam in ("dynamodb", "elasticache"):
        for tool, event in _GATED.items():
            spy = MagicMock(return_value={"queries": []})
            with _patch_family(fam), patch.dict(handler.TOOLS[tool], {"impl": spy}):
                result = _invoke(tool, event)
            assert result["status"] == "unsupported_engine", (fam, tool)
            assert result["engine_family"] == fam
            spy.assert_not_called()
    for tool in ("explain_plan", "recommend_index"):
        spy = MagicMock(return_value={"queries": []})
        with _patch_family("documentdb"), patch.dict(handler.TOOLS[tool], {"impl": spy}):
            result = _invoke(tool, _GATED[tool])
        assert result["status"] == "unsupported_engine", tool
        assert result["engine_family"] == "documentdb"
        spy.assert_not_called()


def test_gate_none_family_fail_closed():
    """An unresolvable cluster (missing row / lookup error) is REFUSED, not
    allowed through with a default engine."""
    for tool, event in _GATED.items():
        spy = MagicMock()
        with _patch_family(None), patch.dict(handler.TOOLS[tool], {"impl": spy}):
            result = _invoke(tool, event)
        assert result["status"] == "unsupported_engine", tool
        # actionable + static: names the two real causes, leaks no exception text
        assert "엔진을 확인할 수 없습니다" in result["reason"]
        assert "수집" in result["reason"]
        spy.assert_not_called()


def test_rds_instance_has_query_stats_but_not_explain_or_index_advice():
    """rds_instance collects query_stats (direct-TCP collectors) so the query
    tools run, but explain/index_advice are PG-only today and must refuse."""
    for tool in ("get_top_queries", "get_slow_queries", "detect_regressions"):
        spy = MagicMock(return_value={"status": "ok"})
        with _patch_family("rds_instance"), patch.dict(handler.TOOLS[tool], {"impl": spy}):
            assert _invoke(tool, _GATED[tool])["status"] == "ok", tool
        spy.assert_called_once()
    for tool in ("explain_plan", "recommend_index"):
        spy = MagicMock()
        with _patch_family("rds_instance"), patch.dict(handler.TOOLS[tool], {"impl": spy}):
            result = _invoke(tool, _GATED[tool])
        assert result["status"] == "unsupported_engine", tool
        spy.assert_not_called()


def test_relational_passes_every_gated_tool():
    for tool, event in _GATED.items():
        spy = MagicMock(return_value={"status": "ok"})
        with _patch_family("relational"), patch.dict(handler.TOOLS[tool], {"impl": spy}):
            assert _invoke(tool, event)["status"] == "ok", tool
        spy.assert_called_once()


def test_ungated_tools_stay_ungated():
    """Genuinely engine-agnostic cache reads are NOT gated, so a DynamoDB cluster
    still reaches their impls.

    The example used to be get_pi_metrics, and that premise was wrong: Performance
    Insights is an RDS/Aurora feature, so for DynamoDB the tool was not
    engine-agnostic, it was silently empty. detect_anomalies is the honest example
    because it reads metric_snapshots, which EVERY family has a collector for.
    """
    spy = MagicMock(return_value={"status": "ok"})
    with _patch_family("dynamodb"), patch.dict(handler.TOOLS["detect_anomalies"], {"impl": spy}):
        assert _invoke("detect_anomalies", {"cluster_id": "ddb-1"})["status"] == "ok"
    spy.assert_called_once()


def test_tool_exception_returns_a_static_reason_with_no_exception_text():
    """A raising impl must never echo the exception into the RESPONSE: the text
    carries SQL fragments, ARNs and internal paths straight to the chat UI.
    Static reason + logger.exception only."""
    secret = "relation cluster_meta does not exist at arn:aws:rds:secret"
    spy = MagicMock(side_effect=RuntimeError(secret))
    with _patch_family("relational"), patch.dict(handler.TOOLS["get_top_queries"], {"impl": spy}):
        raw = handler.lambda_handler({"cluster_id": "x"}, _Ctx("get_top_queries"))
    text = raw["content"][0]["text"]
    assert secret not in text
    assert "RuntimeError" not in text and "Traceback" not in text
    result = json.loads(text)
    assert result["status"] == "tool_error"
    assert result["tool"] == "get_top_queries"
    assert result["reason"]


def test_resolve_family_fails_closed_on_lookup_error():
    """_resolve_family itself: empty cluster_id, missing row and a raising cache
    all resolve to None (which the gate turns into a refusal)."""
    assert handler._resolve_family("") is None
    with patch.object(handler.cache, "execute", return_value=MagicMock(rows=[])):
        assert handler._resolve_family("ghost") is None
    with patch.object(handler.cache, "execute", side_effect=RuntimeError("boom")):
        assert handler._resolve_family("ghost") is None
    with patch.object(handler.cache, "execute",
                      return_value=MagicMock(rows=[{"engine": "aurora-mysql"}])):
        assert handler._resolve_family("prod-1") == "relational"
