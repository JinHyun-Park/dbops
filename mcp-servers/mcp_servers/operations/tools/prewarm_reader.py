"""prewarm_reader — approval-gated Aurora PostgreSQL reader buffer-cache prewarm.

A new/cold reader instance has an empty buffer pool, so its first production
queries hit storage (slow). This tool warms the reader's buffer pool BEFORE it
takes traffic:

  1. (optional) Exclude the reader from a custom endpoint so no prod traffic
     reaches it while cold.
  2. CREATE EXTENSION IF NOT EXISTS pg_prewarm/pg_buffercache — run on the WRITER
     via RDS Data API (readers are read-only; Aurora shares the catalog through
     storage, so the extensions then exist cluster-wide).
  3. Connect DIRECTLY to the reader INSTANCE endpoint (Data API is cluster-scoped
     and CANNOT target one instance), pick the top-N relations by size, run
     pg_prewarm on each, and measure buffer occupancy before/after via
     pg_buffercache.
  4. (optional) Re-include the reader in the endpoint.

Engine gate: the handler positive-gates this tool on the relational-only
`prewarm` capability, so non-relational engines get unsupported_engine before the
impl runs. pg_prewarm is PostgreSQL-specific, so the impl additionally refuses
Aurora MySQL.
# ponytail: MySQL buffer warming would be scan-based (SELECT the hot tables) —
# a different mechanism, explicitly out of scope for v1. Upgrade path: add a
# MySQL branch that runs `SELECT COUNT(*)`-style table scans if demand appears.

FAIL-CLOSED like every write tool: any doubt on approval refuses, and no str(e)
internals leak into a return shown to users (errors are logged to CloudWatch).
"""

import json
import time

from mcp_servers.shared import pg_direct
from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

# ponytail: cap relations + wall-clock. Lambda timeout is 120s; stop launching
# new prewarms past the budget and never warm an unbounded number of relations.
# Ceiling: a huge cold reader won't be fully warmed in one call — re-run to
# continue, or raise the budget if the Lambda timeout is raised too.
_TOP_N_CAP = 50
_WALL_BUDGET_SECONDS = 60

# Direct-connection queries (pg8000, not routed through CacheClient) must carry
# the /* source=dbops-agent */ audit marker themselves — the CacheClient Data-API
# path injects it automatically, but this path does not.
_SRC = "/* source=dbops-agent */ "

# User relations only. Excluding just pg_catalog/information_schema is NOT
# enough — pg_toast (and pg_temp*) are separate schemas whose largest members
# (e.g. system-catalog TOAST indexes) sort to the top by size, and the Aurora
# master user (rds_superuser, not a real superuser) can't pg_prewarm system
# TOAST → the whole run aborts with "permission denied". `left(nspname,3) <>
# 'pg_'` drops every pg_* schema; warming user data is the point anyway.
_TOP_REL_SQL = (
    _SRC + "SELECT c.oid::regclass::text AS rel, pg_relation_size(c.oid) AS bytes "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r','i') "
    "AND left(n.nspname, 3) <> 'pg_' AND n.nspname <> 'information_schema' "
    "ORDER BY bytes DESC LIMIT :n"
)


def _build_plan(cluster_id, reader_instance_id, endpoint_identifier, top_n):
    """Human-readable multi-line step plan shown on the approval card via the
    generic cli_preview renderer (this is NOT a shell command)."""
    lines = [
        "prewarm_reader 실행 계획 (Aurora PostgreSQL 리더 버퍼풀 예열)",
        f"대상: 클러스터 {cluster_id} / 리더 인스턴스 {reader_instance_id}",
    ]
    step = 1
    if endpoint_identifier:
        lines.append(
            f"{step}) 커스텀 엔드포인트 {endpoint_identifier}에서 리더 일시 제외 "
            "(콜드 상태로 프로덕션 트래픽 차단)"
        )
        step += 1
    lines.append(
        f"{step}) writer에 CREATE EXTENSION IF NOT EXISTS pg_prewarm, pg_buffercache "
        "(Aurora 공유 카탈로그로 클러스터 전역 적용)"
    )
    step += 1
    lines.append(
        f"{step}) 리더 인스턴스 엔드포인트에 직접 접속 → 크기 상위 {top_n}개 릴레이션을 "
        "pg_prewarm으로 버퍼풀에 적재"
    )
    step += 1
    lines.append(f"{step}) pg_buffercache로 예열 전/후 버퍼 점유 측정")
    step += 1
    if endpoint_identifier:
        lines.append(f"{step}) 엔드포인트에서 리더 재편입 (트래픽 재개)")
    return "\n".join(lines)


def _is_postgres(cache, cluster_id):
    """True only for Aurora PostgreSQL. The handler gate already ensured the
    family is relational; this distinguishes PG from MySQL (both relational)."""
    try:
        res = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception as e:
        print(f"[prewarm_reader] engine lookup failed for {cluster_id}: {e}")
        return False
    rows = getattr(res, "rows", res)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return "postgres" in str(rows[0].get("engine") or "").lower()
    return False


def _resolve_reader(rds, cluster_id, reader_instance_id):
    """Confirm reader_instance_id is a READER member of this cluster and resolve
    its instance endpoint Address+Port. Returns {"status":"ok","address","port"}
    or an error-shaped dict. IsClusterWriter lives on the cluster's member list
    (describe_db_clusters); the endpoint Address lives on describe_db_instances."""
    try:
        dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[prewarm_reader] describe_db_clusters failed for {cluster_id}: {e}")
        return {"status": "error", "cluster_id": cluster_id,
                "reason": "클러스터 조회에 실패했습니다 — 대상 클러스터 식별자를 확인하세요."}

    members = {m.get("DBInstanceIdentifier"): m for m in dbc.get("DBClusterMembers") or []}
    member = members.get(reader_instance_id)
    if not member:
        return {"status": "reader_not_found", "cluster_id": cluster_id,
                "reason": f"{reader_instance_id!r} 인스턴스가 이 클러스터의 멤버가 아닙니다."}
    if member.get("IsClusterWriter"):
        return {"status": "not_a_reader", "cluster_id": cluster_id,
                "reason": f"{reader_instance_id!r} 는 writer 인스턴스입니다 — 리더만 예열 대상입니다."}

    # describe_db_instances filter name is `db-cluster-id` (RDS API).
    try:
        di = rds.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}]
        )
    except Exception as e:
        print(f"[prewarm_reader] describe_db_instances failed for {cluster_id}: {e}")
        return {"status": "error", "cluster_id": cluster_id,
                "reason": "인스턴스 조회에 실패했습니다."}

    for inst in di.get("DBInstances") or []:
        if inst.get("DBInstanceIdentifier") == reader_instance_id:
            ep = inst.get("Endpoint") or {}
            addr = ep.get("Address")
            if addr:
                return {"status": "ok", "address": addr, "port": ep.get("Port", 5432)}
    return {"status": "reader_not_found", "cluster_id": cluster_id,
            "reason": "리더 인스턴스의 엔드포인트 주소를 확인할 수 없습니다."}


def _endpoint_excluded_members(rds, endpoint_identifier):
    resp = rds.describe_db_cluster_endpoints(
        DBClusterEndpointIdentifier=endpoint_identifier
    )
    eps = resp.get("DBClusterEndpoints") or []
    if not eps:
        return None
    return set(eps[0].get("ExcludedMembers") or [])


def _set_reader_excluded(rds, endpoint_identifier, reader_instance_id, excluded):
    """Add or remove the reader from the endpoint's ExcludedMembers (a set — AWS
    ignores order). ponytail: only ExcludedMembers are managed; a StaticMembers
    (explicit-include) endpoint isn't touched here — excluding is moot when the
    reader isn't in the static list. Upgrade path: also prune StaticMembers if a
    static-endpoint use case appears."""
    current = _endpoint_excluded_members(rds, endpoint_identifier)
    if current is None:
        return
    new = (current | {reader_instance_id}) if excluded else (current - {reader_instance_id})
    rds.modify_db_cluster_endpoint(
        DBClusterEndpointIdentifier=endpoint_identifier,
        ExcludedMembers=sorted(new),
    )


def _reinclude_with_retry(rds, endpoint_identifier, reader_instance_id, attempts=4, delay=15):
    """Re-include the reader, retrying while the endpoint is still settling from
    the earlier exclude. A member-list change puts the endpoint in 'modifying'
    for tens of seconds, so an immediate re-include races it and raises
    InvalidDBClusterEndpointStateFault — which is exactly how a fast failure (or
    a fast warm) could strand the reader OUT of the endpoint. Bounded so the
    Lambda can't hang past its timeout (attempts*delay stays well under 120s)."""
    for i in range(attempts):
        try:
            _set_reader_excluded(rds, endpoint_identifier, reader_instance_id, False)
            return True
        except Exception as e:  # noqa: BLE001 - want to inspect the fault code
            transient = "modifying" in str(e) or "InvalidDBClusterEndpointStateFault" in str(e)
            if transient and i < attempts - 1:
                time.sleep(delay)
                continue
            print(f"[prewarm_reader] re-include of {reader_instance_id} failed: {e}")
            return False
    return False


def _reader_creds(cache, cluster_id):
    """Resolve (creds, db_name) for the target cluster's master secret, read in
    the cluster's own account. Returns (None, reason) on any failure."""
    target = cache._resolve_target(cluster_id) or {}
    secret_arn = target.get("secret_arn")
    db_name = target.get("db_name") or "postgres"
    if not secret_arn:
        return None, "대상 클러스터의 시크릿을 레지스트리에서 찾을 수 없습니다."
    try:
        raw = client_for_cluster(cluster_id, "secretsmanager").get_secret_value(
            SecretId=secret_arn
        ).get("SecretString") or "{}"
        creds = json.loads(raw)
    except Exception as e:
        print(f"[prewarm_reader] secret lookup failed for {cluster_id}: {e}")
        return None, "대상 클러스터 자격증명 조회에 실패했습니다."
    if not creds.get("username") or not creds.get("password"):
        return None, "자격증명이 불완전합니다 (username/password 누락)."
    return {"username": creds["username"], "password": creds["password"]}, db_name


def _one(rows, key, default=0):
    if rows and isinstance(rows[0], dict) and key in rows[0]:
        return rows[0][key]
    return default


def prewarm_reader_impl(
    cache: CacheClient,
    cluster_id: str,
    reader_instance_id: str = "",
    endpoint_identifier: str = "",
    top_n: int = 20,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    reader_instance_id = (reader_instance_id or "").strip()
    endpoint_identifier = (endpoint_identifier or "").strip()
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 20
    top_n = max(1, min(top_n, _TOP_N_CAP))

    if not reader_instance_id:
        return {"status": "invalid_reader_instance", "cluster_id": cluster_id,
                "reason": "reader_instance_id가 필요합니다."}

    # PG-only gate (handler already ensured relational). See module docstring.
    if not _is_postgres(cache, cluster_id):
        return {"status": "unsupported_engine", "cluster_id": cluster_id,
                "reason": "prewarm_reader는 Aurora PostgreSQL 전용입니다 "
                          "(MySQL 버퍼 예열은 스캔 기반으로 v1 범위 외)."}

    plan = _build_plan(cluster_id, reader_instance_id, endpoint_identifier, top_n)

    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id,
                "reader_instance_id": reader_instance_id,
                "endpoint_identifier": endpoint_identifier, "top_n": top_n,
                "cli_preview": plan}

    guard = verify_approval(
        approval_id, cluster_id, "prewarm_reader",
        payload={"cluster_id": cluster_id, "reader_instance_id": reader_instance_id,
                 "endpoint_identifier": endpoint_identifier, "top_n": top_n},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    rds = client_for_cluster(cluster_id, "rds")
    resolved = _resolve_reader(rds, cluster_id, reader_instance_id)
    if resolved.get("status") != "ok":
        return resolved  # no writes done yet — nothing to undo
    host, port = resolved["address"], resolved["port"]

    excluded = False
    try:
        if endpoint_identifier:
            _set_reader_excluded(rds, endpoint_identifier, reader_instance_id, True)
            excluded = True

        # CREATE EXTENSION runs on the WRITER via Data API. IF NOT EXISTS makes
        # it idempotent; log (don't fail) so a transient DDL hiccup surfaces in
        # CloudWatch rather than aborting — a missing extension fails loudly at
        # the pg_prewarm/pg_buffercache query below anyway.
        for ext in ("pg_prewarm", "pg_buffercache"):
            try:
                cache.execute_on_target(cluster_id, f"CREATE EXTENSION IF NOT EXISTS {ext}")
            except Exception as e:
                print(f"[prewarm_reader] CREATE EXTENSION {ext} failed on {cluster_id}: {e}")

        creds, db_name = _reader_creds(cache, cluster_id)
        if creds is None:
            return {"status": "error", "cluster_id": cluster_id, "reason": db_name}

        try:
            conn = pg_direct.connect(host, port, db_name, creds["username"], creds["password"])
        except Exception as e:
            print(f"[prewarm_reader] direct connect to {reader_instance_id} failed: {e}")
            return {"status": "connect_failed", "cluster_id": cluster_id,
                    "reader_instance_id": reader_instance_id,
                    "hint": "대상 클러스터 SG가 operations Lambda SG의 인그레스"
                            "(포트 5432)를 허용하는지 확인하세요"}

        try:
            buffers_before = _one(pg_direct.query(conn, _SRC + "SELECT count(*) AS n FROM pg_buffercache"), "n")
            rels = pg_direct.query(conn, _TOP_REL_SQL, {"n": top_n})
            warmed, total_blocks = [], 0
            deadline = time.time() + _WALL_BUDGET_SECONDS
            for r in rels:
                if time.time() > deadline:
                    break  # ponytail: budget exhausted — stop starting new prewarms
                # Per-relation guard: one relation we can't prewarm (permission,
                # dropped mid-run) must not abort the whole warm. pg8000 runs in
                # autocommit so a failed statement doesn't poison the next.
                try:
                    blocks = int(_one(
                        pg_direct.query(conn, _SRC + "SELECT pg_prewarm(:rel) AS blocks", {"rel": r["rel"]}),
                        "blocks",
                    ) or 0)
                except Exception as e:  # noqa: BLE001
                    print(f"[prewarm_reader] skip relation {r.get('rel')}: {e}")
                    continue
                warmed.append({"rel": r["rel"], "blocks": blocks})
                total_blocks += blocks
            buffers_after = _one(pg_direct.query(conn, _SRC + "SELECT count(*) AS n FROM pg_buffercache"), "n")
        except Exception as e:
            print(f"[prewarm_reader] prewarm query failed on {reader_instance_id}: {e}")
            return {"status": "prewarm_failed", "cluster_id": cluster_id,
                    "reader_instance_id": reader_instance_id,
                    "reason": "버퍼 예열 쿼리 실행에 실패했습니다 (pg_prewarm/pg_buffercache 확인)."}
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {
            "status": "prewarmed",
            "cluster_id": cluster_id,
            "reader_instance_id": reader_instance_id,
            "endpoint_identifier": endpoint_identifier,
            "relations_warmed": warmed,
            "total_blocks": total_blocks,
            "buffers_before": buffers_before,
            "buffers_after": buffers_after,
            "endpoint_choreography": "excluded→included" if excluded else "skipped",
        }
    finally:
        # Safety net: whether we succeeded or bailed, never leave the reader
        # stranded out of the endpoint. Bounded retry rides out the endpoint's
        # post-exclude 'modifying' window instead of failing on the first race.
        if excluded:
            _reinclude_with_retry(rds, endpoint_identifier, reader_instance_id)
