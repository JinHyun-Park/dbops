"""create_docdb_index — approval-gated DocumentDB index creation over the Mongo
wire protocol (`db[collection].create_index(keys, background=True, name=name)`).

A hardcoded single-command WRITE — there is NO generic runCommand/eval surface,
mirroring the read collector's allowlist. `background=True` so a large-collection
build does not block the primary.

Safety model (mirrors set_docdb_profiler + the DynamoDB write tools):
  - Separate WRITE credentials: connect with `mongo_write_secret_arn` from the
    registry row. A documentdb cluster without it → {"status":"unsupported_engine",
    ...} (no-op).
  - FAIL-CLOSED engine gate enforced in the handler (docdb_write capability).
  - Approval-gated 3-state flow.
  - Idempotent: if an index with the requested `name` already exists, no-change.
  - Ordered keys (fix #2): compound-index field ORDER is semantically significant,
    so keys is an ORDERED list of (field, direction) tuples — NEVER sorted. The
    approval binds the exact ordered list, and we execute that same ordered list.
  - NEVER raises into the caller: any pymongo/guard error → {"status":"error", ...}
    with a STATIC Korean reason; the detail goes to the module logger. Raw exception
    text must never reach a tool response: a pymongo/Secrets Manager error carries the
    cluster endpoint, the secret ARN and the platform role name, and the request-time
    list_indexes below is reachable by any chat user before an approval exists.

pymongo is imported lazily inside `_client_factory` so the unit tests patch the
module-level `_CLIENT_FACTORY` hook and run WITHOUT pymongo installed.
"""

import json
import logging
import os

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import lookup_cluster

logger = logging.getLogger(__name__)

_CA_BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "global-bundle.pem",
)

SERVER_SELECTION_TIMEOUT_MS = 5000


def _client_factory(host, port, username, password):
    """Default MongoClient factory (WRITE). Imports pymongo lazily so the module
    loads (and is unit-tested) without pymongo installed; tests patch the
    module-level _CLIENT_FACTORY hook below."""
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


_CLIENT_FACTORY = _client_factory


def _write_creds(cluster_id: str):
    """Resolve the RW Mongo secret. Returns (creds, error) — error is a status
    dict the caller returns verbatim. SEPARATE field from the collector's
    read-only `mongo_secret_arn`."""
    row = lookup_cluster(cluster_id)
    secret_arn = row.get("mongo_write_secret_arn")
    if not secret_arn:
        # NOT `unsupported_engine`. That status means "this engine family cannot do
        # this", and a DocumentDB cluster reading it concludes index creation is
        # unavailable for DocumentDB, which is false: it is a per-cluster
        # CONFIGURATION gap. Measured 2026-08-02 on the real docdb cluster, where
        # the tool reported unsupported_engine on the one family it exists for.
        # The reason names the field and the fix, because "no write credentials
        # configured" did not say WHICH credential or WHERE to set it.
        return None, {
            "status": "write_not_configured",
            "cluster_id": cluster_id,
            "reason": (
                "이 클러스터 레지스트리 행에 mongo_write_secret_arn이 설정되지 "
                "않았습니다. DocumentDB 인덱스 생성은 쓰기 전용 Mongo 자격증명을 "
                "요구하며, 수집기가 쓰는 읽기 전용 mongo_secret_arn과 의도적으로 "
                "분리되어 있습니다. /clusters에서 이 클러스터의 "
                "mongo_write_secret_arn을 등록하세요. (엔진이 지원하지 않는 것이 "
                "아니라 설정이 비어 있는 것입니다.)"
            ),
        }
    try:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn).get(
            "SecretString"
        ) or "{}"
        creds = json.loads(raw)
    except Exception:
        logger.warning("mongo write secret fetch failed for %s", cluster_id, exc_info=True)
        return None, {
            "status": "error",
            "reason": (
                "쓰기 자격증명(Secrets Manager) 조회에 실패했습니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
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


def _normalize_keys(keys):
    """Validate + normalize the index key spec into an ORDERED list of
    (field, direction) tuples, preserving the caller's order (compound-index
    order is semantic — never sort). Accepts an ordered list/tuple of
    [field, dir] pairs OR a dict (insertion-ordered in py3.7+).

    Returns (keys_list, error) where keys_list is [(field, dir), ...] and
    direction ∈ {1, -1}; error is a message string on validation failure."""
    if isinstance(keys, dict):
        items = list(keys.items())
    elif isinstance(keys, (list, tuple)):
        items = []
        for pair in keys:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return None, "keys의 각 항목은 [field, direction] 쌍이어야 합니다."
            items.append((pair[0], pair[1]))
    else:
        return None, "keys는 [field, direction] 쌍의 순서 있는 리스트(또는 dict)여야 합니다."

    if not items:
        return None, "keys는 비어 있을 수 없습니다 (최소 1개 필드 필요)."

    out = []
    for field, direction in items:
        field = str(field or "").strip()
        if not field:
            return None, "인덱스 필드 이름이 비어 있습니다."
        try:
            d = int(direction)
        except (TypeError, ValueError):
            return None, f"direction은 1 또는 -1이어야 합니다 (필드 {field!r}: {direction!r})."
        if d not in (1, -1):
            return None, f"direction은 1(오름차순) 또는 -1(내림차순)이어야 합니다 (필드 {field!r}: {d})."
        out.append((field, d))
    return out, None


def _index_names(client, db: str, collection: str) -> set:
    """Existing index names for the collection via list_indexes."""
    names = set()
    for idx in client[db][collection].list_indexes():
        try:
            name = idx.get("name")
        except AttributeError:
            name = None
        if name:
            names.add(name)
    return names


def create_docdb_index_impl(
    cache,
    cluster_id: str,
    db: str = "",
    collection: str = "",
    keys=None,
    name: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """db[collection].create_index(<ordered (field,dir) list>, background=True,
    name=name). Approval-gated; never raises. The approval binds
    {db, collection, keys:[[field,dir],...] (ORDERED), name}."""
    db = (db or "").strip()
    collection = (collection or "").strip()
    name = (name or "").strip()

    # --- validate BEFORE any connect/write (no partial writes) ---
    if not db:
        return {"status": "error", "reason": "db가 필요합니다.", "cluster_id": cluster_id}
    if not collection:
        return {"status": "error", "reason": "collection이 필요합니다.", "cluster_id": cluster_id}
    if not name:
        return {
            "status": "error",
            "reason": "name이 필요합니다 (인덱스 이름은 필수).",
            "cluster_id": cluster_id,
        }

    keys_list, kerr = _normalize_keys(keys)
    if kerr:
        return {"status": "error", "reason": kerr, "cluster_id": cluster_id}

    creds, err = _write_creds(cluster_id)
    if err is not None:
        return err

    # The ordered [field, direction] pairs the approval is bound to (fix #2).
    payload = {
        "db": db,
        "collection": collection,
        "keys": [[f, d] for f, d in keys_list],
        "name": name,
    }

    client = None
    try:
        try:
            client = _CLIENT_FACTORY(
                creds["host"], creds["port"], creds["username"], creds["password"]
            )
        except Exception:
            logger.warning("docdb write connect failed for %s", cluster_id, exc_info=True)
            return {
                "status": "error",
                "reason": "DocumentDB 연결 실패 (자세한 원인은 서버 로그를 확인하세요).",
                "cluster_id": cluster_id,
            }

        # --- request-time read: idempotent skip if the named index exists ---
        try:
            existing = _index_names(client, db, collection)
        except Exception:
            logger.warning(
                "list_indexes failed for %s (db=%s, collection=%s)",
                cluster_id, db, collection, exc_info=True,
            )
            return {
                "status": "error",
                "reason": (
                    f"인덱스 목록 조회 실패 ({db}.{collection}). 적용 전 현재 상태를 "
                    "확인할 수 없어 중단합니다 (자세한 원인은 서버 로그를 확인하세요)."
                ),
                "cluster_id": cluster_id,
            }

        if name in existing:
            return {
                "status": "skipped",
                "reason": f"인덱스 {name!r}가 이미 존재합니다 (변경 없음).",
                "cluster_id": cluster_id,
                "db": db,
                "collection": collection,
                "name": name,
            }

        warnings = [
            "대용량 컬렉션의 인덱스 빌드는 IO를 소비합니다. background=True로 "
            "프라이머리를 블록하지 않지만, 빌드 동안 부하가 증가할 수 있습니다."
        ]

        if not approved:
            return {
                "status": "approval_required",
                "cluster_id": cluster_id,
                "db": db,
                "collection": collection,
                "keys": payload["keys"],
                "name": name,
                "warnings": warnings,
            }

        guard = verify_approval(
            approval_id, cluster_id, "create_docdb_index", payload=payload
        )
        if not guard.get("ok"):
            return {
                "status": "approval_denied",
                "reason": guard.get("reason", "approval guard rejected the request"),
                "cluster_id": cluster_id,
            }

        # --- TOCTOU re-read (fix #6): re-check IMMEDIATELY before the write ---
        try:
            fresh = _index_names(client, db, collection)
        except Exception:
            logger.warning(
                "pre-write list_indexes failed for %s (db=%s, collection=%s)",
                cluster_id, db, collection, exc_info=True,
            )
            return {
                "status": "error",
                "reason": (
                    "적용 직전 재조회 실패. 안전을 위해 인덱스를 만들지 않고 중단합니다 "
                    "(자세한 원인은 서버 로그를 확인하세요)."
                ),
                "cluster_id": cluster_id,
            }
        if name in fresh:
            # The index appeared between request and execute — refuse rather than
            # error on a duplicate-name create.
            return {
                "status": "approval_denied",
                "reason": "index state changed since approval",
                "cluster_id": cluster_id,
            }

        # --- the single allowlisted write: ORDERED tuples, background build ---
        try:
            client[db][collection].create_index(
                list(keys_list), background=True, name=name
            )
        except Exception:
            logger.warning(
                "create_index failed for %s (db=%s, collection=%s, name=%s)",
                cluster_id, db, collection, name, exc_info=True,
            )
            return {
                "status": "error",
                "reason": (
                    f"인덱스 생성 실패 ({db}.{collection}, 인덱스={name}). "
                    "자세한 원인은 서버 로그를 확인하세요."
                ),
                "cluster_id": cluster_id,
            }

        return {
            "status": "modified",
            "cluster_id": cluster_id,
            "db": db,
            "collection": collection,
            "keys": payload["keys"],
            "name": name,
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
