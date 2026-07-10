"""pg_direct — a tiny direct-PG helper for the one case RDS Data API can't serve.

The platform reaches Aurora exclusively through RDS Data API (cluster-scoped),
which CANNOT target a specific instance. `prewarm_reader` must connect to a
chosen READER instance's endpoint over TCP to warm ITS buffer pool, so it needs
a direct driver. pg8000 is a pure-Python PG driver (no C build) bundled into the
operations Lambda asset via mcp-servers/requirements.txt.

pg8000 is imported lazily inside connect() (like set_docdb_profiler lazy-imports
pymongo) so this module — and the unit tests — import fine WITHOUT pg8000
installed.
"""

import os

# RDS CA bundle vendored into the operations asset during CDK bundling
# (global-bundle.pem, same file set_docdb_profiler uses for DocDB TLS). Resolved
# relative to this file: shared/ -> mcp_servers/.
_CA_BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "global-bundle.pem",
)

# Fail fast on an unreachable instance rather than hanging the Lambda.
CONNECT_TIMEOUT_SECONDS = 10


def _ssl_context():
    """SSL context that VERIFIES the server cert against the vendored RDS CA
    bundle (create_default_context → check_hostname + CERT_REQUIRED).

    FAIL-CLOSED: if the CA bundle is missing we raise rather than downgrade to an
    unverified connection. This path carries DB master credentials to a database
    instance, so a silent fail-open (CERT_NONE) would be a security regression —
    and it matches set_docdb_profiler, which also hard-requires the CA (tlsCAFile).
    The CDK bundling vendors global-bundle.pem into the operations asset."""
    import ssl

    if not os.path.exists(_CA_BUNDLE_PATH):
        raise RuntimeError(
            "RDS CA bundle (global-bundle.pem) not found in the asset — refusing "
            "an unverified TLS connection to a database instance."
        )
    return ssl.create_default_context(cafile=_CA_BUNDLE_PATH)


def connect(host, port, database, user, password):
    """pg8000 (native) connection to a PG instance over SSL. Lazy-imports pg8000
    so the module loads without it (unit tests patch connect/query instead)."""
    import pg8000.native as native  # lazy: not importable in the test env

    return native.Connection(
        user=user,
        password=password,
        host=host,
        port=int(port),
        database=database,
        ssl_context=_ssl_context(),
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


def query(conn, sql, params=None):
    """Run `sql` (pg8000 named `:param` style) and return rows as list[dict].
    Minimal on purpose — prewarm_reader only needs a handful of small reads."""
    rows = conn.run(sql, **(params or {}))
    cols = [c["name"] for c in conn.columns]
    return [dict(zip(cols, r, strict=False)) for r in rows]


if __name__ == "__main__":
    # ponytail self-check: SSL builder VERIFIES when the CA is present and is
    # FAIL-CLOSED (raises) when it isn't; query() maps rows against a fake conn.
    import ssl

    if os.path.exists(_CA_BUNDLE_PATH):
        assert isinstance(_ssl_context(), ssl.SSLContext)
    else:
        try:
            _ssl_context()
            raise AssertionError("expected _ssl_context to raise without the CA bundle")
        except RuntimeError:
            pass

    class _FakeConn:
        columns = [{"name": "rel"}, {"name": "bytes"}]

        def run(self, sql, **params):
            return [["public.orders", 4096], ["public.users", 2048]]

    out = query(_FakeConn(), "SELECT rel, bytes FROM x")
    assert out == [
        {"rel": "public.orders", "bytes": 4096},
        {"rel": "public.users", "bytes": 2048},
    ], out
    print("pg_direct self-check OK")
