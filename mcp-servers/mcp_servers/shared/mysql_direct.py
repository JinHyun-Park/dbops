"""mysql_direct — pymysql connect + Data-API-shape adapter for RDS MySQL.

RDS for MySQL has no Data API, so execute_sql's ad-hoc chat SQL against RDS
MySQL clusters needs a direct TCP connection re-encoded into the same
response shape RDS Data API returns.

data-pipeline/rds_direct_collector/mysql_adapter.py is a sibling adapter that
already does this for the vendored deep-read collectors, which unwrap rows by
POSITION and never look at columnMetadata — so that copy stays minimal by
design. execute_sql decodes RDS Data API responses by COLUMN NAME
(resp["columnMetadata"]), so THIS adapter additionally synthesizes
columnMetadata from cursor.description and surfaces write-statement rowcount
as numberOfRecordsUpdated (the Data API field for non-SELECT statements).
Without columnMetadata, chat output would degrade to positional col_0/col_1
names.
"""

import os
from datetime import date, datetime
from decimal import Decimal

# RDS CA bundle vendored into the operations asset during CDK bundling
# (global-bundle.pem, same file pg_direct/set_docdb_profiler use). Resolved
# relative to this file: shared/ -> mcp_servers/.
_CA_BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "global-bundle.pem",
)


def connect(host, port, database, user, password):
    """pymysql connection to an RDS MySQL instance over verified TLS.

    FAIL-CLOSED: the CA bundle check runs BEFORE the pymysql import, so a
    missing CA raises RuntimeError even where pymysql isn't installed (tests),
    and this path never falls back to an unverified connection — it carries
    DB credentials to a database instance (matches pg_direct's contract)."""
    if not os.path.exists(_CA_BUNDLE_PATH):
        raise RuntimeError(
            "RDS CA bundle (global-bundle.pem) not found in the asset — refusing "
            "an unverified TLS connection to a database instance."
        )
    import pymysql  # lazy: not importable in the test env

    return pymysql.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        ssl_ca=_CA_BUNDLE_PATH,
        ssl_verify_cert=True,
        ssl_verify_identity=True,
        connect_timeout=8,
        read_timeout=30,
        autocommit=True,
    )


def _field(v):
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, Decimal):
        return {"doubleValue": float(v)}
    if isinstance(v, bytes):
        return {"stringValue": v.decode("utf-8", errors="replace")}
    if isinstance(v, (datetime, date)):
        return {"stringValue": str(v)}
    return {"stringValue": str(v)}


class MySQLDataApiAdapter:
    """Duck-types the subset of the rds-data client execute_sql uses."""

    def __init__(self, conn):
        self._conn = conn

    def execute_statement(self, resourceArn=None, secretArn=None, database=None,
                           sql=None, parameters=None, includeResultMetadata=None,
                           **kwargs):
        # execute_sql passes literal SQL only (parameters are inlined by the
        # caller); assert loudly if that assumption ever breaks.
        if parameters:
            raise ValueError("MySQLDataApiAdapter does not support parameters")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is not None:
                rows = cur.fetchall()
                return {
                    "records": [[_field(v) for v in row] for row in rows],
                    "columnMetadata": [{"name": d[0]} for d in cur.description],
                }
            return {"records": [], "columnMetadata": [], "numberOfRecordsUpdated": cur.rowcount}
