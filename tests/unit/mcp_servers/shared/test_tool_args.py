"""Argument validation: one misnamed key must be correctable, not opaque.

The failure this replaces: every handler calls `impl(cache, **event)`, so an
unexpected key raised TypeError, and the handler's `except Exception` correctly
refuses to echo exception text, which removed the only part naming the bad
argument. The caller got "도구 실행 중 내부 오류가 발생했습니다" with nothing to act on.

The caller is an LLM. While probing all 64 tools with `tools/list` output open, I
sent `get_runbook` an undeclared `cluster_id` and `search_logs` an undeclared
`pattern` instead of `query`. Both looked like tool defects until a self-audit
diffed the arguments against the schema.
"""

import json
from unittest.mock import MagicMock

import pytest
from mcp_servers.shared.tool_args import accepted_params, invalid_argument_error


def _impl(cache, cluster_id, hours=6, limit=10):
    return {"ok": True}


def _impl_with_catchall(cache, cluster_id, **_ignored):
    return {"ok": True}


# --------------------------------------------------------------------------
# signature introspection
# --------------------------------------------------------------------------

def test_cache_is_not_an_accepted_argument():
    """Handlers pass `cache` positionally, and it is not in the published schema,
    so a caller naming it is an error like any other."""
    names, has_var_kw = accepted_params(_impl)
    assert names == {"cluster_id", "hours", "limit"}
    assert has_var_kw is False


def test_catchall_is_detected():
    names, has_var_kw = accepted_params(_impl_with_catchall)
    assert names == {"cluster_id"}
    assert has_var_kw is True


def test_uninspectable_callable_is_permitted_not_refused(monkeypatch):
    """When introspection fails there is no signature to judge against, so the
    check fails OPEN: refusing every call to such a tool would be worse than
    passing through.

    Driven by making inspect.signature raise rather than by picking a "builtin",
    because builtins like len() DO expose a signature in modern Python and the test
    would then be asserting the wrong reason for the right outcome.
    """
    import mcp_servers.shared.tool_args as ta

    def _boom(_):
        raise ValueError("no signature for this callable")

    monkeypatch.setattr(ta.inspect, "signature", _boom)
    names, has_var_kw = ta.accepted_params(_impl)
    assert names == frozenset()
    assert has_var_kw is True
    assert ta.invalid_argument_error("t", _impl, {"anything": 1}) is None


# --------------------------------------------------------------------------
# the check itself
# --------------------------------------------------------------------------

def test_a_wellformed_call_is_permitted():
    assert invalid_argument_error("t", _impl, {"cluster_id": "c1", "hours": 3}) is None


def test_an_unknown_argument_is_refused_and_names_the_fix():
    err = invalid_argument_error("search_logs", _impl, {"cluster_id": "c1", "pattern": "ERROR"})
    assert err["status"] == "invalid_arguments"
    assert err["unknown_arguments"] == ["pattern"]
    # The accepted names are what makes it self-correctable in one turn. They are
    # already published in cdk/tool_definitions.py and by tools/list, so this is
    # categorically different from echoing str(e).
    assert err["accepted_arguments"] == ["cluster_id", "hours", "limit"]
    assert "pattern" in err["reason"]
    assert "변경되지 않았습니다" in err["reason"]


def test_the_transport_method_key_is_not_an_argument():
    """`method` belongs to the MCP transport (tools/list), not to any tool."""
    assert invalid_argument_error("t", _impl, {"cluster_id": "c1", "method": "tools/call"}) is None


def test_a_catchall_impl_is_passed_through_unchanged():
    """22 of the 64 impls declare **_ignored and have deliberately opted into
    tolerating extras. Tightening those is a separate change with a different blast
    radius, so this check must not silently start refusing them."""
    assert invalid_argument_error("t", _impl_with_catchall, {"cluster_id": "c1", "bogus": 1}) is None


def test_multiple_unknowns_are_all_reported():
    err = invalid_argument_error("t", _impl, {"cluster_id": "c1", "a": 1, "b": 2})
    assert err["unknown_arguments"] == ["a", "b"]


def test_a_non_dict_event_is_permitted():
    for event in (None, [], "x"):
        assert invalid_argument_error("t", _impl, event) is None


# --------------------------------------------------------------------------
# wired into all four handlers, AFTER the engine gate
# --------------------------------------------------------------------------

_HANDLERS = ("performance", "incident", "operations", "simulation")


def _ctx(tool_name):
    ctx = MagicMock()
    ctx.client_context = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": tool_name}
    return ctx


@pytest.mark.parametrize("server", _HANDLERS)
def test_every_handler_refuses_an_unknown_argument(server, monkeypatch):
    import importlib
    import os

    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
    os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:t")
    os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:t")
    handler = importlib.import_module(f"mcp_servers.{server}.handler")

    # A tool with no **kwargs catch-all, so the check applies.
    tool = next(
        (name for name, t in handler.TOOLS.items()
         if not accepted_params(t["impl"])[1]),
        None,
    )
    assert tool, f"{server}: no non-catchall tool to exercise"

    # A REAL function, not a MagicMock. MagicMock reports a (*args, **kwargs)
    # signature, so substituting one erases the very thing this guard inspects and
    # the check silently passes everything. Same shape of mistake as mocking a
    # QueryResult as a plain list: the fixture has to preserve the property under
    # test.
    called = []

    def stub(cache, cluster_id=None):
        called.append(cluster_id)
        return {"status": "ok"}

    monkeypatch.setitem(handler.TOOLS[tool], "impl", stub)
    # Resolve a permissive family so the engine gate cannot be what refuses.
    if hasattr(handler, "_resolve_family"):
        monkeypatch.setattr(handler, "_resolve_family", lambda cid: "relational")

    raw = handler.lambda_handler(
        {"cluster_id": "c1", "definitely_not_a_real_argument": 1}, _ctx(tool))
    body = json.loads(raw["content"][0]["text"])
    assert body["status"] == "invalid_arguments", f"{server}/{tool}: {body}"
    assert "definitely_not_a_real_argument" in body["unknown_arguments"]
    # The whole point: the tool did not run, so nothing changed.
    assert called == []


def test_the_engine_gate_still_wins_over_an_argument_complaint(monkeypatch):
    """Ordering matters. A wrong-engine call should get the engine answer, which is
    more informative than an argument complaint about a tool that would not have
    applied anyway."""
    import importlib
    import os

    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
    os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:t")
    os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:t")
    handler = importlib.import_module("mcp_servers.operations.handler")

    gated = next(iter(handler._ENGINE_GATED_TOOLS))
    monkeypatch.setattr(handler, "_resolve_family", lambda cid: "dynamodb")
    spy = MagicMock()
    monkeypatch.setitem(handler.TOOLS[gated], "impl", spy)

    raw = handler.lambda_handler(
        {"cluster_id": "ddb-1", "definitely_not_a_real_argument": 1}, _ctx(gated))
    body = json.loads(raw["content"][0]["text"])
    assert body["status"] == "unsupported_engine", body
    spy.assert_not_called()
