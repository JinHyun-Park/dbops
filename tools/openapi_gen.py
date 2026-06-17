"""Generate an OpenAPI 3.0 spec for the DBOps REST API from the CDK route table.

The route table in ``cdk/stacks/agent_stack.py`` (``self.api.add_routes(...)``)
is the single source of truth. Hand-maintaining a parallel OpenAPI doc drifts;
instead we parse the add_routes calls and emit the spec. A unit test
(``tests/unit/test_openapi_spec.py``) asserts the committed
``frontend/public/openapi.json`` matches what this produces, so a new route
fails CI until the spec is regenerated.

Run directly to (re)write the committed spec:
    python tools/openapi_gen.py
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_STACK = _REPO / "cdk" / "stacks" / "agent_stack.py"
_OUT = _REPO / "frontend" / "public" / "openapi.json"

# Public routes (no Cognito JWT) authenticate differently — Slack via HMAC,
# /health is an open uptime probe. Everything else requires a Bearer token.
_PUBLIC_MARKER = "public_authorizer"


def _iter_add_routes(src: str):
    """Yield (block_text, path, methods[]) for each self.api.add_routes(...)."""
    for m in re.finditer(r"\.add_routes\(", src):
        start = m.end()
        depth = 1
        i = start
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        block = src[start : i - 1]
        pm = re.search(r'path\s*=\s*"([^"]+)"', block)
        if not pm:
            continue
        methods = re.findall(r"HttpMethod\.(\w+)", block)
        yield block, pm.group(1), methods


def _tag_for(path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    # parts[0] == "api"; the resource is the next segment.
    return parts[1] if len(parts) > 1 else "api"


def build_spec() -> dict:
    src = _STACK.read_text()
    paths: dict = {}
    tags: set = set()
    for block, path, methods in _iter_add_routes(src):
        is_public = _PUBLIC_MARKER in block
        tag = _tag_for(path)
        tags.add(tag)
        params = [
            {
                "name": seg[1:-1],
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
            for seg in path.split("/")
            if seg.startswith("{") and seg.endswith("}")
        ]
        item = paths.setdefault(path, {})
        for method in methods:
            op: dict = {
                "tags": [tag],
                "summary": f"{method} {path}",
                "responses": {
                    "200": {"description": "OK"},
                    "401": {"description": "Unauthorized (missing/invalid token)"},
                },
            }
            if params:
                op["parameters"] = params
            if not is_public:
                op["security"] = [{"cognitoJwt": []}]
            else:
                op["responses"].pop("401", None)
            item[method.lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "DBOps REST API",
            "version": "1.0.0",
            "description": (
                "Dashboard / data REST surface for the DBOps platform. All "
                "routes require a Cognito JWT (Authorization: Bearer <access "
                "token>) except the Slack webhooks (HMAC-signed) and /health."
            ),
        },
        "components": {
            "securitySchemes": {
                "cognitoJwt": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            }
        },
        "tags": [{"name": t} for t in sorted(tags)],
        "paths": dict(sorted(paths.items())),
    }


if __name__ == "__main__":
    spec = build_spec()
    _OUT.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
    n_ops = sum(len(v) for v in spec["paths"].values())
    print(f"wrote {_OUT.relative_to(_REPO)}: {len(spec['paths'])} paths, {n_ops} operations")
