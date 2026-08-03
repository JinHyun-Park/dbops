"""tool_args — turn an argument mistake into something the caller can fix itself.

THE PROBLEM
-----------
Every MCP handler calls its tool as

    result = TOOLS[tool_name]["impl"](cache, **(event or {}))

so the event dict is trusted as a kwargs bag. One key the impl does not accept
raises TypeError, and the handler's `except Exception` correctly refuses to echo
exception text (it can carry SQL, ARNs and host names), which strips the only part
that said WHICH argument was wrong. The caller gets "도구 실행 중 내부 오류가
발생했습니다" and has no way to correct itself, so it retries the same call or gives
up.

The caller is an LLM. This is not hypothetical: while probing all 64 tools for this
exact class of bug, with `tools/list` output open, I sent `get_runbook` an
undeclared `cluster_id` and `search_logs` an undeclared `pattern` instead of
`query`. Both surfaced as generic internal errors and both looked like tool
defects until a self-audit diffed the arguments against the schema. A careful caller
holding the schema got it wrong twice.

WHY REFUSE RATHER THAN DROP, AND WHY A CATCH-ALL IS NOT AN EXEMPTION
-------------------------------------------------------------------
Silently dropping an unknown key turns "you named an argument wrong" into "proceed
with the default", and 22 of the 64 impls do exactly that via `**_ignored`.

The first version of this module treated those 22 as having opted into tolerating
extras and permitted any call to them. That lasted about an hour: calling
simulate_elasticache_node_resize with `node_type` (the real parameter is
`new_node_type`) returned status "ok" with proposed == current, a confident "no cost
change" answer to a resize question that was never asked. A catch-all does not make
an out-of-schema key harmless, it makes it INVISIBLE, and it is arguably worse on a
READ tool than on a write one, because no approval card and no downstream check ever
looks at it again.

So the allow-list is the declared schema UNION the impl signature, and it applies
regardless of a catch-all. Refusing is fail-closed and, because the response names
the accepted parameters, self-correctable in one turn.

Naming the parameters is not a leak: they are already published in
`cdk/tool_definitions.py` and returned by `tools/list`. That is categorically
different from `str(e)`.

CALL IT AFTER THE ENGINE GATE. A wrong-engine call should get the engine answer,
which is more informative than an argument complaint about a tool that would not
have applied anyway.
"""

import inspect

# `method` is the transport's own key (tools/list), never a tool parameter.
_TRANSPORT_KEYS = frozenset({"method"})


def accepted_params(impl) -> tuple[frozenset, bool]:
    """(parameter names the impl accepts, whether it has a **kwargs catch-all).

    `names` is None when the callable cannot be introspected at all, which is
    DISTINCT from "introspected and found a catch-all". Conflating the two is a real
    bug: after the catch-all exemption was removed, an uninspectable impl reported an
    EMPTY name set and the check then refused every argument. None means "no
    signature information", and the caller must fall back rather than refuse.

    The leading `cache` parameter is excluded: handlers pass it positionally, so a
    caller naming it would be an error too, and it is not part of the tool's
    published schema.
    """
    try:
        sig = inspect.signature(impl)
    except (TypeError, ValueError):  # builtins / C callables: cannot introspect
        return None, True
    names, has_var_kw = [], False
    for i, (name, p) in enumerate(sig.parameters.items()):
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_kw = True
        elif p.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        elif i == 0 and name == "cache":
            continue
        else:
            names.append(name)
    return frozenset(names), has_var_kw


def invalid_argument_error(tool_name: str, impl, event, schema_props=None) -> dict | None:
    """The `invalid_arguments` payload, or None when the call is well-formed.

    The allow-list is the DECLARED SCHEMA properties UNION the impl signature, and
    it applies even to an impl with a `**kwargs` catch-all.

    An earlier version of this function permitted any call to a catch-all impl, on
    the theory that those 22 impls had opted into tolerating extras. That was wrong,
    and the counter-example arrived within the hour: calling
    simulate_elasticache_node_resize with `node_type` (the real parameter is
    `new_node_type`) returned status "ok" with proposed == current, i.e. a confident
    "no cost change" answer to a resize question that was never actually asked. A
    catch-all does not make an out-of-schema key harmless, it makes it INVISIBLE,
    and a silently substituted default is worse on a read tool than on a write tool
    because nothing downstream flags it.

    Both sources are needed, and the union is never more restrictive than either:
      * schema properties are the contract the agent sees (tools/list,
        cdk/tool_definitions.py), so they are what an out-of-schema key violates;
      * the impl signature covers params in `_INTENTIONALLY_HIDDEN`, which exist on
        purpose and are absent from the schema. Judging on the schema alone would
        refuse those.
    """
    if not isinstance(event, dict):
        return None
    sig_names, _has_var_kw = accepted_params(impl)
    if sig_names is None and not schema_props:
        # No signature information and no schema: nothing to judge against, so fail
        # OPEN. Refusing every call to such a tool would be worse than passing
        # through.
        return None
    names = (sig_names or frozenset()) | frozenset(schema_props or ())
    unknown = sorted(set(event) - names - _TRANSPORT_KEYS)
    if not unknown:
        return None
    return {
        "status": "invalid_arguments",
        "tool": tool_name,
        "unknown_arguments": unknown,
        "accepted_arguments": sorted(names),
        "reason": (
            f"{tool_name}이 받지 않는 인자입니다: {', '.join(unknown)}. "
            f"허용되는 인자는 {', '.join(sorted(names)) or '(없음)'} 입니다. "
            "인자 이름을 고쳐 다시 호출하세요. 이 도구는 실행되지 않았으므로 "
            "아무 것도 변경되지 않았습니다."
        ),
    }
