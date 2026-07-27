"""No MCP handler may echo exception text into its RESPONSE.

The catch-all `except Exception as e: return {"error": str(e)}` shape used to
live in all four handlers. Exception text here carries SQL fragments, secret
ARNs, hostnames and internal paths, and the response goes straight into the
agent transcript the DBA reads. The performance and operations handlers were
cleaned first; this file covers ALL four so the class cannot come back in the
two that were missed (incident, simulation).

Diagnostics belong in CloudWatch via logger.exception, never in the payload."""

import ast
import json
import os
import pathlib
import textwrap
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


# ---------------------------------------------------------------------------
# Tree-wide source scan
# ---------------------------------------------------------------------------
# The four handler catch-alls above were only the visible tip. The same class
# lives in individual tools and REST handlers: an AWS/driver exception rendered
# into the `reason`/`error`/`message` a DBA reads. AWS error text routinely
# carries the hub account id, the platform IAM role name, target ARNs and SQL
# fragments, so the RULE is absolute: static Korean reason in the payload,
# `logger.<level>(..., exc_info=True)` for the detail.
#
# This is a deliberately practical detector, not a sound taint analysis. It
# looks for the exception name (or a local that was assigned its text) reaching
# a returned expression, a response-shaped dict value, a response-shaped keyword
# argument, or a subscript assignment. Comparisons are skipped (classifying on
# `"AccessDenied" in str(e)` leaks nothing) and so are logger/print calls, which
# are the sanctioned destination.

_REPO = pathlib.Path(__file__).resolve().parents[3]
_SCAN_ROOTS = (_REPO / "mcp-servers" / "mcp_servers", _REPO / "api")

# Response-shaped field STEMS, matched as substrings so the one list covers
# `error`, `connection_error`, `error_message`, `failure_reason`, `last_err`.
# Enumerating exact field names missed `connection_error`, which the registry
# persists and the dashboard reads straight back out.
_RESPONSE_FIELD_STEMS = ("error", "err", "reason", "message", "msg", "detail", "text", "note")


def _is_response_field(name) -> bool:
    return isinstance(name, str) and any(s in name.lower() for s in _RESPONSE_FIELD_STEMS)


# Attributes that ARE the exception message. Deliberately excluded:
# `e.response["Error"]["Code"]`, a bounded AWS error code (AccessDenied,
# ThrottlingException). Several tools keep the code on purpose, it is an enum,
# not free text. ponytail: if a leak ever hides behind another attribute, add it
# here rather than flagging every `e.<attr>` chain.
_MESSAGE_ATTRS = frozenset({"args", "message", "msg", "reason", "strerror"})

# Sites the response-leak sweep deliberately kept, as ("<repo-relative path>",
# <line>) pairs. EMPTY ON PURPOSE: at the time this guard was written no site
# had been justified.
#
# To add one you must be able to state, in the comment beside the entry, why the
# text is safe to show a DBA: it is a value THIS code constructed from known
# inputs (a parameter name the caller passed, an AWS error code from a bounded
# enum), never free-form text from an AWS or driver exception. "It is useful for
# debugging" is not a justification: that is what CloudWatch is for.
_ALLOWLIST: tuple[tuple[str, int], ...] = ()


# Helpers that take an exception and are VERIFIED to return caller-safe text:
# they log the detail to CloudWatch and return a static reason (or a bool).
# Recognising them by name keeps this guard stable while line numbers move.
#
# Before adding a name here, READ the helper and confirm it cannot return
# exception text on any path. `_conn_error` maps the AWS error CODE to a static
# Korean reason; `_ec_not_found` returns a bool.
_SANITIZERS = frozenset({"_conn_error", "_ec_not_found"})


def _called_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def _is_clean_call(node: ast.AST) -> bool:
    """Calls that may legitimately receive exception text.

    logger.warning(...) / print(...) / self.log.error(...) are the sanctioned
    destination, and a verified sanitizer launders the exception into a static
    reason before it reaches the payload.
    """
    if not isinstance(node, ast.Call):
        return False
    if _called_name(node) in _SANITIZERS:
        return True
    func = node.func
    if isinstance(func, ast.Name):
        # isinstance/issubclass yield a bool, exactly like a comparison. Missing
        # this tainted `code = e.response[...] if isinstance(e, ClientError)...`,
        # flagging the bounded AWS error code the write tools keep on purpose.
        return func.id in ("print", "warn", "isinstance", "issubclass")
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return any("log" in p.lower() for p in parts)


def _leaks(node: ast.AST, names: frozenset, exc_names: frozenset = frozenset()) -> bool:
    """True when `node` renders one of `names` as text somewhere inside it."""
    if node is None:
        return False
    # A comparison yields a bool. `if "modifying" in str(e)` is classification.
    if isinstance(node, ast.Compare):
        return False
    if _is_clean_call(node):
        return False
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        # An attribute read directly off the exception OBJECT: only `.args` and
        # friends are the message. Anything else (`e.response["Error"]["Code"]`)
        # is a bounded AWS code that write tools keep on purpose.
        #
        # This guard must NOT block the descent in general: `str(e).lower()`
        # parses as Attribute(value=Call(str, e)), so returning False here for
        # every non-message attribute hid the leak inside the attribute's value.
        if isinstance(node.value, ast.Name) and node.value.id in exc_names:
            return node.attr in _MESSAGE_ATTRS
        return _leaks(node.value, names, exc_names)
    return any(_leaks(child, names, exc_names) for child in ast.iter_child_nodes(node))


def _tainted_names(scope: ast.AST) -> tuple:
    """(tainted names, exception-bound names) for `scope`.

    Scope is the whole FUNCTION, not just the except block: the common leak is
    `except Exception as e: connection_error = str(e)` followed by a response
    dict built AFTER the try/except. Handler-local scanning cannot see that.

    Two passes catch the second hop (`a = str(e); b = a`); nobody writes a third.
    A later assignment of non-exception text KILLS the name, so a function that
    classifies on `msg = str(e)` and then reuses `msg` for a static reason does
    not get flagged.
    """
    exc_names = frozenset(
        h.name for h in ast.walk(scope) if isinstance(h, ast.ExceptHandler) and h.name
    )
    if not exc_names:
        return frozenset(), exc_names

    # `for e in edges:` / `lambda e: ...` / `sum(1 for e in endpoints)` rebind the
    # name to something ordinary. api/dashboard/handler.py does exactly this in a
    # function that also has an `except ... as e`, and without these kills the
    # loop body reads as a leak. Comprehension and lambda bindings never hold
    # exception text at all, so they are killed for the whole scope; a `for`
    # target is killed in source order, because `except ... as e` appearing
    # AFTER a loop must still taint.
    shadowed: set = set()
    for n in ast.walk(scope):
        if isinstance(n, ast.comprehension):
            shadowed |= {s.id for s in ast.walk(n.target) if isinstance(s, ast.Name)}
        elif isinstance(n, ast.Lambda):
            # Lambda params are ast.arg, NOT ast.Name, so walking for Name here
            # silently collects nothing.
            a = n.args
            shadowed |= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}

    events = sorted(
        (
            n
            for n in ast.walk(scope)
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.ExceptHandler))
        ),
        key=lambda n: (n.lineno, n.col_offset),
    )
    names: set = set()
    for _ in range(2):
        for node in events:
            if isinstance(node, ast.ExceptHandler):
                if node.name:
                    names |= {node.name}
                continue
            if isinstance(node, (ast.For, ast.AsyncFor)):
                targets, leaky = [node.target], False
            else:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                leaky = _leaks(node.value, frozenset(names), exc_names)
            bound = {s.id for t in targets for s in ast.walk(t) if isinstance(s, ast.Name)}
            names = (names | bound) if leaky else (names - bound)
    return frozenset(names) - shadowed, exc_names


def _scan_source(source: str) -> list:
    """Report (line, kind) for every exception-text-into-response site.

    Scopes are functions. Module level is skipped: every handler here lives in a
    function, and pooling taint across a whole module would flag the world.
    """
    tree = ast.parse(source)
    found = {}
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in functions:
        names, exc = _tainted_names(scope)
        if not names:
            continue
        for node in ast.walk(scope):
            if isinstance(node, ast.Return) and _leaks(node.value, names, exc):
                found.setdefault(node.lineno, "returned")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key, ast.Constant)
                        and _is_response_field(key.value)
                        and _leaks(value, names, exc)
                    ):
                        found.setdefault(getattr(value, "lineno", node.lineno), "dict value")
            elif isinstance(node, ast.Call) and not _is_clean_call(node):
                for kw in node.keywords:
                    if _is_response_field(kw.arg) and _leaks(kw.value, names, exc):
                        found.setdefault(kw.value.lineno, "kwarg")
            elif isinstance(node, ast.Assign) and _leaks(node.value, names, exc):
                # resp["error"] = str(e) / errors_by_region[r] = str(e)
                if any(isinstance(t, ast.Subscript) for t in node.targets):
                    found.setdefault(node.lineno, "response field assign")
    return sorted(found.items())


# The shapes this guard must catch, and the ones it must stay quiet about. A
# detector that silently stops detecting is worse than no detector: once the
# tree is clean the scan below goes green, and nothing else would notice if a
# refactor broke it. Every case here is a shape observed in this repo.
_MUST_CATCH = (
    'return {"error": str(e)}',
    'return {"reason": f"조회 실패: {str(e)[:200]}"}',
    'return _resp(500, {"error": str(e)[:300]})',
    'return _resp("error", reason=f"연결 실패: {str(e)[:160]}")',
    'return {"error": repr(e)}',
    'return {"detail": str(e).lower()}',
    'msg = str(e)\nreturn {"error": msg}',
    'a = str(e)\nb = a\nreturn {"reason": b}',
    'conn_err = str(e)[:300]\nitem = {"connection_error": conn_err}\nreturn _resp(200, item)',
    'out = {}\nout["error"] = str(e)\nreturn out',
    'return {"message": "실패: " + str(e)}',
    'return {"error": e.args[0]}',
    'return {"errors": [str(e)]}',
)
_MUST_ALLOW = (
    'logger.warning("failed", exc_info=True)\nreturn {"reason": "실패했습니다."}',
    'print(f"[tool] failed: {e}")\nreturn {"reason": "실패했습니다."}',
    'logger.exception("failed for %s: %s", cid, str(e))\nreturn {"reason": "실패."}',
    'if "NotFound" in str(e):\n    return {"status": "not_found"}\nreturn {"reason": "실패."}',
    'msg = str(e).lower()\nreturn {"status": "gone" if "not exist" in msg else "error"}',
    'code = e.response.get("Error", {}).get("Code", "")\nreturn {"reason": f"실패 ({code})"}',
    'code = e.response["Error"]["Code"] if isinstance(e, ClientError) else ""\n'
    'return {"reason": f"실패 ({code})"}',
    'return {"reason": f"파라미터 {parameter_name} 적용에 실패했습니다."}',
    'return {"error": _conn_error(e, "register")}',
    'for e in edges:\n    deg[e["src"]] = 1\nreturn {"error": "정적 사유"}',
    'return {"count": sum(1 for e in rows if e.get("x"))}',
    'return {"error": ", ".join(sorted(names, key=lambda e: e.rank))}',
    'msg = str(e)\nmsg = "정적 사유"\nreturn {"error": msg}',
)


def _wrap(body: str) -> str:
    """Put a snippet inside a realistic `try/except Exception as e:` function."""
    return (
        "def f(cid, parameter_name):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        + textwrap.indent(body, "        ")
        + "\n"
    )


def test_the_detector_actually_detects():
    """Self-check: the scan must flag every known leak shape."""
    missed = [s for s in _MUST_CATCH if not _scan_source(_wrap(s))]
    assert not missed, "detector went blind on:\n" + "\n".join(f"  {m!r}" for m in missed)


def test_the_detector_allows_the_sanctioned_shapes():
    """Self-check: logging, classifying and static reasons must stay quiet, or
    the scan turns into noise the next author disables."""
    noisy = [s for s in _MUST_ALLOW if _scan_source(_wrap(s))]
    assert not noisy, "detector false-positives on:\n" + "\n".join(f"  {n!r}" for n in noisy)


def test_no_exception_text_reaches_a_response_payload():
    """Tree-wide guard: raw exception text must never reach a response payload.

    Scope is mcp-servers/mcp_servers plus api/. `tests`/`test` DIRECTORIES are
    skipped, but NOT files named test_*: `operations/tools/
    test_elasticache_failover.py` is a production tool (the `test_failover`
    ElastiCache API), not a test module.
    """
    offenders = []
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if {"tests", "test", "__pycache__"} & set(path.parts):
                continue
            rel = path.relative_to(_REPO).as_posix()
            for line, kind in _scan_source(path.read_text(encoding="utf-8")):
                if (rel, line) not in _ALLOWLIST:
                    offenders.append(f"  {rel}:{line} ({kind})")

    assert not offenders, (
        f"{len(offenders)} site(s) put exception text into a response payload:\n"
        + "\n".join(offenders)
        + "\n\nAWS/driver exception text carries the hub account id, the platform"
        "\nIAM role name, target ARNs and SQL fragments, and the response is read"
        "\nby a DBA in the agent transcript."
        "\n\nFIX: replace it with a STATIC Korean reason and log the detail with"
        "\n`logger.warning(..., exc_info=True)`. Copy the pattern in"
        "\nmcp-servers/mcp_servers/operations/tools/modify_parameter.py or"
        "\nset_docdb_profiler.py. Do not change status values or control flow."
        "\nIf the caller genuinely needs a detail (which parameter was rejected),"
        "\nbuild that field from the known INPUTS, never from the exception."
        "\n\nOR: if the text really is safe (a value this code constructed, a"
        "\nbounded AWS error code), add ('<path>', <line>) to _ALLOWLIST in this"
        "\nfile with a comment saying why. An unjustified entry is a leak."
    )
