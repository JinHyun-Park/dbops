import base64
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
import tenancy
from botocore.config import Config
from botocore.exceptions import ClientError

# UI-initiated custom-endpoint writes (N-①). These action_types are executed
# INLINE by this handler on approve — but ONLY when the row carries origin="ui".
# Chat-initiated rows of the same action_type have NO origin and are replayed by
# the agent, so auto-executing them here would double-execute the write.
_ENDPOINT_ACTIONS = (
    "create_custom_endpoint",
    "modify_custom_endpoint",
    "delete_custom_endpoint",
)

# Success statuses the endpoint tools return once the RDS mutation is accepted.
_ENDPOINT_OK_STATUS = ("creating", "modifying", "deleting")

# Endpoint create/modify/delete return in seconds and API Gateway caps at 29s,
# so the default read_timeout is plenty. Set timeouts explicitly and DISABLE
# botocore retries (max_attempts=0) so a slow/failed sync invoke can never
# double-invoke — the tool's verify_approval consumes the approval single-use,
# and a retry after a partial run would fail or double-execute.
_OPS_LAMBDA_CFG = Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 0})


def _client_context(tool_name: str) -> str:
    """Base64 ClientContext so the operations Lambda's _extract_tool_name reads
    custom.tool_name — Lambda only delivers ClientContext on a SYNCHRONOUS
    (RequestResponse) invoke, so callers must invoke sync. Mirrors the
    restore_finalizer's construction."""
    return base64.b64encode(
        json.dumps({"custom": {"tool_name": tool_name}}).encode("utf-8")
    ).decode("utf-8")


def _invoke_operations(tool_name: str, payload: dict) -> dict:
    """Synchronously invoke the operations Lambda's <tool_name> and return the
    parsed tool-result dict. The operations Lambda owns the endpoint tools +
    approval_guard + cross-account access, so api/ (which cannot import
    mcp_servers) delegates rather than duplicating that logic.

    Returns the tool's own result dict, or an {"status": "invoke_error", ...}
    shape on any transport/parse failure — never raises, never leaks str(e)
    beyond a short reason so the approve/POST paths don't crash."""
    ops_fn = os.environ.get("OPERATIONS_FUNCTION_NAME", "")
    if not ops_fn:
        return {"status": "invoke_error", "reason": "OPERATIONS_FUNCTION_NAME not configured"}
    try:
        resp = boto3.client("lambda", config=_OPS_LAMBDA_CFG).invoke(
            FunctionName=ops_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload, default=str).encode("utf-8"),
            ClientContext=_client_context(tool_name),
        )
    except Exception as e:
        # Log the transport/fault detail server-side only; the client gets a
        # static reason (str(e) can carry the function ARN, role, endpoint URL).
        print(f"[approvals] operations invoke ({tool_name}) failed: {e}")
        return {"status": "invoke_error", "reason": "operations 호출에 실패했습니다"}
    if resp.get("FunctionError"):
        return {"status": "invoke_error", "reason": "operations Lambda가 오류를 반환했습니다"}
    try:
        body = json.loads(resp["Payload"].read().decode("utf-8"))
        text = (body.get("content") or [{}])[0].get("text") if isinstance(body, dict) else None
        result = json.loads(text) if text else body
    except Exception:
        return {"status": "invoke_error", "reason": "operations 응답을 해석할 수 없습니다"}
    return result if isinstance(result, dict) else {"status": "invoke_error", "reason": "unexpected result shape"}


def _cluster_item(cluster_id: str) -> dict:
    """Fetch {cluster_id, team_id} from the clusters registry for a single
    cluster. Returns {} on miss or infra error (caller's cluster_visible treats
    missing team_id as default-open)."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not cluster_id or not table_name:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[approvals] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode a JWT payload — no signature check needed here
    because API Gateway's Cognito JWT authorizer already verified the
    token before the Lambda was invoked."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    """True if the caller's token does not place them in dbops-viewer.
    Matches the frontend isAdmin() semantics: no group claim = default admin.
    No token at all = NOT admin (fail-closed)."""
    hdrs = event.get("headers") or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and "dbops-admin" not in groups:
        return False
    return True


def _caller_name(event: dict) -> str:
    """Return the caller's display name from the token claims, for audit
    stamping. Falls back to 'unknown' when the claim is absent."""
    hdrs = event.get("headers") or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "unknown"
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
        or "unknown"
    )


def _created_ms(item: dict) -> float:
    """정렬용 created_at 정규화. 두 생성 경로가 다른 포맷을 쓴다 —
    request_approval(MCP)은 ms-epoch 문자열("1781069757421"), approvals
    POST(UI)는 ISO("2026-06-10T06:42:08"). 문자열 정렬은 "2026..." >
    "1781..."이라 UI발 행이 항상 에이전트발 행보다 최신으로 보이는
    시간 무관 정렬이 된다. epoch ms로 통일해 비교한다."""
    raw = str(item.get("created_at", "") or "")
    if raw.isdigit():
        return float(raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # 이 핸들러가 쓰는 naive ISO는 utcnow() 산물 — UTC로 명시 고정해야
        # 실행 환경 타임존(Lambda=UTC, 로컬 테스트=KST)과 무관하게 같은 값.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except ValueError:
        return 0.0


def _scan_all(table, **kwargs) -> list:
    """LastEvaluatedKey를 끝까지 따라가는 scan. 단일 호출 scan은 1MB 페이지에서
    조용히 잘린다 — 승인 이력이 쌓이면 활동 피드·목록·approval_id 조회가
    임의로 누락되는, approval_guard의 Limit=1 버그와 같은 잘림 패밀리."""
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _compact_activity(items: list) -> list:
    """Project approval rows to the compact activity-feed shape (strip noisy
    fields; action_details kept as a 500-char head excerpt)."""
    compact = []
    for it in items:
        details = it.get("action_details") or it.get("parameters") or {}
        details_str = details if isinstance(details, str) else json.dumps(details, default=str)
        compact.append({
            "approval_id": it.get("approval_id"),
            "created_at": it.get("created_at"),
            "resolved_at": it.get("resolved_at"),
            "consumed_at": it.get("consumed_at"),
            "approval_status": it.get("approval_status"),
            "cluster_id": it.get("cluster_id"),
            "action_type": it.get("action_type") or it.get("tool_name"),
            "requested_by": it.get("requested_by"),
            "approved_by": it.get("approved_by"),
            "action_details_excerpt": details_str[:500],
        })
    return compact


def _scaleout_state(item: dict) -> str:
    """Derive the DBA-facing scale-out lifecycle state from a prewarm approval
    row. `consumed`/`cancelled`/`awaiting_instance_failed` are terminal and win
    over `warm_dispatched` — a consumed row also carries warm_dispatched=True
    (the finalizer sets warm_dispatched while status is still `approved`, then
    prewarm_reader flips it to consumed), so warming must be checked AFTER
    warmed or a completed op would read as still-warming.

    A recorded FAILED warm (`warm_result == "failed"`) outranks consumed AND
    warm_dispatched — the finalizer sets warm_dispatched=True for any response,
    including a prewarm that ran but failed, so without this check the row would
    show "warmed"/"warming" forever. A failed warm is terminal: the DBA re-warms
    manually via chat, the finalizer never retries."""
    status = item.get("approval_status", "")
    if status == "cancelled":
        return "cancelled"
    if item.get("warm_result") == "failed":
        return "warm_failed"
    if status == "consumed":
        return "warmed"
    if status == "awaiting_instance_failed":
        return "provision_failed"
    if item.get("warm_dispatched"):
        return "warming"
    return {
        "awaiting_instance": "reader_provisioning",
        "pending": "warm_pending_approval",
        "approved": "warm_approved",
    }.get(status, status or "unknown")


def _handle_scaleout(event, table, method, path, path_params, headers) -> dict:
    """N-④ Phase 2 — scale-out ops management. Scale-out ops ARE approval rows
    (scaleout=true prewarm approvals); this resource surfaces them with a
    derived lifecycle state + a cancel that only stops the auto-warm."""
    # GET /api/scaleout-ops — every scale-out op visible to the caller,
    # newest-first. Tenant-scoped exactly like the /api/approvals list.
    if method == "GET":
        rows = _scan_all(
            table,
            FilterExpression="scaleout = :t",
            ExpressionAttributeValues={":t": True},
        )
        visible = tenancy.visible_set_from_registry(event)
        if visible is not None:
            rows = [r for r in rows if r.get("cluster_id") in visible]
        rows = sorted(rows, key=_created_ms, reverse=True)
        ops = []
        for r in rows:
            ad = r.get("action_details")
            if not isinstance(ad, dict):
                ad = {}
            ops.append({
                "approval_id": r.get("approval_id"),
                "cluster_id": r.get("cluster_id"),
                "reader_instance_id": r.get("reader_instance_id") or ad.get("reader_instance_id"),
                "endpoint_identifier": ad.get("endpoint_identifier", ""),
                "top_n": ad.get("top_n"),
                "state": _scaleout_state(r),
                "created_at": r.get("created_at"),
                "warm_dispatched": bool(r.get("warm_dispatched")),
            })
        return {"statusCode": 200, "headers": headers,
                "body": json.dumps({"ops": ops, "count": len(ops)}, default=str)}

    # POST /api/scaleout-ops/{id}/cancel — cancel an op that hasn't warmed.
    if method == "POST":
        # Same admin gate the approve/reject path uses — fail-closed (no bearer
        # => not admin => 403).
        if not _is_admin(event):
            return {"statusCode": 403, "headers": headers,
                    "body": json.dumps({"error": "forbidden",
                                        "reason": "admin role required to cancel a scale-out op"})}
        approval_id = path_params.get("id")
        if not approval_id:
            return {"statusCode": 400, "headers": headers,
                    "body": json.dumps({"error": "approval_id required"})}
        items = _scan_all(
            table,
            FilterExpression="approval_id = :aid",
            ExpressionAttributeValues={":aid": approval_id},
        )
        item = items[0] if items else None
        if not item or not item.get("scaleout"):
            return {"statusCode": 404, "headers": headers,
                    "body": json.dumps({"error": "not found"})}
        if not tenancy.cluster_visible(event, _cluster_item(item.get("cluster_id", ""))):
            return {"statusCode": 403, "headers": headers,
                    "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}
        # Only awaiting_instance / pending are cancellable — never an op that is
        # already approved/warming/consumed. The ConditionExpression makes the
        # check atomic (a concurrent finalizer transition loses the race → 409).
        try:
            table.update_item(
                Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
                UpdateExpression="SET approval_status = :c, resolved_at = :t, resolved_by = :by",
                ConditionExpression="scaleout = :true AND approval_status IN (:aw, :pd)",
                ExpressionAttributeValues={
                    ":c": "cancelled",
                    ":t": datetime.utcnow().isoformat() + "Z",
                    ":by": _caller_name(event),
                    ":true": True,
                    ":aw": "awaiting_instance",
                    ":pd": "pending",
                },
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return {"statusCode": 409, "headers": headers,
                        "body": json.dumps({
                            "error": "cannot_cancel",
                            "detail": f"현재 상태({_scaleout_state(item)})에서는 취소할 수 없습니다 — "
                                      "이미 승인·예열·완료된 작업입니다.",
                        })}
            raise
        return {"statusCode": 200, "headers": headers,
                "body": json.dumps({
                    "approval_id": approval_id,
                    "state": "cancelled",
                    "note": "자동 예열만 취소되었습니다 — 생성된 리더 인스턴스는 유지됩니다. "
                            "필요하면 스케일 인으로 별도 제거하세요.",
                })}

    return {"statusCode": 405, "headers": headers,
            "body": json.dumps({"error": "Method not allowed"})}


def resolve_eligible_approvers(cluster_id, action_type, policies) -> set:
    """Return the designated-approver set for a request (lower-cased), or an
    EMPTY set when no policy matches (= policy not applicable → fallback).

    A policy matches when its cluster_id is the request's cluster_id or "*",
    AND its action_type is the request's action_type or "*". The most specific
    matching policy wins (cluster-exact=2 + action-exact=1); ties at the top
    score union their approvers."""
    best, eligible = -1, set()
    for p in policies or []:
        pc = p.get("cluster_id", "*")
        pa = p.get("action_type", "*")
        if pc not in (cluster_id, "*") or pa not in (action_type, "*"):
            continue
        score = (2 if pc == cluster_id else 0) + (1 if pa == action_type else 0)
        approvers = {str(a).strip().lower() for a in (p.get("approvers") or []) if str(a).strip()}
        if score > best:
            best, eligible = score, set(approvers)
        elif score == best:
            eligible |= approvers
    return eligible


def _load_eligible_approvers(cluster_id, action_type) -> set:
    """Resolve the eligible approver set from the policies table. FAIL-SAFE:
    any error (no env, no grant, DDB down) → empty set → fallback to any-admin.
    A policy-infra failure must never freeze the approval loop."""
    try:
        name = os.environ.get("APPROVAL_POLICIES_TABLE")
        if not name:
            return set()
        table = boto3.resource("dynamodb").Table(name)
        return resolve_eligible_approvers(cluster_id, action_type, _scan_all(table))
    except Exception as e:  # noqa: BLE001 - fail-safe by design
        print(f"[approvals] policy load failed: {type(e).__name__}: {e}")
        return set()


def _execute_enable_data_api(item: dict) -> dict:
    """enable_data_api 승인은 승인 즉시 이 핸들러가 직접 실행한다 — 에이전트
    재호출(replay) 단계가 없어, 실행이 DBA의 인증된 승인 클릭 아래에서 일어난다.

    권한은 rds:EnableHttpEndpoint 단일 액션으로 스코프한다. ModifyDBCluster를
    쓰면 마스터 패스워드 변경·삭제 보호 해제까지 가능한 광범위 권한을 플랫폼에
    줘야 하므로, 설정 1비트짜리 전용 API를 쓰는 것이 이 기능의 보안 전제다.
    (참고: modify-db-cluster --enable-http-endpoint는 legacy Serverless v1
    전용으로 Sv2·프로비저닝에선 조용히 무시된다 — 실측 확인.)"""
    cluster_id = item.get("cluster_id", "")
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return {"ok": False, "error": "CLUSTERS_TABLE not configured"}
    if not cluster_id:
        return {"ok": False, "error": "approval row has no cluster_id"}
    # 레지스트리의 cluster_arn이 권위 — 크로스리전이어도 ARN에 리전이 담겨 있다.
    try:
        row = (
            boto3.resource("dynamodb")
            .Table(table_name)
            .get_item(Key={"cluster_id": cluster_id})
            .get("Item")
            or {}
        )
    except Exception as e:
        return {"ok": False, "error": f"registry lookup failed: {str(e)[:200]}"}
    arn = row.get("cluster_arn", "")
    if not arn:
        return {"ok": False, "error": f"cluster_arn not found in registry for {cluster_id!r}"}

    rds = boto3.client("rds")
    if not hasattr(rds, "enable_http_endpoint"):
        # Lambda 런타임 boto3가 너무 오래된 경우의 명시적 실패 — raw
        # AttributeError보다 조치 가능한 메시지를 남긴다.
        return {"ok": False, "error": "런타임 boto3가 EnableHttpEndpoint API를 지원하지 않습니다 — 런타임 업그레이드 필요"}
    try:
        resp = rds.enable_http_endpoint(ResourceArn=arn)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    return {
        "ok": True,
        "cluster_arn": arn,
        "http_endpoint_enabled": bool(resp.get("HttpEndpointEnabled")),
        "note": "전파까지 보통 1~2분, VPC(NAT) 경유 호출자는 더 걸릴 수 있습니다. 다음 수집 사이클에 대시보드 경고가 사라집니다.",
    }


def _handle_endpoint_requests(event, method, headers) -> dict:
    """POST /api/endpoint-requests — console-initiated custom-endpoint write.

    Admin-gated + tenant-scoped. Validates the action + its required fields,
    then invokes the operations Lambda's request_approval tool (origin="ui") to
    mint a correctly payload-hashed PENDING approval. api/ CANNOT import
    mcp_servers, so the hash MUST be computed by the operations Lambda's
    approval_guard — hence the invoke rather than writing the row directly.

    The write itself does NOT run here; it runs when the DBA approves and the
    approve path auto-executes the origin="ui" row (see below)."""
    if method != "POST":
        return {"statusCode": 405, "headers": headers,
                "body": json.dumps({"error": "Method not allowed"})}
    if not _is_admin(event):
        return {"statusCode": 403, "headers": headers,
                "body": json.dumps({"error": "forbidden",
                                    "reason": "admin role required to request an endpoint change"})}
    body = json.loads(event.get("body", "{}"))
    cluster_id = str(body.get("cluster_id") or "").strip()
    action = str(body.get("action") or "").strip()
    if action not in _ENDPOINT_ACTIONS:
        return {"statusCode": 400, "headers": headers,
                "body": json.dumps({"error": "invalid_action",
                                    "detail": f"action은 {_ENDPOINT_ACTIONS} 중 하나여야 합니다"})}
    if not cluster_id:
        return {"statusCode": 400, "headers": headers,
                "body": json.dumps({"error": "cluster_id_required"})}
    # Tenant scope — the caller must be able to see this cluster (same visibility
    # check the write routes use). Non-visible → 403, never a silent pass.
    if not tenancy.cluster_visible(event, _cluster_item(cluster_id)):
        return {"statusCode": 403, "headers": headers,
                "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."})}

    endpoint_identifier = str(body.get("endpoint_identifier") or "").strip()
    if not endpoint_identifier:
        return {"statusCode": 400, "headers": headers,
                "body": json.dumps({"error": "endpoint_identifier_required"})}
    static_members = [str(m).strip() for m in (body.get("static_members") or []) if str(m).strip()]
    excluded_members = [str(m).strip() for m in (body.get("excluded_members") or []) if str(m).strip()]
    if static_members and excluded_members:
        return {"statusCode": 400, "headers": headers,
                "body": json.dumps({"error": "invalid_members",
                                    "detail": "static_members와 excluded_members는 상호 배타적입니다 — 하나만 지정하세요"})}

    if action == "create_custom_endpoint":
        etype = str(body.get("endpoint_type") or "").strip().upper()
        if etype not in ("READER", "ANY"):
            return {"statusCode": 400, "headers": headers,
                    "body": json.dumps({"error": "invalid_endpoint_type",
                                        "detail": "endpoint_type은 READER 또는 ANY 여야 합니다"})}
        action_details = {"endpoint_identifier": endpoint_identifier, "endpoint_type": etype,
                          "static_members": static_members, "excluded_members": excluded_members}
    elif action == "modify_custom_endpoint":
        if not static_members and not excluded_members:
            return {"statusCode": 400, "headers": headers,
                    "body": json.dumps({"error": "members_required",
                                        "detail": "static_members 또는 excluded_members 중 하나는 지정해야 합니다"})}
        action_details = {"endpoint_identifier": endpoint_identifier,
                          "static_members": static_members, "excluded_members": excluded_members}
    else:  # delete_custom_endpoint
        action_details = {"endpoint_identifier": endpoint_identifier}

    result = _invoke_operations("request_approval", {
        "cluster_id": cluster_id,
        "action_type": action,
        "action_details": action_details,
        "requested_by": _caller_name(event),
    })
    if result.get("status") != "pending" or not result.get("approval_id"):
        return {"statusCode": 502, "headers": headers,
                "body": json.dumps({"error": "request_failed",
                                    "detail": result.get("reason") or result.get("message")
                                    or "승인 요청 생성에 실패했습니다"})}
    # Stamp origin="ui" onto the row HERE — from the trusted API Lambda, not the
    # request_approval tool. The agent's only channel to request_approval is the
    # gateway (which carries only declared-schema params, and origin is NOT in
    # that schema), so it cannot mint an origin="ui" row; only this admin+tenant-
    # gated API path can. That makes origin=="ui" a real trust boundary the
    # approve-path auto-execute can rely on. Uses the (approval_id, created_at)
    # key the tool just returned. Origin is metadata — not in payload_hash — so
    # stamping it never invalidates the binding.
    created_at = result.get("created_at")
    if created_at:
        try:
            boto3.resource("dynamodb").Table(os.environ["APPROVALS_TABLE"]).update_item(
                Key={"approval_id": result["approval_id"], "created_at": created_at},
                UpdateExpression="SET #o = :ui",
                ExpressionAttributeNames={"#o": "origin"},
                ExpressionAttributeValues={":ui": "ui"},
            )
        except Exception as e:
            # Fail-SAFE: without the origin stamp the row simply won't
            # auto-execute (it stays a normal pending approval) — never
            # fail-open. Surface a soft warning; the request row still exists.
            print(f"[approvals] origin stamp failed for {result['approval_id']}: {e}")
            return {"statusCode": 201, "headers": headers,
                    "body": json.dumps({
                        "approval_id": result["approval_id"],
                        "cluster_id": cluster_id,
                        "action": action,
                        "origin_stamped": False,
                        "message": "승인 요청은 생성됐지만 자동 실행 표식 기록에 실패했습니다 — 승인해도 자동 실행되지 않을 수 있습니다. 관리자에게 문의하세요.",
                    })}
    return {"statusCode": 201, "headers": headers,
            "body": json.dumps({
                "approval_id": result["approval_id"],
                "cluster_id": cluster_id,
                "action": action,
                "message": "승인 요청이 생성되었습니다 — 승인 센터에서 검토·승인하면 실행됩니다.",
            })}


def _execute_endpoint_action(item: dict) -> dict:
    """Auto-execute an approved, UI-originated endpoint write via the operations
    Lambda. The tool's verify_approval sees status=approved, consumes the row
    single-use, and runs the RDS mutation — so execution happens under the DBA's
    authenticated approve click, exactly like enable_data_api.

    ONLY origin=="ui" rows reach here (the caller gates on it): chat rows are
    replayed by the agent, so auto-executing them would double-execute.

    On a non-success status the row is left consumed-or-approved by the tool
    (verify_approval only consumes on success), and the failure is surfaced so
    the DBA sees it — the approve path never crashes."""
    action_type = item.get("action_type") or item.get("tool_name")
    ad = item.get("action_details")
    if not isinstance(ad, dict):
        ad = {}
    payload = {
        "cluster_id": item.get("cluster_id", ""),
        "approved": True,
        "approval_id": item.get("approval_id"),
        **ad,
    }
    result = _invoke_operations(action_type, payload)
    status = result.get("status", "unknown")
    return {
        "executed": status in _ENDPOINT_OK_STATUS,
        "status": status,
        "reason": result.get("reason") or result.get("error"),
    }


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["APPROVALS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath") or event.get("path") or ""
    path_params = event.get("pathParameters") or {}
    approval_id = path_params.get("id")
    qsp = event.get("queryStringParameters") or {}

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    # /api/scaleout-ops (+ /{id}/cancel) — scale-out op management. Must come
    # BEFORE the generic GET-list arm: the list route also has no approval_id,
    # so it would otherwise swallow GET /api/scaleout-ops.
    if "/scaleout-ops" in path:
        return _handle_scaleout(event, table, method, path, path_params, headers)

    # /api/endpoint-requests (N-①) — console-initiated custom-endpoint write.
    # Must come BEFORE the generic POST arm below (which only allows
    # enable_data_api), or that arm would reject the endpoint action_type.
    if "/endpoint-requests" in path:
        return _handle_endpoint_requests(event, method, headers)

    # /api/activity — chronological feed of every approval (any status)
    # for compliance + retro queries ("what writes happened in cluster X
    # last week?"). The DDB scan is cheap because approvals are short-
    # lived: rows expire on TTL or get consumed by the next write.
    if method == "GET" and path.endswith("/activity"):
        cluster_filter = qsp.get("cluster_id")
        actor_filter = qsp.get("actor")
        action_filter = qsp.get("action_type")
        limit = max(1, min(int(qsp.get("limit", "200")), 500))

        filters = []
        attr_values: dict = {}
        if cluster_filter:
            filters.append("cluster_id = :cid")
            attr_values[":cid"] = cluster_filter
        if actor_filter:
            # Match either requested_by or approved_by — DBA might be
            # asking "what did this person do?" not just "what did they
            # request?"
            filters.append("(requested_by = :a OR approved_by = :a)")
            attr_values[":a"] = actor_filter
        if action_filter:
            filters.append("(action_type = :at OR tool_name = :at)")
            attr_values[":at"] = action_filter

        scan_kwargs: dict = {}
        if filters:
            scan_kwargs["FilterExpression"] = " AND ".join(filters)
            scan_kwargs["ExpressionAttributeValues"] = attr_values

        cursor_param = qsp.get("cursor")
        export_mode = qsp.get("export") == "true" or bool(cursor_param)

        if export_mode:
            page = max(1, min(int(qsp.get("limit", "500")), 1000))
            if cursor_param:
                try:
                    decoded = json.loads(base64.urlsafe_b64decode(cursor_param))
                    if not isinstance(decoded, dict):
                        raise ValueError("cursor is not a key object")
                    scan_kwargs["ExclusiveStartKey"] = decoded
                except Exception:
                    return {"statusCode": 400, "headers": headers,
                            "body": json.dumps({"error": "invalid cursor"})}
            scan_kwargs["Limit"] = page
            resp = table.scan(**scan_kwargs)
            # Apply the same tenant filter as the normal arm — otherwise a
            # viewer could bypass cluster visibility via ?export=true and read
            # other teams' approval activity (cluster_id/action_type/requester).
            # Admin → visible is None → unfiltered. Post-filter page size varies;
            # acceptable for an export/audit feed (the cursor still advances by
            # the underlying scan page).
            export_items = resp.get("Items", [])
            visible = tenancy.visible_set_from_registry(event)
            if visible is not None:
                export_items = [it for it in export_items if it.get("cluster_id") in visible]
            compact = _compact_activity(export_items)
            lek = resp.get("LastEvaluatedKey")
            # NOTE: approvals keys are strings (approval_id, created_at), so the
            # json round-trip is lossless. default=str would coerce a Decimal/
            # Binary key to a string — revisit this codec if the key schema
            # ever gains a numeric/binary key.
            next_cursor = (
                base64.urlsafe_b64encode(json.dumps(lek, default=str).encode()).decode()
                if lek else None
            )
            return {"statusCode": 200, "headers": headers,
                    "body": json.dumps({"items": compact, "count": len(compact),
                                        "next_cursor": next_cursor}, default=str)}

        all_items = _scan_all(table, **scan_kwargs)
        visible = tenancy.visible_set_from_registry(event)
        if visible is not None:
            all_items = [it for it in all_items if it.get("cluster_id") in visible]
        items = sorted(all_items, key=_created_ms, reverse=True)[:limit]
        compact = _compact_activity(items)
        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({"items": compact, "count": len(compact)}, default=str),
        }

    if method == "GET" and not approval_id:
        status_filter = qsp.get("status", "pending")
        # "승인됨" 탭은 consumed(승인 후 실행 완료)도 포함한다 — DBA의 멘탈
        # 모델에서 둘 다 "내가 승인한 작업"이고, consumed가 어느 탭에도 안
        # 보이면 실행된 승인이 UI에서 증발한 것처럼 보인다.
        if status_filter == "approved":
            rows = _scan_all(
                table,
                FilterExpression="approval_status IN (:s1, :s2)",
                ExpressionAttributeValues={":s1": "approved", ":s2": "consumed"},
            )
        else:
            rows = _scan_all(
                table,
                FilterExpression="approval_status = :s",
                ExpressionAttributeValues={":s": status_filter},
            )
        visible = tenancy.visible_set_from_registry(event)
        if visible is not None:
            rows = [r for r in rows if r.get("cluster_id") in visible]
        items = sorted(rows, key=_created_ms, reverse=True)
        return {"statusCode": 200, "headers": headers, "body": json.dumps(items, default=str)}

    if method == "GET" and approval_id:
        response = table.get_item(Key={"approval_id": approval_id, "created_at": qsp.get("created_at", "")})
        item = response.get("Item")
        if not item:
            items = _scan_all(
                table,
                FilterExpression="approval_id = :aid",
                ExpressionAttributeValues={":aid": approval_id},
            )
            item = items[0] if items else None
        if item:
            if not tenancy.cluster_visible(event, _cluster_item(item.get("cluster_id", ""))):
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({"error": "이 클러스터에 대한 접근 권한이 없습니다."}),
                }
        return {
            "statusCode": 200 if item else 404,
            "headers": headers,
            "body": json.dumps(item or {"error": "not found"}, default=str),
        }

    if method == "POST":
        # P2.4.2 server-side RBAC: only admins may create approval requests
        # via the UI path. Agent-originated rows come through the MCP
        # request_approval tool (different code path) which also requires
        # an authenticated runtime session, so this gate doesn't affect it.
        if not _is_admin(event):
            return {
                "statusCode": 403,
                "headers": headers,
                "body": json.dumps({
                    "error": "forbidden",
                    "reason": "admin role required to create approval requests",
                }),
            }
        body = json.loads(event.get("body", "{}"))
        now = datetime.utcnow().isoformat()
        action_type = body.get("action_type", "")

        # 이 POST 경로(UI발)로는 enable_data_api 승인만 만들 수 있다. 다른
        # 쓰기 액션(execute_sql/modify_*/restore 등)은 반드시 MCP의
        # request_approval을 거쳐야 한다 — 거기서만 payload_hash가 계산되어
        # 승인이 "특정 페이로드"에 바인딩된다. POST가 임의 action_type을
        # 받아들이면 payload_hash 없는(=guard가 페이로드 검증을 건너뛰는)
        # 쓰기 승인을 만들 수 있어, 한 승인을 다른 SQL/파라미터로 재사용하는
        # 구멍이 된다(Codex 감사 적발). enable_data_api는 페이로드가
        # cluster_id뿐이고 승인 즉시 서버가 실행하므로 이 경로가 안전하다.
        if action_type and action_type != "enable_data_api":
            return {
                "statusCode": 400,
                "headers": headers,
                "body": json.dumps({
                    "error": "unsupported_action_type",
                    "detail": (
                        f"{action_type!r}는 이 경로로 승인 요청을 만들 수 없습니다 — "
                        "쓰기 작업은 에이전트의 request_approval 도구를 통해 "
                        "페이로드 바인딩과 함께 등록해야 합니다."
                    ),
                }),
            }

        # UI발 enable_data_api 요청은 멱등 — 같은 클러스터의 pending 요청이
        # 이미 있으면 새로 만들지 않고 그 행을 돌려준다 (버튼 더블클릭·
        # 페이지 재방문으로 승인 대기열이 중복으로 쌓이는 것 방지).
        if action_type == "enable_data_api":
            existing = _scan_all(
                table,
                FilterExpression="cluster_id = :c AND action_type = :a AND approval_status = :p",
                ExpressionAttributeValues={
                    ":c": body.get("cluster_id", ""),
                    ":a": "enable_data_api",
                    ":p": "pending",
                },
            )
            if existing:
                return {"statusCode": 200, "headers": headers, "body": json.dumps(existing[0], default=str)}

        item = {
            "approval_id": str(uuid.uuid4()),
            "created_at": now,
            # DynamoDB TTL: pending requests auto-expire 24h after creation so
            # stale, never-approved requests don't linger in the Approval Center
            # (well above the 60-min replay window, so approved rows stay usable).
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 24 * 60 * 60,
            "cluster_id": body.get("cluster_id", ""),
            "tool_name": body.get("tool_name", ""),
            "action_description": body.get("action_description", ""),
            "parameters": json.dumps(body.get("parameters", {})),
            "risk_level": body.get("risk_level", "medium"),
            "requested_by": body.get("requested_by", "agent"),
            "approval_status": "pending",
        }
        # 신형 스키마(action_type + action_details) — request_approval MCP 툴이
        # 만드는 행과 같은 모양이라 Approval Center 카드 렌더러를 공유한다.
        if action_type:
            item["action_type"] = action_type
            item["action_details"] = body.get("action_details") or {}
        table.put_item(Item=item)
        return {"statusCode": 201, "headers": headers, "body": json.dumps(item, default=str)}

    if method == "PUT" and approval_id:
        # P2.4.2 server-side RBAC: only admins may approve or reject operations.
        # Viewers can READ the approval queue but cannot act on it.
        if not _is_admin(event):
            return {
                "statusCode": 403,
                "headers": headers,
                "body": json.dumps({
                    "error": "forbidden",
                    "reason": "admin role required to approve or reject operations",
                }),
            }
        body = json.loads(event.get("body", "{}"))
        action = body.get("action")
        if action not in ("approve", "reject"):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "action must be approve or reject"})}

        items = _scan_all(
            table,
            FilterExpression="approval_id = :aid",
            ExpressionAttributeValues={":aid": approval_id},
        )
        if not items:
            return {"statusCode": 404, "headers": headers, "body": json.dumps({"error": "not found"})}

        item = items[0]

        # Advanced approval — designated approvers + separation of duties.
        # Applies to approve only; reject keeps the _is_admin-only gate so a
        # requester can still cancel their own request.
        if action == "approve":
            approver = _caller_name(event)
            # Depends on the bearer being the Cognito ID token (the frontend
            # sends getValidIdToken()), which always carries cognito:username/
            # email — so a logged-in admin never resolves to "unknown". If the
            # bearer ever switches to an access token (no username claims), this
            # guard would 403 every approval — revisit _caller_name then.
            if not approver or approver == "unknown":
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "not_authenticated",
                        "reason": "승인자를 식별할 수 없습니다.",
                    }),
                }
            if approver.strip().lower() == str(item.get("requested_by") or "").strip().lower():
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "self_approval",
                        "reason": "자기 요청은 승인할 수 없습니다 — 다른 승인자가 처리해야 합니다.",
                    }),
                }
            action_type = item.get("action_type") or item.get("tool_name")
            eligible = _load_eligible_approvers(item.get("cluster_id"), action_type)
            if eligible and approver.strip().lower() not in eligible:
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "not_designated_approver",
                        "reason": "이 작업은 지정된 승인자만 승인할 수 있습니다.",
                    }),
                }

        # pending 상태에서만 전이 허용 — ConditionExpression이 없으면
        # 이미 consumed/rejected된 행도 PUT approve로 다시 approved가 되어,
        # approval_guard의 consume-on-use replay 방어를 API에서 되살릴 수
        # 있다(Codex 감사 적발). 이미 처리된 승인은 409로 거부한다.
        try:
            table.update_item(
                Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
                UpdateExpression="SET approval_status = :s, resolved_at = :t, resolved_by = :by",
                ConditionExpression="approval_status = :pending",
                ExpressionAttributeValues={
                    ":s": "approved" if action == "approve" else "rejected",
                    ":t": datetime.utcnow().isoformat() + "Z",
                    ":by": _caller_name(event),  # stamped from verified token, not caller-supplied body
                    ":pending": "pending",
                },
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return {
                    "statusCode": 409,
                    "headers": headers,
                    "body": json.dumps({
                        "error": "already_resolved",
                        "detail": f"승인 요청이 이미 {item.get('approval_status')} 상태입니다 — 재처리할 수 없습니다.",
                    }),
                }
            raise

        # enable_data_api는 승인 즉시 실행하는 액션 — 쓰기 도구 replay가 없다.
        # 성공하면 행을 consumed로 마감해 Approval Center 의미론(실행된 승인은
        # 재사용 불가)을 유지하고, 실패하면 approved 상태로 남겨 재승인 클릭이
        # 자연스러운 재시도 경로가 되게 한다.
        execution = None
        if action == "approve" and (item.get("action_type") or item.get("tool_name")) == "enable_data_api":
            execution = _execute_enable_data_api(item)
            stamp = datetime.utcnow().isoformat() + "Z"
            if execution.get("ok"):
                table.update_item(
                    Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
                    UpdateExpression="SET approval_status = :c, consumed_at = :t, execution_result = :r",
                    ExpressionAttributeValues={
                        ":c": "consumed",
                        ":t": stamp,
                        ":r": json.dumps(execution, default=str),
                    },
                )
            else:
                table.update_item(
                    Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
                    UpdateExpression="SET execution_error = :e",
                    ExpressionAttributeValues={":e": execution.get("error", "unknown")[:300]},
                )

        # N-① custom-endpoint auto-execute — ONLY origin=="ui" rows. The write
        # runs via the operations Lambda (which owns the tool + approval_guard);
        # its verify_approval consumes the row single-use on success. Chat rows
        # of the same action_type have NO origin → NOT executed here (the agent
        # replays them), so this can never double-execute a write.
        endpoint_execution = None
        row_action = item.get("action_type") or item.get("tool_name")
        if action == "approve" and row_action in _ENDPOINT_ACTIONS and item.get("origin") == "ui":
            endpoint_execution = _execute_endpoint_action(item)

        exec_failed = (execution and not execution.get("ok")) or (
            endpoint_execution and not endpoint_execution.get("executed")
        )
        return {
            "statusCode": 502 if exec_failed else 200,
            "headers": headers,
            "body": json.dumps({"approval_id": approval_id, "status": action + "d",
                                "execution": execution,
                                "endpoint_execution": endpoint_execution}),
        }

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}
