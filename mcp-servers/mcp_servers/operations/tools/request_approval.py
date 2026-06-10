"""request_approval — agent-facing primitive for creating DBA approval rows.

When a write tool (`execute_sql` DDL/DML, `modify_parameter`, `modify_scaling`,
`manage_maintenance`) returns `{"status": "approval_required", ...}`, the
agent should call this tool with the same payload to register the request
in the Approval Center. The DBA then reviews it on `/approvals`, and on
approval the agent re-issues the write with `approved=true`.

The handoff is intentionally explicit (two tool calls) so:
  - The agent's transcript shows exactly what it proposed.
  - The DBA sees the same JSON the agent saw.
  - Replay (re-issuing after approval) is auditable.
"""

import os
import time
import uuid
from decimal import Decimal

import boto3

from mcp_servers.shared.approval_guard import canonical_action_hash


def _ddb_safe(value):
    """boto3의 DynamoDB resource는 Python float을 거부한다("Float types are
    not supported") — ACU 범위(0.5, 4.0) 같은 숫자가 action_details에 오면
    put_item이 통째로 실패해 에이전트가 우회 재시도를 해야 했다. float은
    Decimal로, 중첩 구조는 재귀 변환한다. 해시는 변환 전 값으로 이미 계산되며
    _norm_val이 숫자/문자열을 동일 취급하므로 검증과도 일관된다."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(v) for v in value]
    return value


def request_approval_impl(
    cache,
    cluster_id: str,
    action_type: str,
    action_details: dict,
    requested_by: str = "agent",
) -> dict:
    """Create an approval row in the DDB approvals table and return its id
    plus a deep link the agent can mention in chat."""
    table_name = os.environ.get("APPROVALS_TABLE", "")
    if not table_name:
        return {
            "status": "error",
            "message": "APPROVALS_TABLE env var not configured on this deployment",
        }

    if action_type not in (
        "execute_sql",
        "modify_parameter",
        "modify_scaling",
        "manage_maintenance",
        "create_snapshot",
        "restore_cluster",
        # enable_data_api는 replay 없는 승인-즉시-실행 액션: DBA가 Approval
        # Center에서 승인하는 순간 approvals API가 rds:EnableHttpEndpoint를
        # 직접 호출한다. 에이전트는 요청 등록까지만 하면 된다.
        "enable_data_api",
        "other",
    ):
        return {
            "status": "error",
            "message": f"unknown action_type {action_type!r}",
        }

    approval_id = str(uuid.uuid4())
    created_at = str(int(time.time() * 1000))  # ms epoch as string for sort key

    try:
        ddb = boto3.resource("dynamodb").Table(table_name)
        ddb.put_item(
            Item={
                "approval_id": approval_id,
                "created_at": created_at,
                "approval_status": "pending",
                "cluster_id": cluster_id,
                "action_type": action_type,
                "action_details": _ddb_safe(action_details),
                # Bind the approval to this exact payload. verify_approval
                # re-derives the same hash from the tool's real args at execute
                # time and refuses any mismatch — so an approval for one SQL
                # cannot be consumed for a different one on the same cluster.
                "payload_hash": canonical_action_hash(action_type, action_details),
                "requested_by": requested_by,
            }
        )
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}

    frontend = os.environ.get("FRONTEND_URL", "").rstrip("/")
    deep_link = f"{frontend}/approvals" if frontend else "/approvals"

    return {
        "status": "pending",
        "approval_id": approval_id,
        "cluster_id": cluster_id,
        "action_type": action_type,
        "action_details": action_details,
        "review_url": deep_link,
        "message": (
            f"DBA 승인이 등록되었습니다 (approval_id={approval_id}). "
            "검토 후 승인이 떨어지면 같은 호출을 approved=true 와 "
            f"approval_id={approval_id!r} 를 모두 넣어서 다시 실행해주세요. "
            "approval_id 가 없거나 30분이 지나면 서버가 거부합니다."
        ),
    }
