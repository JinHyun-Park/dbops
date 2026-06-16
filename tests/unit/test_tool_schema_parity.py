"""핸들러 ↔ Gateway 스키마 정합 회귀 테스트.

세 번 재발한 버그 패밀리의 영구 차단:
  1. request_approval이 스키마에서 통째로 누락 → 승인 루프 dead-end (P0)
  2. manage_maintenance/create_snapshot/restore_cluster에 approved/approval_id
     누락 → 승인 후 재실행 불가
  3. get_slow_queries 등 12개 툴의 시간창·튜닝 파라미터 누락 → 에이전트
     능력이 기본값으로 조용히 제한

핸들러에 파라미터를 추가하면 이 테스트가 스키마 추가를 강제한다. 반대
방향(스키마에만 있는 파라미터)은 핸들러가 **kwargs 없이 TypeError를 내므로
역시 잡는다. cdk 패키지 임포트 없이 텍스트 파싱 — CI에 aws-cdk 불필요.
"""

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA_SRC = (_REPO / "cdk" / "tool_definitions.py").read_text()
_TOOLS_ROOT = _REPO / "mcp-servers" / "mcp_servers"
_CEDAR_ROOT = _REPO / "cdk" / "policies" / "cedar"

# READ-ONLY MCP 서버 → 그 서버 전 툴이 permit 되어야 하는 Cedar 정책 파일.
# 이 서버들은 모든 툴이 read-only이므로 단일 permit allowlist에 전부 들어가야
# 한다(performance/incident는 진단 read, simulation은 what-if 추정만 — 변경
# 없음). operations는 MIXED(write는 approved=true 필요)라 정책 구조가 달라 이
# 불변식에서 의도적으로 제외한다.
_READONLY_POLICY = {
    "performance": "performance_policy.cedar",
    "incident": "incident_policy.cedar",
    "simulation": "simulation_policy.cedar",
}

# 핸들러 시그니처에 있지만 Gateway에 의도적으로 노출하지 않는 파라미터.
# 추가할 때는 반드시 사유를 적을 것.
_INTENTIONALLY_HIDDEN: dict[str, set[str]] = {}


def _parse_schema() -> dict[str, set[str]]:
    """_tool("name", "desc", {props}, [required]) 블록에서 properties 키 추출.
    중첩 괄호를 균형 추적으로 처리한다."""
    schema: dict[str, set[str]] = {}
    for m in re.finditer(r'_tool\(\s*"([^"]+)"', _SCHEMA_SRC):
        name = m.group(1)
        start = m.start()
        depth = 0
        block = ""
        for i, ch in enumerate(_SCHEMA_SRC[start:], start):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    block = _SCHEMA_SRC[start : i + 1]
                    break
        schema[name] = set(
            re.findall(
                r'"(\w+)":\s*"(?:string|number|integer|boolean|object|array)"',
                block,
            )
        )
    return schema


def _parse_handlers() -> dict[str, list[str]]:
    handlers: dict[str, list[str]] = {}
    for p in _TOOLS_ROOT.rglob("tools/*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name.endswith("_impl"):
                tool = node.name[: -len("_impl")]
                handlers[tool] = [
                    a.arg
                    for a in node.args.args
                    if a.arg not in ("cache", "rds", "self")
                ]
    return handlers


def _parse_handler_tools(server: str) -> set[str]:
    """한 서버의 tools/*.py에서 `*_impl` 툴 이름만 추출."""
    tools: set[str] = set()
    for p in (_TOOLS_ROOT / server / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name.endswith("_impl"):
                tools.add(node.name[: -len("_impl")])
    return tools


# AgentCore Cedar grants come in two forms (see cdk/policies/README.md):
#   • target-wide — `action in AgentCore::Action::"__TARGET__"` permits EVERY
#     tool on the target. This is the LOG_ONLY rollout form all policies use now.
#   • per-tool    — `AgentCore::Action::"<target>___<tool>"` (THREE underscores)
#     permits one tool. This is the ENFORCE refinement form (STEP 2).
_TARGET_WIDE_RE = re.compile(r'action\s+in\s+AgentCore::Action::"__TARGET__"')


def _policy_permits_all_tools(policy_file: str) -> bool:
    """True when the policy grants its whole target — which covers every tool on
    that target, so no per-tool allowlisting is needed (the LOG_ONLY form)."""
    src = (_CEDAR_ROOT / policy_file).read_text()
    return bool(_TARGET_WIDE_RE.search(src))


def _parse_cedar_actions(policy_file: str) -> set[str]:
    """Per-tool tool names a policy permits. Extracts the `<tool>` suffix from
    per-tool `AgentCore::Action::"<target>___<tool>"` grants; the target-wide
    `__TARGET__` form is handled separately by _policy_permits_all_tools."""
    src = (_CEDAR_ROOT / policy_file).read_text()
    tools: set[str] = set()
    for raw in re.findall(r'Action::"([^"]+)"', src):
        if raw == "__TARGET__":
            continue
        tools.add(raw.rsplit("___", 1)[-1])
    return tools


def test_every_readonly_tool_is_permitted_in_cedar_policy():
    """READ-ONLY 서버의 모든 툴이 Cedar permit allowlist에 있어야 한다.

    네 번째 재발 버그 패밀리: 핸들러+스키마에는 추가했으나 Cedar 정책
    allowlist에 빠뜨려, Gateway 기본 DENY 하에서 툴이 조용히 차단되는 경우
    (#4에서 get_maintenance_findings; 동시에 explain_plan/get_vacuum_stats/
    recommend_index/diagnose_root_cause 누락도 적발). 새 read-only 툴을
    추가하면 이 테스트가 Cedar 등록을 강제한다.

    두 정책 형식을 모두 지원: target-wide permit(`action in
    AgentCore::Action::"__TARGET__"`)는 그 타깃의 모든 툴을 덮으므로 통과,
    per-tool ENFORCE 형식으로 전환되면 그때는 툴별 등록을 강제한다.
    """
    problems = []
    for server, policy_file in sorted(_READONLY_POLICY.items()):
        tools = _parse_handler_tools(server)
        assert tools, f"{server}: 핸들러 툴 파싱 실패"
        if _policy_permits_all_tools(policy_file):
            continue  # target-wide permit covers every tool on the target
        permitted = _parse_cedar_actions(policy_file)
        missing = sorted(tools - permitted)
        if missing:
            problems.append(f"{server} ({policy_file}): allowlist 누락 {missing}")
    assert not problems, (
        "READ-ONLY 툴이 Cedar permit allowlist에 없음 — Gateway 기본 DENY에서 "
        "차단됩니다. cdk/policies/cedar/*.cedar에 Action을 추가하세요:\n"
        + "\n".join(problems)
    )


def test_nosql_write_actions_in_operations_cedar_write_block():
    """The 5 NoSQL write actions (multi-engine #P3.6 Group C) must be in the
    operations Cedar policy — otherwise the Gateway default-DENY silently blocks
    them despite the handler + guard being complete (same failure family as the
    create_snapshot/restore_cluster + request_approval Explore findings).

    Under the LOG_ONLY rollout the operations policy is a single target-wide
    permit, which already covers every operations tool (incl. the NoSQL writes)
    — actual write-gating lives in the tool-level approval_guard, and per-tool
    Cedar `approved==true` conditions are the ENFORCE/STEP-2 refinement. Once
    that refinement lands (per-tool grants), this test enforces each required
    action is present."""
    if _policy_permits_all_tools("operations_policy.cedar"):
        return  # target-wide permit covers all operations tools (LOG_ONLY)
    actions = _parse_cedar_actions("operations_policy.cedar")
    required = {
        "modify_dynamodb_capacity",
        "modify_dynamodb_ttl",
        "enable_dynamodb_pitr",
        "set_docdb_profiler",
        "create_docdb_index",
        # Explore-found gaps closed in the same pass.
        "create_snapshot",
        "restore_cluster",
        "request_approval",
        "query_activity_audit",
        "get_runbook",
    }
    missing = sorted(required - actions)
    assert not missing, (
        "operations_policy.cedar에 누락된 Action — Gateway 기본 DENY에서 차단됩니다: "
        + ", ".join(missing)
    )


def test_every_handler_param_is_exposed_in_gateway_schema():
    schema = _parse_schema()
    handlers = _parse_handlers()
    assert len(schema) >= 30, "스키마 파싱이 깨졌다면 여기서 먼저 실패해야 한다"
    assert handlers, "핸들러 파싱 실패"

    problems = []
    for tool, params in sorted(handlers.items()):
        if tool not in schema:
            problems.append(f"{tool}: Gateway 스키마에 툴 자체가 없음")
            continue
        hidden = _INTENTIONALLY_HIDDEN.get(tool, set())
        missing = [p for p in params if p not in schema[tool] and p not in hidden]
        if missing:
            problems.append(f"{tool}: 스키마에 누락된 파라미터 {missing}")
    assert not problems, (
        "핸들러↔스키마 불일치 — cdk/tool_definitions.py에 노출하거나 "
        "_INTENTIONALLY_HIDDEN에 사유와 함께 등록하세요:\n" + "\n".join(problems)
    )
