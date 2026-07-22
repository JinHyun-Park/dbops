"""mssql_direct — pytds connect (enforced TLS) + Data-API-shape adapter for RDS SQL Server.

RDS for SQL Server has no Data API, so execute_sql's ad-hoc chat SQL against
RDS SQL Server instances needs a direct TCP connection re-encoded into the
same response shape RDS Data API returns. Mirrors mysql_direct.py.

TLS we enforce: RDS SQL Server does NOT force SSL by default (rds.force_ssl
is off), unlike RDS MySQL/PostgreSQL. So this module always passes `cafile`
(the vendored CA bundle) with `validate_host=True` to get verified TLS
regardless of the instance's parameter group — never `enc_login_only=True`,
which would only encrypt the login packet and leave the rest of the session
in plaintext.
"""

import os
from datetime import date, datetime
from decimal import Decimal

# RDS CA bundle vendored into the operations asset during CDK bundling
# (global-bundle.pem, same file pg_direct/mysql_direct/set_docdb_profiler
# use). Resolved relative to this file: shared/ -> mcp_servers/.
_CA_BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "global-bundle.pem",
)


def connect(host, port, database, user, password):
    """pytds connection to an RDS SQL Server instance over verified TLS.

    FAIL-CLOSED: the CA bundle check runs BEFORE the pytds import, so a
    missing CA raises RuntimeError even where pytds isn't installed (tests),
    and this path never falls back to an unverified connection — it carries
    DB credentials to a database instance (matches pg_direct/mysql_direct's
    contract)."""
    if not os.path.exists(_CA_BUNDLE_PATH):
        raise RuntimeError(
            "RDS CA bundle (global-bundle.pem) not found in the asset — refusing "
            "an unverified TLS connection to a database instance."
        )
    import pytds  # lazy: not importable in the test env

    return pytds.connect(
        server=host,
        port=int(port),
        database=database or None,
        user=user,
        password=password,
        cafile=_CA_BUNDLE_PATH,
        validate_host=True,
        login_timeout=10,
        timeout=30,
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


class MSSQLDataApiAdapter:
    """Duck-types the subset of the rds-data client execute_sql uses."""

    def __init__(self, conn):
        self._conn = conn

    def execute_statement(self, resourceArn=None, secretArn=None, database=None,
                           sql=None, parameters=None, includeResultMetadata=None,
                           **kwargs):
        # execute_sql passes literal SQL only (parameters are inlined by the
        # caller); assert loudly if that assumption ever breaks.
        if parameters:
            raise ValueError("MSSQLDataApiAdapter does not support parameters")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is not None:
                rows = cur.fetchall()
                return {
                    "records": [[_field(v) for v in row] for row in rows],
                    "columnMetadata": [{"name": d[0]} for d in cur.description],
                }
            return {"records": [], "columnMetadata": [], "numberOfRecordsUpdated": cur.rowcount}
