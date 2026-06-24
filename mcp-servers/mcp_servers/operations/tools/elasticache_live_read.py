"""ElastiCache live deep-read — read-only Redis/Valkey/Memcached inspector.

Connects over the native protocol from the in-VPC operations MCP Lambda and runs
a FIXED allowlist of read-only inspector commands (Redis: INFO, SLOWLOG GET,
CLIENT LIST, MEMORY STATS; Memcached: stats). No write/admin command and no
free-form command from the caller. TLS + Secrets-Manager AUTH; cross-account
secret + describe via assumed spoke role. Mirrors the DocDB native-protocol tool
pattern (lazy client import + monkeypatchable factory)."""

import json

from mcp_servers.shared.cluster_targets import lookup_cluster, session_for

_CONNECT_TIMEOUT = 5
_SLOWLOG_COUNT = 20
_CLIENT_SAMPLE = 10
_ARG_TRUNC = 120
_REDIS_INFO_SECTIONS = ["server", "clients", "memory", "stats", "replication", "keyspace"]


def _redis_factory(host, port, password, tls):
    import redis  # lazy: not importable in the test env
    return redis.Redis(
        host=host, port=int(port), password=(password or None),
        ssl=bool(tls), ssl_cert_reqs=None,
        socket_connect_timeout=_CONNECT_TIMEOUT, socket_timeout=_CONNECT_TIMEOUT,
        decode_responses=True,
    )


def _memcached_factory(host, port):
    from pymemcache.client.base import Client  # lazy
    return Client((host, int(port)), connect_timeout=_CONNECT_TIMEOUT, timeout=_CONNECT_TIMEOUT)


# Indirection hooks so tests can inject fakes without the libs installed.
_REDIS_FACTORY = _redis_factory
_MEMCACHED_FACTORY = _memcached_factory


def _resp(status, **kw):
    return {"status": status, **kw}


def _read_auth_token(secret_arn, sess):
    raw = (sess.client("secretsmanager").get_secret_value(SecretId=secret_arn).get("SecretString") or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return d.get("auth_token") or d.get("password") or d.get("token")
        except Exception:
            return raw
    return raw


def _resolve_endpoint(ec_client, resource_name):
    try:
        rg = (ec_client.describe_replication_groups(ReplicationGroupId=resource_name)
              .get("ReplicationGroups") or [])
        if rg:
            g = rg[0]
            ce = g.get("ConfigurationEndpoint")
            if ce and ce.get("Address"):
                return ce["Address"], ce.get("Port", 6379)
            for ng in (g.get("NodeGroups") or []):
                pe = ng.get("PrimaryEndpoint")
                if pe and pe.get("Address"):
                    return pe["Address"], pe.get("Port", 6379)
    except Exception:
        pass
    try:
        cc = (ec_client.describe_cache_clusters(CacheClusterId=resource_name, ShowCacheNodeInfo=True)
              .get("CacheClusters") or [])
        if cc:
            c = cc[0]
            ce = c.get("ConfigurationEndpoint")
            if ce and ce.get("Address"):
                return ce["Address"], ce.get("Port", 11211)
            nodes = c.get("CacheNodes") or []
            if nodes and nodes[0].get("Endpoint"):
                ep = nodes[0]["Endpoint"]
                return ep.get("Address"), ep.get("Port", 6379)
    except Exception:
        pass
    return None, None


def _decode(d):
    out = {}
    for k, v in (d or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


def elasticache_live_read_impl(cache, cluster_id=None, sections=None, **_):
    if not cluster_id:
        return _resp("error", reason="cluster_id required")
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    if isinstance(rd, str):
        try:
            rd = json.loads(rd)
        except Exception:
            rd = {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    region = row.get("region", "")
    role_arn = row.get("spoke_role_arn", "")
    tls = bool(rd.get("tls_enabled"))
    secret_arn = rd.get("auth_secret_arn") or row.get("auth_secret_arn")
    resource_name = row.get("resource_name") or cluster_id

    try:
        sess = session_for(region, role_arn)
    except Exception as e:
        return _resp("error", reason=f"session 생성 실패: {str(e)[:160]}", cluster_id=cluster_id)

    host, port = _resolve_endpoint(sess.client("elasticache"), resource_name)
    if not host:
        return _resp("unavailable", reason="도달 가능한 엔드포인트를 찾지 못했습니다", cluster_id=cluster_id)

    token = None
    if secret_arn:
        try:
            token = _read_auth_token(secret_arn, sess)
        except Exception as e:
            return _resp("error", reason=f"AUTH 시크릿 조회 실패: {str(e)[:160]}", cluster_id=cluster_id)

    is_memcached = engine == "memcached"
    try:
        if is_memcached:
            client = _MEMCACHED_FACTORY(host, port)
            stats = _decode(client.stats() or {})
            return _resp("ok", engine=engine, host=host, memcached=stats, cluster_id=cluster_id)
        client = _REDIS_FACTORY(host, port, token, tls)
        want = [s for s in (sections or _REDIS_INFO_SECTIONS) if s in _REDIS_INFO_SECTIONS]
        info = {}
        # Probe with the first section un-guarded so a connection-level error
        # escapes to the outer except and returns status=error rather than ok.
        if want:
            info[want[0]] = client.info(want[0])
            for sec in want[1:]:
                try:
                    info[sec] = client.info(sec)
                except Exception:
                    pass
        else:
            for sec in want:
                try:
                    info[sec] = client.info(sec)
                except Exception:
                    pass
        slow = []
        try:
            for e in (client.slowlog_get(_SLOWLOG_COUNT) or []):
                args = e.get("command")
                if isinstance(args, (list, tuple)):
                    args = " ".join(str(a) for a in args)
                slow.append({"id": e.get("id"), "duration_us": e.get("duration"),
                             "ts": e.get("start_time"), "command": str(args)[:_ARG_TRUNC]})
        except Exception:
            pass
        clients = {}
        try:
            cl = client.client_list() or []
            clients = {"count": len(cl), "sample": cl[:_CLIENT_SAMPLE]}
        except Exception:
            pass
        mem = {}
        try:
            ms = client.memory_stats() or {}
            mem = {k: ms.get(k) for k in ("total.allocated", "peak.allocated", "keys.count", "dataset.bytes") if k in ms}
        except Exception:
            pass
        return _resp("ok", engine=engine, host=host, info=info, slowlog=slow,
                     clients=clients, memory=mem, cluster_id=cluster_id)
    except Exception as e:
        return _resp("error", reason=f"연결/조회 실패: {str(e)[:160]}", host=host, cluster_id=cluster_id)
