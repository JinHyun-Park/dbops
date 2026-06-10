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
