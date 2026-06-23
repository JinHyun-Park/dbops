"""Unit tests for _extract_usage helper in agent/server.py.

agent/server.py imports heavy runtime deps (strands, bedrock_agentcore, rpds…)
that are not installed in the unit-test environment. We extract the
_extract_usage function via ast — its body has zero external imports — compile
it into a minimal throwaway module, and test that compiled function directly.

teardown_module cleans agent/__pycache__ because the AgentCore Runtime deploy
rejects a __pycache__ directory under agent/.
"""

import ast
import shutil
import types
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[3] / "agent"
_SERVER = _AGENT / "server.py"


def _load_extract_usage():
    """Extract and compile _extract_usage from agent/server.py without importing
    the module (which would pull heavy deps unavailable in the test env)."""
    src = _SERVER.read_text()
    tree = ast.parse(src)

    # Find the _extract_usage FunctionDef node
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_extract_usage":
            func_node = node
            break
    if func_node is None:
        raise RuntimeError("_extract_usage not found in agent/server.py")

    # Wrap it in a minimal module AST so it compiles cleanly
    mod_ast = ast.Module(body=[func_node], type_ignores=[])
    ast.fix_missing_locations(mod_ast)
    code = compile(mod_ast, str(_SERVER), "exec")

    ns: dict = {}
    exec(code, ns)  # noqa: S102 — test-only, not untrusted input
    return ns["_extract_usage"]


def teardown_module(_):
    # AgentCore Runtime deploy rejects a __pycache__ under agent/ — clean it.
    pc = _AGENT / "__pycache__"
    if pc.exists():
        shutil.rmtree(pc, ignore_errors=True)


def test_extract_usage_present():
    """Real Strands shape: stream_async final event is {"result": AgentResult}.
    AgentResult.metrics is EventLoopMetrics; .accumulated_usage is a Usage
    TypedDict with inputTokens/outputTokens/totalTokens."""
    fn = _load_extract_usage()

    # Mirror the real Strands object graph:
    #   event["result"].metrics.accumulated_usage = {"inputTokens": N, "outputTokens": M, ...}
    accumulated_usage = {"inputTokens": 120, "outputTokens": 340, "totalTokens": 460}
    metrics = types.SimpleNamespace(accumulated_usage=accumulated_usage)
    result = types.SimpleNamespace(metrics=metrics)
    event = {"result": result}

    u = fn(event)
    assert u == {"input_tokens": 120, "output_tokens": 340}


def test_extract_usage_absent_returns_none():
    """Normal streaming text events ({"data": "..."}) have no result key."""
    fn = _load_extract_usage()
    assert fn({"data": "hello"}) is None


def test_extract_usage_malformed_no_raise():
    """Unexpected/malformed event shapes must never raise — return None."""
    fn = _load_extract_usage()
    assert fn({"result": "weird"}) is None
    assert fn(None) is None
