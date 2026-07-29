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

import logging
import os
import time
import uuid
from decimal import Decimal

import boto3

from mcp_servers.operations.tools.set_docdb_profiler import validate_profiler_params
from mcp_servers.shared.approval_guard import boolean_flag_error, canonical_action_hash

logger = logging.getLogger(__name__)


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
    plus a deep link the agent can mention in chat.

    NOTE: this tool DELIBERATELY does not accept/write an `origin` marker.
    The approve handler auto-executes only rows carrying origin=="ui", and that
    marker is stamped onto the row by the TRUSTED approvals API Lambda AFTER
    this tool returns (see api/approvals/handler.py `_handle_endpoint_requests`).
    Keeping origin out of the tool means the agent — whose only channel here is
    the gateway — has no way to set it, so a chat-initiated row can never be
    mistaken for a UI-initiated one and auto-executed. `created_at` is returned
    so the API caller can address the row it just created to stamp origin."""
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
        # Aurora custom cluster endpoints (P2-⑤): create/delete/modify are all
        # approval-gated writes; their enum values must be listed or
        # request_approval rejects them and the approval loop dead-ends.
        "create_custom_endpoint",
        "delete_custom_endpoint",
        "modify_custom_endpoint",
        # Reader buffer-cache prewarm (P2-④): approval-gated write; its enum value
        # must be listed or request_approval rejects it and the loop dead-ends.
        "prewarm_reader",
        # Reader scale-out/scale-in (N-③): approval-gated writes that create/delete
        # a billable Aurora reader instance; enum values must be listed here.
        "add_reader_instance",
        "remove_reader_instance",
        # Reader scale-out + auto-warmup (N-④): approval #1 of the semi-automatic
        # flow. Creates a billable reader and auto-queues the prewarm approval.
        "scale_out_with_warmup",
        # enable_data_api는 replay 없는 승인-즉시-실행 액션: DBA가 Approval
        # Center에서 승인하는 순간 approvals API가 rds:EnableHttpEndpoint를
        # 직접 호출한다. 에이전트는 요청 등록까지만 하면 된다.
        "enable_data_api",
        # NoSQL write/remediation (multi-engine #P3.6 Group C). DynamoDB tools
        # ship this stage; the two DocDB Mongo writes ship stage 2 but their
        # enum values are added now so the approval enum stays coherent with
        # the guard projections + Cedar write block.
        "modify_dynamodb_capacity",
        "modify_dynamodb_ttl",
        "enable_dynamodb_pitr",
        "set_docdb_profiler",
        "create_docdb_index",
        # ElastiCache write tools (EC-4): approval enum must list these or
        # request_approval rejects them and the approval loop dead-ends.
        "modify_elasticache_node_type",
        "create_elasticache_snapshot",
        "reboot_elasticache",
        "test_elasticache_failover",
        # Standalone RDS instance write tools (R-3): approval-gated reboot /
        # snapshot / instance-class resize. enum values must be listed here or
        # request_approval rejects them and the approval loop dead-ends.
        "reboot_rds_instance",
        "create_rds_snapshot",
        "modify_rds_instance_class",
        # INSTANCE parameter-group change (E-3). Aurora's cluster-group change is
        # the separate modify_parameter action; the two are gated to disjoint
        # engine families and must stay separate enum values.
        "modify_rds_instance_params",
        "other",
    ):
        return {
            "status": "error",
            "message": f"unknown action_type {action_type!r}",
        }

    # An ambiguous flag must never enter the payload_hash: bare bool("false") is
    # True, so a card reading `enabled: "false"` hashed identically to an
    # executed enabled=True. Refuse here (registration boundary) the same way the
    # write tools refuse a non-bool flag at execute time.
    flag_error = boolean_flag_error(action_type, action_details)
    if flag_error:
        return {"status": "error", "message": flag_error}

    # Range-check what the write tool will range-check, with the SAME helper, on
    # the registration path too: this path mints the payload_hash the write is
    # bound to, so an out-of-range value would otherwise be shown to the DBA,
    # approved, and only then refused at execute time (a burnt approval and a
    # confusing dead-end).
    #
    # Then NORMALIZE: the tool hashes the EFFECTIVE values (its own defaults
    # already applied), so an omitted knob registered as None hashed to a
    # different payload than the one executed and EVERY default-path approval was
    # permanently deniable (approval burnt, retry impossible). Writing the
    # validated effective values back fixes the hash AND shows the DBA the real
    # numbers on the approval card. Same rule as the modify_dynamodb_capacity
    # projection: hash the effective value, not the requested one.
    if action_type == "set_docdb_profiler":
        details = dict(action_details or {})
        threshold_i, rate_f, param_error = validate_profiler_params(
            details.get("threshold_ms"), details.get("sampling_rate")
        )
        if param_error:
            return {"status": "error", "message": param_error}
        details["enabled"] = details.get("enabled", True)  # tool/schema default
        details["threshold_ms"] = threshold_i
        details["sampling_rate"] = rate_f
        action_details = details

    # The two argument preconditions BOTH parameter tools answer BEFORE
    # verify_approval, answered on the path that MINTS the payload_hash too. Each
    # tool refuses an empty parameter_name and an empty value pre-consume, so a
    # card carrying either could only ever answer invalid_request: the DBA reviews
    # and approves a write that cannot run, and the only way out is a new request.
    #
    # `parameter` is accepted as the alias `_project` already tolerates, because
    # it is the key the tools' own approval_required response uses. Only
    # surrounding whitespace is stripped ("0" and "off" are legitimate parameter
    # values, and both tools accept them), and the stripped values are written
    # back so the card does not show padding the executor drops.
    #
    # It is only the WHITESPACE that is reconciled here, not the case. Registration
    # cannot know the parameter's canonical spelling: that comes from the group's
    # own describe, which only the executing tool calls. So a card registered as
    # "MAX_CONNECTIONS" still displays that, while the write sends the API's
    # "max_connections". The card remains executable because both projections
    # case-fold `parameter_name` before hashing (approval_guard._project), which is
    # the equality the round trip rests on, not the displayed string.
    if action_type in ("modify_parameter", "modify_rds_instance_params"):
        details = dict(action_details or {})
        raw_value = details.get("value")
        name = str(details.get("parameter_name")
                   or details.get("parameter") or "").strip()
        value = str(raw_value if raw_value is not None else "").strip()
        if not name:
            return {
                "status": "error",
                "message": "parameter_name이 필요합니다. 파라미터 이름이 비어 있는 승인은 "
                           "실행 시점에 거부되므로 등록하지 않았습니다.",
            }
        if not value:
            return {
                "status": "error",
                "message": "value가 필요합니다. 파라미터를 엔진 기본값으로 되돌리는 것은 "
                           "이 툴들이 지원하지 않는 별개의 작업이고, 값이 비어 있는 승인은 "
                           "실행 시점에 거부되므로 등록하지 않았습니다.",
            }
        for key in ("parameter_name", "parameter"):
            if key in details:
                details[key] = name
        details["value"] = value
        action_details = details

    approval_id = str(uuid.uuid4())
    created_at = str(int(time.time() * 1000))  # ms epoch as string for sort key

    item = {
        "approval_id": approval_id,
        "created_at": created_at,
        # DynamoDB TTL (the table's ttl attribute): a pending request
        # auto-expires 24h after creation so stale, never-acted-on
        # requests don't linger in the Approval Center indefinitely.
        # Well above the 60-min replay window, so a legitimately
        # approved request always stays consumable.
        "ttl": int(time.time()) + 24 * 60 * 60,
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

    try:
        ddb = boto3.resource("dynamodb").Table(table_name)
        ddb.put_item(Item=item)
    except Exception:
        # Static reason only: a DDB error message can carry the table ARN, the
        # account id or the full item, and this string lands in the chat.
        logger.exception("[request_approval] put_item failed for %s", cluster_id)
        return {
            "status": "error",
            "message": (
                "승인 요청 등록에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요)."
            ),
        }

    frontend = os.environ.get("FRONTEND_URL", "").rstrip("/")
    deep_link = f"{frontend}/approvals" if frontend else "/approvals"

    return {
        "status": "pending",
        "approval_id": approval_id,
        "created_at": created_at,
        "cluster_id": cluster_id,
        "action_type": action_type,
        "action_details": action_details,
        "review_url": deep_link,
        "message": (
            f"DBA 승인이 등록되었습니다 (approval_id={approval_id}). "
            "검토 후 승인이 떨어지면 같은 호출을 approved=true 와 "
            f"approval_id={approval_id!r} 를 모두 넣어서 다시 실행해주세요. "
            "approval_id 가 없거나 승인 후 1시간이 지나면 서버가 거부합니다."
        ),
    }
