import json
import os
import uuid
from datetime import datetime

import boto3


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


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["APPROVALS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath") or event.get("path") or ""
    path_params = event.get("pathParameters") or {}
    approval_id = path_params.get("id")
    qsp = event.get("queryStringParameters") or {}

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

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

        response = table.scan(**scan_kwargs)
        items = sorted(
            response.get("Items", []),
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )[:limit]
        # Strip noisy fields so the activity feed stays scannable. The
        # action_details JSON can be huge for big DDL — keep a head
        # excerpt only.
        compact = []
        for it in items:
            details = it.get("action_details") or it.get("parameters") or {}
            if isinstance(details, str):
                details_str = details
            else:
                details_str = json.dumps(details, default=str)
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
        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({"items": compact, "count": len(compact)}, default=str),
        }

    if method == "GET" and not approval_id:
        status_filter = qsp.get("status", "pending")
        response = table.scan(
            FilterExpression="approval_status = :s",
            ExpressionAttributeValues={":s": status_filter},
        )
        items = sorted(response.get("Items", []), key=lambda x: x.get("created_at", ""), reverse=True)
        return {"statusCode": 200, "headers": headers, "body": json.dumps(items, default=str)}

    if method == "GET" and approval_id:
        response = table.get_item(Key={"approval_id": approval_id, "created_at": qsp.get("created_at", "")})
        item = response.get("Item")
        if not item:
            response = table.scan(
                FilterExpression="approval_id = :aid",
                ExpressionAttributeValues={":aid": approval_id},
            )
            items = response.get("Items", [])
            item = items[0] if items else None
        return {
            "statusCode": 200 if item else 404,
            "headers": headers,
            "body": json.dumps(item or {"error": "not found"}, default=str),
        }

    if method == "POST":
        body = json.loads(event.get("body", "{}"))
        now = datetime.utcnow().isoformat()
        action_type = body.get("action_type", "")

        # UI발 enable_data_api 요청은 멱등 — 같은 클러스터의 pending 요청이
        # 이미 있으면 새로 만들지 않고 그 행을 돌려준다 (버튼 더블클릭·
        # 페이지 재방문으로 승인 대기열이 중복으로 쌓이는 것 방지).
        if action_type == "enable_data_api":
            existing = table.scan(
                FilterExpression="cluster_id = :c AND action_type = :a AND approval_status = :p",
                ExpressionAttributeValues={
                    ":c": body.get("cluster_id", ""),
                    ":a": "enable_data_api",
                    ":p": "pending",
                },
            ).get("Items") or []
            if existing:
                return {"statusCode": 200, "headers": headers, "body": json.dumps(existing[0], default=str)}

        item = {
            "approval_id": str(uuid.uuid4()),
            "created_at": now,
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
        body = json.loads(event.get("body", "{}"))
        action = body.get("action")
        if action not in ("approve", "reject"):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "action must be approve or reject"})}

        response = table.scan(
            FilterExpression="approval_id = :aid",
            ExpressionAttributeValues={":aid": approval_id},
        )
        items = response.get("Items", [])
        if not items:
            return {"statusCode": 404, "headers": headers, "body": json.dumps({"error": "not found"})}

        item = items[0]
        table.update_item(
            Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
            UpdateExpression="SET approval_status = :s, resolved_at = :t, resolved_by = :by",
            ExpressionAttributeValues={
                ":s": "approved" if action == "approve" else "rejected",
                ":t": datetime.utcnow().isoformat() + "Z",
                ":by": body.get("approved_by", "dba"),
            },
        )

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

        return {
            "statusCode": 200 if not (execution and not execution.get("ok")) else 502,
            "headers": headers,
            "body": json.dumps({"approval_id": approval_id, "status": action + "d", "execution": execution}),
        }

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}
