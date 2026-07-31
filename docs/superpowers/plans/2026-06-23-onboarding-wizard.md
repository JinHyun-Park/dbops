# Onboarding Wizard (Spoke-Account Setup) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A guided spoke-account onboarding wizard — generate the spoke-role CloudFormation template (hub-account trust + curated least-privilege perms, read-only default + write-remediation toggle), verify via the existing test-connection, hand off to existing discover/register.

**Architecture:** One new admin-gated endpoint (`GET /api/onboarding/template`) that emits a JSON CloudFormation template; a wizard UI that stitches it with the existing `/api/clusters/test-connection` and `/clusters` discover/register. No new ping endpoint, no new cross-account plumbing.

**Tech Stack:** Python 3.12 Lambda, API Gateway HTTP API, Next.js 16 (static export).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **OpenAPI parity:** the new route requires `python tools/openapi_gen.py` regen; `tests/unit/test_openapi_spec.py` enforces it.
- **CDK-only infra:** all AWS via CDK.
- **Admin gate server-side + fail-closed:** copy the hardened `api/config/handler.py` `_is_admin` (no `Bearer ` → False; empty/garbage claims → False; viewer → False).
- **Reuse, don't rebuild:** the connection check is the EXISTING `POST /api/clusters/test-connection`; discovery/registration is the EXISTING `/api/clusters/discover` + `/bulk-register`. This feature adds ONLY the template endpoint + the wizard UI.
- **Trust = hub account root:** the generated spoke role trusts `arn:aws:iam::<HUB_ACCOUNT_ID>:root` (matches the direct-assume `_session_for`); role name fixed `dbops-spoke-role`. ExternalId is a documented follow-up (would require changing the shared `_session_for`).
- **Read-only by default:** the template's permission policy is read-only unless `remediation=true`.
- **CloudFormation as JSON** (CFN accepts JSON natively) — `json.dumps` of a template dict; NO PyYAML dependency.
- **Korean UI copy** for explanatory/step text; keep AWS identifiers/ARNs as-is.

---

### Task 1: Template-generation API — `GET /api/onboarding/template`

**Files:**

- Create: `api/onboarding/handler.py`, `api/onboarding/__init__.py` (empty)
- Modify: `cdk/stacks/agent_stack.py` (new `OnboardingApi` Lambda + route + `HUB_ROLE_ARN` env + `sts:GetCallerIdentity` perm)
- Modify: `frontend/public/openapi.json` (regenerated)
- Test: `tests/unit/api/test_onboarding.py`

**Interfaces:**

- Produces: `GET /api/onboarding/template?region=<r>&remediation=<bool>` → `{"template": "<json>", "hub_account_id", "hub_role_arn", "role_name": "dbops-spoke-role", "remediation": <bool>, "region": <r|null>}`. Admin-gated.

- [ ] **Step 1: Write the handler.** Create `api/onboarding/handler.py`:

```python
"""Onboarding API — generates the spoke-account IAM role CloudFormation template
(JSON) a member-account admin deploys so DBOps's hub account can assume into it.
Admin-only, fail-closed (mirrors api/config/handler.py)."""

import base64
import json
import os

import boto3

ROLE_NAME = "dbops-spoke-role"

# Curated cross-account READ actions DBOps uses after assuming the spoke role.
READ_ACTIONS = [
    "rds:Describe*",
    "rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
    "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
    "pi:GetResourceMetrics", "pi:DescribeDimensionKeys", "pi:ListAvailableResourceMetrics",
    "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups",
    "dynamodb:ListTables", "dynamodb:DescribeTable",
    "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive",
]
# Approval-gated WRITE actions (remediation) the operations MCP uses cross-account.
WRITE_ACTIONS = [
    "rds:ModifyDBCluster", "rds:ModifyDBInstance",
    "rds:ModifyDBParameterGroup", "rds:ModifyDBClusterParameterGroup",
    "rds:CreateDBClusterSnapshot", "rds:CreateDBSnapshot",
    "rds:RebootDBInstance", "rds:ApplyPendingMaintenanceAction",
    "dynamodb:UpdateTable", "dynamodb:UpdateContinuousBackups", "dynamodb:UpdateTimeToLive",
]
# secretsmanager is resource-scoped (dbops/* only), kept as its own statement.
SECRETS_ACTIONS = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    return True


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _build_template(hub_account_id: str, remediation: bool) -> dict:
    statements = [
        {"Sid": "DBOpsRead", "Effect": "Allow", "Action": list(READ_ACTIONS), "Resource": "*"},
        {"Sid": "DBOpsSecrets", "Effect": "Allow", "Action": list(SECRETS_ACTIONS),
         "Resource": "arn:aws:secretsmanager:*:*:secret:dbops/*"},
    ]
    if remediation:
        statements.append({"Sid": "DBOpsRemediation", "Effect": "Allow",
                           "Action": list(WRITE_ACTIONS), "Resource": "*"})
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "DBOps spoke-account role — lets the DBOps hub account assume in for "
                       "read-only monitoring/analysis" + (" + approval-gated remediation" if remediation else ""),
        "Resources": {
            "DBOpsSpokeRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": ROLE_NAME,
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"AWS": f"arn:aws:iam::{hub_account_id}:root"},
                            "Action": "sts:AssumeRole",
                        }],
                    },
                    "Policies": [{
                        "PolicyName": "dbops-spoke-access",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }],
                },
            }
        },
        "Outputs": {
            "RoleArn": {"Description": "Spoke role ARN — register this in DBOps",
                        "Value": {"Fn::GetAtt": ["DBOpsSpokeRole", "Arn"]}},
        },
    }


def lambda_handler(event, context=None):
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method == "OPTIONS":
        return _resp(200, {})
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})

    qs = event.get("queryStringParameters") or {}
    region = (qs.get("region") or "").strip() or None
    remediation = str(qs.get("remediation") or "").strip().lower() in ("true", "1", "yes", "on")

    try:
        hub_account_id = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as e:
        return _resp(500, {"error": f"could not resolve hub account: {type(e).__name__}"})

    template = _build_template(hub_account_id, remediation)
    return _resp(200, {
        "template": json.dumps(template, indent=2),
        "hub_account_id": hub_account_id,
        "hub_role_arn": os.environ.get("HUB_ROLE_ARN", ""),
        "role_name": ROLE_NAME,
        "remediation": remediation,
        "region": region,
    })
```

(The implementer may refine `READ_ACTIONS`/`WRITE_ACTIONS` against the actual cross-account call sites in the MCP servers/collectors — the lists above are the curated baseline; keep `Resource:"*"` for the describe/metric actions which don't support resource scoping, and the secrets statement scoped to `dbops/*`.)

- [ ] **Step 2: Empty package marker.** Create `api/onboarding/__init__.py` (empty).

- [ ] **Step 3: Write the tests.** Create `tests/unit/api/test_onboarding.py` (mirror `tests/unit/api/test_config.py` harness — importlib-load, `_jwt`/`_event` helpers, patch `boto3`/`get_caller_identity`):

```python
# load handler via importlib; _jwt(admin=...) builds a Bearer token.
def test_template_read_only_default(monkeypatch):
    # mock boto3.client("sts").get_caller_identity -> {"Account": "111122223333"}
    # GET with admin token, no remediation -> 200; parse body["template"] (json);
    # assert RoleName == "dbops-spoke-role";
    # assert the trust Principal.AWS == "arn:aws:iam::111122223333:root";
    # assert a read action present (e.g. "rds:Describe*") and NO write action
    #   (e.g. "rds:ModifyDBCluster" absent);
    # assert body["remediation"] is False and body["hub_account_id"] == "111122223333".

def test_template_remediation_adds_write(monkeypatch):
    # GET ?remediation=true -> the template's statements include "rds:ModifyDBCluster".

def test_viewer_denied():
    # GET with a dbops-viewer token -> 403.

def test_no_bearer_denied():
    # GET with a raw (no "Bearer ") token -> 403.
```

Write these with REAL assertions (parse the JSON template + assert the trust principal + action membership). Patch `handler.boto3` so `get_caller_identity` returns a fixed account.

Run: `python -m pytest tests/unit/api/test_onboarding.py -q` → PASS.

- [ ] **Step 4: Add the CDK Lambda + route.** In `cdk/stacks/agent_stack.py`, near the other API lambdas, add:

```python
        onboarding_lambda = lambda_.Function(
            self, "OnboardingApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/onboarding"),
            timeout=cdk.Duration.seconds(15),
            environment={"HUB_ROLE_ARN": foundation.hub_role.role_arn},
        )
        onboarding_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:GetCallerIdentity"], resources=["*"],
        ))
```

Then near the other routes:

```python
        self.api.add_routes(
            path="/api/onboarding/template",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("OnboardingIntegration", onboarding_lambda),
        )
```

- [ ] **Step 5: Regenerate OpenAPI.** `python tools/openapi_gen.py`.

- [ ] **Step 6: Run parity + synth.** `python -m pytest tests/unit/api/test_onboarding.py tests/unit/test_openapi_spec.py tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add api/onboarding cdk/stacks/agent_stack.py frontend/public/openapi.json tests/unit/api/test_onboarding.py
git commit -m "feat(onboarding): spoke-role CloudFormation template API (read-only + remediation toggle)"
```

---

### Task 2: Wizard UI — `/onboarding` page + api-client + nav

**Files:**

- Modify: `frontend/src/lib/api-client.ts` (`fetchOnboardingTemplate` + reuse/confirm the cluster test-connection client fn)
- Create: `frontend/src/app/onboarding/page.tsx`
- Modify: `frontend/src/components/app-shell.tsx` (nav entry, `adminOnly: true`)
- Modify: `frontend/src/components/design-system/command-palette.tsx` (entry, `adminOnly: true`)

**Interfaces:**

- Consumes: `GET /api/onboarding/template` (Task 1); the existing `POST /api/clusters/test-connection`; the existing `/clusters` page.

- [ ] **Step 1: api-client.** In `frontend/src/lib/api-client.ts`, add:

```typescript
export interface OnboardingTemplate {
  template: string;
  hub_account_id: string;
  hub_role_arn: string;
  role_name: string;
  remediation: boolean;
  region: string | null;
}

export async function fetchOnboardingTemplate(opts?: {
  region?: string;
  remediation?: boolean;
}): Promise<OnboardingTemplate> {
  const p = new URLSearchParams();
  if (opts?.region) p.set("region", opts.region);
  if (opts?.remediation) p.set("remediation", "true");
  const qs = p.toString();
  const res = await authedFetch(
    await apiUrl(`/api/onboarding/template${qs ? `?${qs}` : ""}`),
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok)
    throw new Error(`onboarding template fetch failed: ${res.status}`);
  return res.json();
}
```

Find the existing cluster test-connection client fn (search `test-connection` in api-client.ts); if none exists, add `testClusterConnection({account_id, region, role_arn})` that POSTs `/api/clusters/test-connection` (match the handler's request/response shape — read `api/clusters/handler.py`'s test-connection block for the exact body keys + response).

- [ ] **Step 2: Build the wizard page.** Create `frontend/src/app/onboarding/page.tsx`. Mirror `frontend/src/app/approval-policies/page.tsx` (read it first) for the admin-page shell + `"admin only"` → notice + load/error. A 3-step flow (numbered sections, all visible — not a hard stepper):

  - **Step 1 — 스포크 역할 생성:** on mount, `fetchOnboardingTemplate({})`; show `hub_account_id` + `hub_role_arn`; a read-only ↔ remediation toggle that re-fetches with `remediation: true`; the `template` JSON in a `<pre>` with a copy button + a download-as-`dbops-spoke-role.json` button; Korean instructions to deploy it as a CloudFormation stack in the member account (`aws cloudformation deploy --template-file dbops-spoke-role.json --stack-name dbops-spoke-role --capabilities CAPABILITY_NAMED_IAM`).
  - **Step 2 — 연결 확인:** `account_id` + `region` inputs → a "테스트" button → `testClusterConnection({account_id, region, role_arn: \`arn:aws:iam::${account_id}:role/dbops-spoke-role\`})` → green success (show the result) or red diagnostic (the handler's error).
  - **Step 3 — 클러스터 등록:** a CTA/link to `/clusters` (the existing discover/register UI). Korean copy explaining discovery happens there.
  - Reuse design-system primitives; match the approval-policies/settings visual language; null-safe; surface backend error messages.

- [ ] **Step 3: Nav + command-palette.** In `app-shell.tsx`, add to the "Configure" NAV group an entry `{ href: "/onboarding", label: "Onboarding", icon: Rocket, adminOnly: true, hint: "멤버 계정 연결 위저드 (관리자)" }` (import a lucide icon — `Rocket`/`PlugZap`/`Workflow`; if taken, pick an available one). In `command-palette.tsx`, add `{ id: "onboarding", label: "Onboarding — 멤버 계정 연결 위저드", path: "/onboarding", group: "Configure", adminOnly: true }`.

- [ ] **Step 4: Build.** `cd frontend && npm run build` → PASS, no type errors.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/lib/api-client.ts frontend/src/app/onboarding/ frontend/src/components/app-shell.tsx frontend/src/components/design-system/command-palette.tsx
git commit -m "feat(onboarding): spoke-account setup wizard UI (hidden from viewers)"
```

---

## Post-implementation (controller, after both tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD`.
- Deploy dev: `cdk deploy dbops-dev-agent` (onboarding Lambda + route). Frontend build → `aws s3 sync frontend/out/ ... --delete --exclude config.json` → CloudFront invalidation `E1234567890ABC`.
- Live smoke (viewer e2e token): `GET /api/onboarding/template` → 403 (admin-gated); (the admin path returns a valid template — unit-covered since the viewer token can't reach it). Confirm the route exists (not 404).
- Then `superpowers:finishing-a-development-branch`.
