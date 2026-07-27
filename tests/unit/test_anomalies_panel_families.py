"""The Anomalies panel must render for every engine family, with neutral copy.

`GET /api/dashboard/{id}/anomalies` is not family-gated, and the ETL now trains
`metric_baselines` for documentdb / dynamodb / elasticache too, so a
relational-only render gate on the advisory tab was the only reason those
operators saw no seasonal anomalies at all.

Regex-based on purpose, same as test_health_score_signals.py: no JS runtime in
CI, and both sides are flat literals.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = (_ROOT / "frontend/src/app/dashboard/page.tsx").read_text()
_PANEL = (
    _ROOT / "frontend/src/components/dashboard/anomalies-panel.tsx"
).read_text()
_HEALTH = (_ROOT / "frontend/src/components/dashboard/health-score.tsx").read_text()
_GLOSSARY = (_ROOT / "frontend/src/lib/metric-glossary.ts").read_text()


def _flat(s: str) -> str:
    """Whitespace-normalized, so Prettier reflowing JSX cannot break a match."""
    return " ".join(s.split())


def _advisory_block() -> str:
    """The advisory tab's JSX, from its banner comment to the next tab's."""
    start = _PAGE.index('{activeTab === "advisory"')
    end = _PAGE.index('{activeTab === "internals"', start)
    return _flat(_PAGE[start:end])


def test_anomalies_panel_is_not_gated_to_relational_families():
    block = _advisory_block()
    assert "<AnomaliesPanel" in block, "advisory tab lost the Anomalies panel"
    # relational keeps its own block (ordered above CapacityForecastPanel).
    assert '{fam === "relational" && ( <> <AnomaliesPanel' in block
    # ...and every other family gets it from one negated gate. engineFamily()
    # has no unknown bucket (unrecognized engines fall back to relational), so
    # this is exactly documentdb + dynamodb + elasticache + rds_instance.
    assert '{fam !== "relational" && ( <AnomaliesPanel' in block, (
        "documentdb / dynamodb / elasticache render no Anomalies panel: their "
        "seasonal baselines are trained but invisible in the UI"
    )
    # The bug shape: AnomaliesPanel behind a single-family equality gate.
    for fam in ("rds_instance", "dynamodb", "documentdb", "elasticache"):
        assert f'{{fam === "{fam}" && ( <AnomaliesPanel' not in block, fam


def _panel_label_keys() -> set:
    block = re.search(
        r"const METRIC_LABELS: Record<string, string> = \{(.*?)^\};",
        _PANEL,
        re.S | re.M,
    )
    assert block, "METRIC_LABELS missing from anomalies-panel.tsx"
    return set(re.findall(r"^\s*([a-z_0-9]+):", block.group(1), re.M))


def test_every_family_metric_resolves_to_a_human_label():
    """prettyMetric falls back to the shared glossary, which already labels the
    non-relational metric_types. Without the fallback a DynamoDB operator reads
    'read_throttle_events' and a Redis one reads 'engine_cpu'."""
    assert "metricDef(k)?.label" in _flat(_PANEL), (
        "prettyMetric must fall back to METRIC_GLOSSARY: METRIC_LABELS only "
        "covers relational metric_types"
    )
    labels = _panel_label_keys() | set(
        re.findall(r"^  ([a-z_0-9]+): \{", _GLOSSARY, re.M)
    )
    for name in (
        "SIGNALS_DOCUMENTDB",
        "SIGNALS_DYNAMODB",
        "SIGNALS_ELASTICACHE_REDIS",
        "SIGNALS_ELASTICACHE_MEMCACHED",
        "SIGNALS_RDS_INSTANCE",
        "SIGNALS_RELATIONAL",
    ):
        body = re.search(
            rf"^const {name}: SignalDef\[\] = \[(.*?)^\];", _HEALTH, re.S | re.M
        )
        assert body, name
        metrics = set(re.findall(r'metric:\s*"([^"]+)"', body.group(1)))
        assert metrics, name
        assert metrics <= labels, f"{name}: unlabeled in Anomalies: {metrics - labels}"


def test_ai_diagnosis_prompt_names_no_relational_only_mechanism():
    """The detail modal's prompt reaches the agent for all five families."""
    for jargon in ("플래너", "락 스톰", "폭주 쿼리"):
        assert jargon not in _PANEL, (
            f"'{jargon}' is a relational-only mechanism and this prompt is now "
            "sent for DocumentDB / DynamoDB / ElastiCache clusters too"
        )
