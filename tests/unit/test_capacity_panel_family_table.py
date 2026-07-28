"""The Capacity Forecast panel's per-family metric tabs must match the server.

`frontend/src/components/dashboard/capacity-forecast-panel.tsx` carries
METRICS_BY_FAMILY, a THIRD copy of the family -> metric table (the other two are
`_CAPACITY_METRICS_BY_FAMILY` in api/dashboard/handler.py and the per-logical-name
series maps in mcp-servers/mcp_servers/performance/tools/forecast_capacity.py,
which tests/unit/api/test_capacity_parity.py already asserts agree with each
other). The panel copy had no guard at all: review deleted its `rds_instance`
entry, then its `elasticache` entry, and every capacity test stayed green with
`tsc` clean. Measured again here before writing this file, with the guard removed
and the panel's `rds_instance` entry deleted: 2094 unit tests pass and
`npx tsc --noEmit` exits 0. Those are the two families E1-5 exists to add, and the
panel is the surface the operator actually looks at, so losing a tab there loses
the feature silently while every server-side test still agrees.

There is no JS runtime in CI, so the table is PARSED out of the source and
compared, the same approach as tests/unit/test_metric_filters.py (SQL predicates),
test_anomalies_panel_empty_state.py (a branch chain) and
test_responsive_display_pattern.py (Tailwind class pairs).

What this does NOT pin: tab ORDER (the panel deliberately leads with the metric a
DBA opens first per family, e.g. connections for DocumentDB, WCU for DynamoDB) and
the LABELS (the panel's are Korean-suffixed: "Read Capacity (RCU/분)").
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _ROOT / "frontend/src/components/dashboard/capacity-forecast-panel.tsx"
_PANEL = _PANEL_PATH.read_text()

_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_capacity_panel", _DASHBOARD_DIR / "handler.py")
_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dash)


def _spec_keys():
    """`const _storage: MetricSpec = { key: "storage", ... }` -> {_storage: storage}.
    The tabs reference these consts, so the const name is not the metric name and
    only the `key` field is what the server receives."""
    found = dict(re.findall(r'const (_\w+): MetricSpec = \{\s*key: "([a-z_]+)"', _PANEL))
    assert found, "no MetricSpec consts found: the panel's shape changed"
    return found


def _panel_table():
    """METRICS_BY_FAMILY as {family: [metric key, ...]}, refusing anything it
    cannot resolve so a renamed spec const fails here instead of at runtime."""
    keys = _spec_keys()
    start = _PANEL.index("const METRICS_BY_FAMILY")
    body = _PANEL[start:_PANEL.index("};", start)]
    table = {}
    for fam, consts in re.findall(r"^\s{2}(\w+): \[([^\]]*)\],", body, re.M):
        names = [c.strip() for c in consts.split(",") if c.strip()]
        unknown = [n for n in names if n not in keys]
        assert not unknown, f"{fam} references unknown MetricSpec const(s): {unknown}"
        table[fam] = [keys[n] for n in names]
    assert table, "METRICS_BY_FAMILY parsed empty: the panel's shape changed"
    return table


def test_the_panel_offers_a_tab_set_for_every_family_the_server_supports():
    """A family the server forecasts but the panel has no entry for falls back to
    the relational tabs (metricsFor()), so the operator is shown Storage /
    Connections / AAS for, say, a DynamoDB table and every tab answers
    unsupported_metric. That fallback is deliberate for an UNKNOWN engine string,
    which is exactly why a missing family key cannot announce itself."""
    assert set(_panel_table()) == set(_dash._CAPACITY_METRICS_BY_FAMILY)


def test_each_family_offers_exactly_the_metrics_the_server_accepts():
    """Order is not pinned, membership is: a tab outside the family's set can only
    ever render a refusal, and a missing tab hides a forecast the server would
    have answered."""
    panel = _panel_table()
    for fam, allowed in sorted(_dash._CAPACITY_METRICS_BY_FAMILY.items()):
        assert set(panel[fam]) == allowed, fam
        # no duplicate tabs for one metric
        assert len(panel[fam]) == len(set(panel[fam])), fam


def test_the_panel_speaks_the_same_logical_vocabulary():
    """Every panel tab sends its `key` to the endpoint as `metric`, so a name the
    server does not know comes back status=unknown_metric. This is the check that a
    raw metric_type (storage_bytes) can never creep back into the panel."""
    assert set(_spec_keys().values()) == set(_dash._CAPACITY_METRICS)
