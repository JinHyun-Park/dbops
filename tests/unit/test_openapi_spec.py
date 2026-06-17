"""OpenAPI spec ↔ route table parity.

The committed frontend/public/openapi.json must equal what tools/openapi_gen.py
derives from the CDK route table. Add a route without regenerating the spec
(`python tools/openapi_gen.py`) and this fails — no silent drift between the
live API surface and its published docs.
"""
import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_GEN = _REPO / "tools" / "openapi_gen.py"
_SPEC = _REPO / "frontend" / "public" / "openapi.json"

_spec = importlib.util.spec_from_file_location("openapi_gen", _GEN)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_committed_openapi_matches_route_table():
    generated = _mod.build_spec()
    committed = json.loads(_SPEC.read_text())
    assert generated == committed, (
        "frontend/public/openapi.json is stale vs the CDK route table — "
        "run `python tools/openapi_gen.py` and commit the result."
    )


def test_openapi_surface_and_auth():
    spec = _mod.build_spec()
    assert len(spec["paths"]) >= 50, "route parsing looks broken"
    # Public routes carry no security; protected routes require the JWT scheme.
    assert "security" not in spec["paths"]["/api/health"]["get"]
    assert spec["paths"]["/api/dashboard/{cluster_id}"]["get"]["security"] == [
        {"cognitoJwt": []}
    ]
    # Path params are surfaced.
    dash = spec["paths"]["/api/dashboard/{cluster_id}"]["get"]
    assert any(p["name"] == "cluster_id" and p["in"] == "path" for p in dash["parameters"])
