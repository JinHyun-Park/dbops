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
    """SSL context for the reader connection. Verifies against the RDS CA bundle
    when it's present in the asset; otherwise falls back to an encrypted-but-
    unverified context.

    # ponytail: unverified fallback when global-bundle.pem isn't vendored.
    # Ceiling: MITM is NOT defended on that path — traffic is still encrypted but
    # the server cert isn't checked. Upgrade path: ensure CDK bundling downloads
    # global-bundle.pem into the operations asset (it already does for DocDB)."""
    import ssl

    ctx = ssl.create_default_context()
    if os.path.exists(_CA_BUNDLE_PATH):
        ctx.load_verify_locations(_CA_BUNDLE_PATH)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
    # ponytail self-check: SSL builder returns a context, and query() maps rows
    # against a fake connection (no pg8000, no real DB).
    import ssl

    assert isinstance(_ssl_context(), ssl.SSLContext)

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
