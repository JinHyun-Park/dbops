"""set_docdb_profiler — approval-gated DocumentDB profiler-level change over the
Mongo wire protocol (`db.command("profile", level, slowms=slowms)`).

This executes what the read-only collector's `docdb_mongo_profiler_off` finding
recommends: turn the database profiler on (level 1, slowms threshold) so slow ops
land in system.profile. It is a hardcoded single-command WRITE — there is NO
generic runCommand/eval surface, mirroring the read collector's allowlist.

Safety model (mirrors the DynamoDB write tools + the spec's 7 fixes):
  - Separate WRITE credentials: connect with `mongo_write_secret_arn` from the
    registry row (NOT the collector's read-only `mongo_secret_arn`). A documentdb
    cluster without that field → {"status":"unsupported_engine", ...} (no-op).
  - FAIL-CLOSED engine gate is enforced in the handler (docdb_write capability);
    a None family never reaches this impl.
  - Approval-gated 3-state flow (approval_required → verify_approval → execute).
  - Idempotent: a request-time read of the current profiling status returns a
    no-change status when already at the requested level+slowms.
  - TOCTOU (fix #6): re-read the profiling status IMMEDIATELY before the write;
    if it drifted from what the approval was bound to, abort.
  - NEVER raises into the caller — any pymongo/guard error degrades to
    {"status":"error", reason}.

pymongo is imported lazily inside `_client_factory` (NOT at module top) so the
unit tests patch the module-level `_CLIENT_FACTORY` hook and run WITHOUT pymongo
installed — identical to the read collector.
"""

import json
import os

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import lookup_cluster

# TLS CA bundle for DocumentDB, vendored into the operations asset during CDK
# bundling (downloaded from truststore.pki.rds.amazonaws.com) or committed as a
# fallback. Resolved relative to this file so the path is valid when deployed.
_CA_BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "global-bundle.pem",
)

# Mongo server-selection timeout — fail fast on an unreachable cluster.
SERVER_SELECTION_TIMEOUT_MS = 5000

# Allowed profiler levels (Mongo): 0=off, 1=slow ops only, 2=all ops.
_VALID_LEVELS = (0, 1, 2)


def _client_factory(host, port, username, password):
    """Default MongoClient factory. Imports pymongo lazily so the module can be
    loaded (and unit-tested) without pymongo installed. Tests patch this with a
    fake-client factory via the module-level _CLIENT_FACTORY hook below.

    A WRITE client: retryWrites=False (DocumentDB does not support retryable
    writes), tls=True with the CA bundle, primary read preference (no secondary)."""
    import pymongo  # lazy: not importable in the test env

    return pymongo.MongoClient(
        host=host,
        port=int(port),
        username=username,
        password=password,
        tls=True,
        tlsCAFile=_CA_BUNDLE_PATH,
        retryWrites=False,
        serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
    )


# Indirection so tests can inject a fake client without importing pymongo.
_CLIENT_FACTORY = _client_factory


def _write_creds(cluster_id: str):
    """Resolve the RW Mongo secret for the cluster. Returns (creds, error) where
    creds is {host, port, username, password} or error is a status dict (no-op /
    error) that the caller returns verbatim. The write secret is a SEPARATE
    registry field from the collector's read-only `mongo_secret_arn`."""
    row = lookup_cluster(cluster_id)
    secret_arn = row.get("mongo_write_secret_arn")
    if not secret_arn:
        return None, {
            "status": "unsupported_engine",
            "reason": "no write credentials configured",
            "cluster_id": cluster_id,
        }
    try:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn).get(
            "SecretString"
        ) or "{}"
        creds = json.loads(raw)
    except Exception as e:
        return None, {
            "status": "error",
            "reason": f"쓰기 자격증명 조회 실패: {str(e)[:200]}",
            "cluster_id": cluster_id,
        }
    host = creds.get("host")
    username = creds.get("username")
    password = creds.get("password")
    if not host or not username or not password:
        return None, {
            "status": "error",
            "reason": "쓰기 자격증명이 불완전합니다 (host/username/password 누락).",
            "cluster_id": cluster_id,
        }
    return (
        {"host": host, "port": creds.get("port", 27017), "username": username, "password": password},
        None,
    )


def _current_profiling(client, db: str) -> dict:
    """Current profiling level + slowms via `profile: -1` (read-only — does NOT
    change the level)."""
    res = client[db].command("profile", -1)
    level = res.get("was", res.get("level", 0))
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 0
    slowms = res.get("slowms")
    try:
        slowms = int(slowms)
    except (TypeError, ValueError):
        slowms = None
    return {"level": level, "slowms": slowms}


def set_docdb_profiler_impl(
    cache,
    cluster_id: str,
    db: str = "admin",
    level: int = 1,
    slowms: int = 100,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """db.command("profile", level, slowms=slowms) for the target DocumentDB db.
    Approval-gated; never raises. The approval binds {db, level, slowms}."""
    db = (db or "admin").strip() or "admin"

    # --- validate BEFORE any connect/write (no partial writes) ---
    try:
        level_i = int(level)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "reason": "level은 정수여야 합니다 (0/1/2).",
            "cluster_id": cluster_id,
        }
    if level_i not in _VALID_LEVELS:
        return {
            "status": "error",
            "reason": f"level은 0, 1, 2 중 하나여야 합니다 (받은 값: {level!r}).",
            "cluster_id": cluster_id,
        }
    try:
        slowms_i = int(slowms)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "reason": "slowms는 정수여야 합니다 (밀리초).",
            "cluster_id": cluster_id,
        }
    if slowms_i < 0:
        return {
            "status": "error",
            "reason": "slowms는 0 이상이어야 합니다.",
            "cluster_id": cluster_id,
        }

    creds, err = _write_creds(cluster_id)
    if err is not None:
        return err

    client = None
    try:
        try:
            client = _CLIENT_FACTORY(
                creds["host"], creds["port"], creds["username"], creds["password"]
            )
        except Exception as e:
            return {
                "status": "error",
                "reason": f"DocumentDB 연결 실패: {str(e)[:200]}",
                "cluster_id": cluster_id,
            }

        # --- request-time read: surface current state + idempotent skip ---
        try:
            state = _current_profiling(client, db)
        except Exception as e:
            return {
                "status": "error",
                "reason": f"프로파일러 상태 조회 실패 — 적용 전 현재 상태를 확인할 수 없어 중단합니다: {str(e)[:200]}",
                "cluster_id": cluster_id,
            }

        # Idempotent: already at the requested level (+ slowms when level>0).
        if state["level"] == level_i and (level_i == 0 or state["slowms"] == slowms_i):
            return {
                "status": "skipped",
                "reason": "프로파일러가 이미 요청한 상태입니다 (변경 없음).",
                "cluster_id": cluster_id,
                "db": db,
                "level": level_i,
                "slowms": slowms_i,
            }

        payload = {"db": db, "level": level_i, "slowms": slowms_i}

        warnings = []
        if level_i == 2:
            warnings.append(
                "level 2는 모든 op을 기록합니다 (system.profile 쓰기 부하 + 디스크 사용 증가). "
                "운영 환경에서는 level 1 + 적절한 slowms를 권장합니다."
            )

        if not approved:
            return {
                "status": "approval_required",
                "cluster_id": cluster_id,
                "db": db,
                "level": level_i,
                "slowms": slowms_i,
                "current_state": state,
                "warnings": warnings,
            }

        guard = verify_approval(
            approval_id, cluster_id, "set_docdb_profiler", payload=payload
        )
        if not guard.get("ok"):
            return {
                "status": "approval_denied",
                "reason": guard.get("reason", "approval guard rejected the request"),
                "cluster_id": cluster_id,
            }

        # --- TOCTOU re-read (fix #6): re-check IMMEDIATELY before the write ---
        try:
            fresh = _current_profiling(client, db)
        except Exception as e:
            return {
                "status": "error",
                "reason": f"적용 직전 재조회 실패 — 안전을 위해 중단합니다: {str(e)[:200]}",
                "cluster_id": cluster_id,
            }
        if fresh != state:
            return {
                "status": "approval_denied",
                "reason": "profiler state changed since approval",
                "cluster_id": cluster_id,
            }

        # --- the single allowlisted write ---
        try:
            client[db].command("profile", level_i, slowms=slowms_i)
        except Exception as e:
            return {
                "status": "error",
                "reason": f"프로파일러 설정 실패: {str(e)[:200]}",
                "cluster_id": cluster_id,
            }

        return {
            "status": "modified",
            "cluster_id": cluster_id,
            "db": db,
            "level": level_i,
            "slowms": slowms_i,
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
