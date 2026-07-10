"""scale_out_with_warmup — approval-gated Aurora reader scale-OUT that also
pre-queues a buffer-pool prewarm (N-④ Phase 1, semi-automatic).

Two human approvals, one flow:
  1. This tool (approval #1) creates a new reader instance AND pre-creates a
     `prewarm_reader` approval row in status `awaiting_instance` (NOT yet
     DBA-visible).
  2. The scheduled restore_finalizer flips that prewarm approval to `pending`
     once the reader instance reaches `available` (now visible in the Approval
     Center), then — after the DBA approves it — invokes THIS Lambda's
     prewarm_reader tool to actually warm the reader.

The prewarm approval ROW is the whole state machine — no new table:
    awaiting_instance → pending → approved → consumed

Why the prewarm approval is minted HERE (not in the finalizer): its
payload_hash must match what prewarm_reader.verify_approval will project, and
only this Lambda can import approval_guard to compute it. The finalizer only
does state transitions + a Lambda invoke; it never hashes and never connects
to a DB.

FAIL-CLOSED like every write tool: verify_approval must pass before any RDS
write, and no str(e) internals leak into a return shown to users (errors are
logged to CloudWatch).
"""

import os
import time
import uuid

import boto3

from mcp_servers.operations.tools.add_reader_instance import _writer_instance_class
from mcp_servers.operations.tools.prewarm_reader import _TOP_N_CAP
from mcp_servers.shared.approval_guard import canonical_action_hash, verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def scale_out_with_warmup_impl(
    cache: CacheClient,
    cluster_id: str,
    new_instance_id: str = "",
    instance_class: str = "",
    endpoint_identifier: str = "",
    top_n: int = 20,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    new_instance_id = (new_instance_id or "").strip()
    instance_class = (instance_class or "").strip()
    endpoint_identifier = (endpoint_identifier or "").strip()
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 20
    # Clamp to prewarm_reader's OWN cap now, so the value we hash into the
    # prewarm approval equals the value prewarm_reader.verify_approval will
    # project (it re-clamps to the same cap → a no-op → identical hash). Import
    # the cap rather than hard-code it so the two stay locked together.
    top_n = max(1, min(top_n, _TOP_N_CAP))

    # The caller must name the new reader — the approval payload hash binds it.
    if not new_instance_id:
        return {"status": "invalid_instance", "cluster_id": cluster_id,
                "reason": "new_instance_id가 필요합니다."}

    if not approved:
        # Resolve the concrete class NOW so the approval payload hash binds the
        # exact billable class the DBA sees — execute never picks a class after
        # approval.
        if not instance_class:
            try:
                rds = client_for_cluster(cluster_id, "rds")
                dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
                instance_class = _writer_instance_class(rds, dbc)
            except Exception as e:
                print(f"[scale_out_with_warmup] preview writer-class lookup failed for {cluster_id}: {e}")
                instance_class = ""
            if not instance_class:
                return {"status": "needs_instance_class", "cluster_id": cluster_id,
                        "reason": "instance_class를 결정할 수 없습니다 — 명시해 주세요 (예: db.serverless)."}
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "new_instance_id": new_instance_id,
            "instance_class": instance_class,
            "endpoint_identifier": endpoint_identifier,
            "top_n": top_n,
            "cli_preview": (
                f"리더 추가 + 자동 예열 (scale-out, 2단계 승인): 클러스터 {cluster_id}에 "
                f"{new_instance_id!r} 리더를 생성합니다 (클래스 {instance_class})"
                + f". 리더가 available되면 크기 상위 {top_n}개 릴레이션 버퍼풀 예열 "
                "승인이 자동으로 승인 대기열에 올라옵니다(DBA 2차 승인 필요). "
                "신규 인스턴스는 과금 대상이며 생성에 수 분이 걸립니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "scale_out_with_warmup",
        payload={"cluster_id": cluster_id, "new_instance_id": new_instance_id,
                 "instance_class": instance_class,
                 "endpoint_identifier": endpoint_identifier, "top_n": top_n},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # The class was resolved in PREVIEW and hash-bound by the approval, so execute
    # uses the exact class the DBA approved — never a post-approval lookup.
    if not instance_class:
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "instance_class가 승인에 바인딩되지 않았습니다 — 미리보기가 제안한 클래스로 다시 승인 요청하세요."}

    # --- a. create the reader (same logic as add_reader_instance) -------------
    rds = client_for_cluster(cluster_id, "rds")
    try:
        dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[scale_out_with_warmup] describe_db_clusters failed for {cluster_id}: {e}")
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "클러스터 조회에 실패했습니다 — 대상 클러스터 식별자를 확인하세요."}

    real_cluster_id = dbc.get("DBClusterIdentifier") or cluster_id
    engine = dbc.get("Engine")

    try:
        rds.create_db_instance(
            DBInstanceIdentifier=new_instance_id,
            DBClusterIdentifier=real_cluster_id,
            Engine=engine,
            DBInstanceClass=instance_class,
            Tags=[{"Key": "dbops:managed", "Value": "scale-out"}],
        )
    except Exception as e:
        print(f"[scale_out_with_warmup] create_db_instance failed for {new_instance_id}: {e}")
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 추가에 실패했습니다 (식별자 중복·용량·권한 확인)."}

    # --- b. pre-create the WARM approval row (status awaiting_instance) --------
    warm_details = {
        "cluster_id": cluster_id,
        "reader_instance_id": new_instance_id,
        "endpoint_identifier": endpoint_identifier,
        "top_n": top_n,
    }
    warm_id = _queue_prewarm_approval(cluster_id, warm_details)
    if not warm_id:
        # The reader IS being created; we just couldn't queue the auto-warm.
        # Surface it so the DBA can prewarm manually — do NOT fail the whole
        # scale-out (the billable reader already exists).
        return {"status": "scaleout_started", "cluster_id": cluster_id,
                "instance_id": new_instance_id, "warm_approval_id": None,
                "note": "리더 생성 중 — 예열 승인 자동 등록에 실패했습니다. "
                        "available 후 prewarm_reader로 수동 예열하세요."}

    return {
        "status": "scaleout_started",
        "cluster_id": cluster_id,
        "instance_id": new_instance_id,
        "warm_approval_id": warm_id,
        "note": "리더 생성 중 — available되면 예열 승인이 자동으로 승인 대기열에 올라옵니다",
    }


def _queue_prewarm_approval(cluster_id: str, warm_details: dict) -> str:
    """Write a prewarm_reader approval row in status `awaiting_instance` with the
    scale-out markers the finalizer needs, and return the new approval_id (or ""
    on failure).

    payload_hash is computed with the SAME canonical_action_hash that
    request_approval uses, over the SAME projection prewarm_reader.verify_approval
    checks — so drift is impossible: it is literally the same function on the
    same dict shape ({cluster_id, reader_instance_id, endpoint_identifier,
    top_n})."""
    table_name = os.environ.get("APPROVALS_TABLE", "")
    if not table_name:
        print("[scale_out_with_warmup] APPROVALS_TABLE not configured — cannot queue prewarm")
        return ""
    reg = lookup_cluster(cluster_id)
    approval_id = str(uuid.uuid4())
    try:
        boto3.resource("dynamodb").Table(table_name).put_item(Item={
            "approval_id": approval_id,
            "created_at": str(int(time.time() * 1000)),  # ms-epoch string sort key
            # 24h TTL like request_approval — the reader is available within
            # minutes, so this leaves the DBA the rest of the day to approve.
            "ttl": int(time.time()) + 24 * 60 * 60,
            # NOT yet DBA-visible; the finalizer flips this to "pending" once the
            # reader instance reaches `available`.
            "approval_status": "awaiting_instance",
            "cluster_id": cluster_id,
            "action_type": "prewarm_reader",
            "action_details": warm_details,  # only strings + one int → DDB-safe
            "payload_hash": canonical_action_hash("prewarm_reader", warm_details),
            "requested_by": "scale-out",
            # scale-out state-machine markers: the finalizer scans on these, acts
            # cross-account via region+spoke_role_arn, and polls reader_instance_id
            # for availability.
            "scaleout": True,
            "reader_instance_id": warm_details["reader_instance_id"],
            "region": reg.get("region", ""),
            "spoke_role_arn": reg.get("spoke_role_arn", ""),
        })
    except Exception as e:
        print(f"[scale_out_with_warmup] queue prewarm approval failed for {cluster_id}: {e}")
        return ""
    return approval_id
