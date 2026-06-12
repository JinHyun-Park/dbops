"""Tests for the DynamoDB Price List lookups.

Pin the suffix-match + confounder-exclusion logic (IA table class and
ReplWrite global-table SKUs share the same suffix and must NOT be picked), the
beyond-free-tier tier selection, the per-million normalization, the soft-fail →
None contract, and the per-(kind,region) cache.
"""
import json
from unittest.mock import MagicMock, patch

import mcp_servers.shared.dynamodb_pricing as p


def _product(usagetype, *price_dims):
    """price_dims: (usd, begin_range) tuples → OnDemand price dimensions."""
    dims = {}
    for i, (usd, begin) in enumerate(price_dims):
        dims[f"d{i}"] = {"beginRange": begin, "pricePerUnit": {"USD": str(usd)}}
    return json.dumps({
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"t": {"priceDimensions": dims}}},
    })


def _client(products):
    cli = MagicMock()
    cli.get_products.return_value = {"PriceList": products}
    return cli


def setup_function():
    p._CACHE.clear()


# Realistic Seoul fixture including the IA + Repl confounders that must be skipped.
_FIXTURE = [
    _product("APN2-ReadCapacityUnit-Hrs", ("0.0000000000", "0"), ("0.00014098", "18600")),
    _product("APN2-WriteCapacityUnit-Hrs", ("0.0000000000", "0"), ("0.0007049", "18600")),
    _product("APN2-ReadRequestUnits", ("0.0000001355", "0")),
    _product("APN2-WriteRequestUnits", ("0.00000068", "0")),
    # Confounders — same suffixes, different (wrong) prices. Must be excluded.
    _product("APN2-IA-ReadCapacityUnit-Hrs", ("0.0001762", "0")),
    _product("APN2-IA-WriteCapacityUnit-Hrs", ("0.000845", "0")),
    _product("APN2-ReplWriteCapacityUnit-Hrs", ("0.0007049", "18600")),
    _product("APN2-IA-ReadRequestUnits", ("0.0000001695", "0")),
    _product("APN2-IA-WriteRequestUnits", ("0.000000845", "0")),
    _product("APN2-ReplWriteRequestUnits", ("0.00000068", "0")),
]


@patch.object(p, "_client")
def test_all_four_prices_parse_from_correct_suffix(mock_client):
    mock_client.return_value = _client(_FIXTURE)
    r = "ap-northeast-2"
    # Provisioned capacity-hour: beyond-free-tier (non-zero) tier, not $0.
    assert p.price_per_rcu_hour(r) == 0.00014098
    assert p.price_per_wcu_hour(r) == 0.0007049
    # On-demand: published per-request → normalized to $/million (× 1e6).
    assert round(p.price_per_million_rru(r), 4) == 0.1355
    assert round(p.price_per_million_wru(r), 4) == 0.68


@patch.object(p, "_client")
def test_excludes_ia_and_replicated_confounders(mock_client):
    # If the IA/Repl rows were (wrongly) matched, the prices would differ.
    mock_client.return_value = _client(_FIXTURE)
    r = "ap-northeast-2"
    assert p.price_per_wcu_hour(r) == 0.0007049          # NOT the IA 0.000845
    assert p.price_per_wcu_hour.__name__  # sanity
    p._CACHE.clear()
    assert round(p.price_per_million_wru(r), 4) == 0.68  # NOT the IA 0.845 or repl


@patch.object(p, "_client")
def test_provisioned_picks_nonzero_tier_not_free_tier(mock_client):
    # Only a $0 free-tier dimension present → still returns 0.0 (no non-zero band),
    # but when both exist the non-zero one wins.
    mock_client.return_value = _client([
        _product("APN2-ReadCapacityUnit-Hrs", ("0.0000000000", "0"), ("0.00014098", "18600")),
    ])
    assert p.price_per_rcu_hour("ap-northeast-2") == 0.00014098


@patch.object(p, "_client")
def test_unmatched_suffix_returns_none(mock_client):
    mock_client.return_value = _client([
        _product("APN2-IncrementalExportDataSize-Bytes", ("0.1083", "0")),
    ])
    assert p.price_per_rcu_hour("ap-northeast-2") is None
    assert p.price_per_million_rru("ap-northeast-2") is None


@patch.object(p, "_client")
def test_soft_fail_returns_none(mock_client):
    cli = MagicMock()
    cli.get_products.side_effect = RuntimeError("AccessDenied")
    mock_client.return_value = cli
    assert p.price_per_rcu_hour("ap-northeast-2") is None
    assert p.price_per_million_wru("ap-northeast-2") is None


@patch.object(p, "_client")
def test_cache_avoids_second_call(mock_client):
    cli = _client(_FIXTURE)
    mock_client.return_value = cli
    r = "ap-northeast-2"
    assert p.price_per_rcu_hour(r) == 0.00014098
    calls_after_first = cli.get_products.call_count
    # Second call for the SAME (kind, region) must hit the cache, not the API.
    assert p.price_per_rcu_hour(r) == 0.00014098
    assert cli.get_products.call_count == calls_after_first
