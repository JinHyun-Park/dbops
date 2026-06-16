# Cross-Account Setup

DBOps uses a Hub-Spoke IAM role pattern for cross-account Aurora cluster management.

## Architecture

```
Central Account (Hub)            Target Account (Spoke)
┌────────────────────────┐       ┌───────────────────────┐
│ Platform Lambda roles:  │       │ dbops-spoke-role      │
│  dashboard / MCP /      │       │                       │
│  ETL collector /        │──────▶│ Trust: Hub Account    │
│  cluster-API            │assume │ Read: RDS, PI, CW     │
│ (each scoped by its own │ role  │ Write: RDS Modify      │
│  sts:AssumeRole policy) │       │  (tagged resources)    │
└────────────────────────┘       └───────────────────────┘
```

Each platform Lambda role assumes the spoke role **directly**. Access requires
BOTH the spoke role's trust policy (trusts the hub account) AND the calling
role's own identity policy (`sts:AssumeRole` on this spoke role ARN) — standard
AWS hub-spoke.

## Setup Steps

### 1. Deploy Spoke Role in Target Account

```bash
aws cloudformation deploy \
  --template-file spoke-role-template.yaml \
  --stack-name dbops-spoke-role \
  --parameter-overrides \
    HubAccountId=<YOUR_HUB_ACCOUNT_ID> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <TARGET_REGION>
```

### 2. Tag Aurora Clusters for Write Access

For clusters you want DBOps to be able to modify:

```bash
aws rds add-tags-to-resource \
  --resource-name arn:aws:rds:<region>:<account>:cluster:<cluster-id> \
  --tags Key=ManagedBy,Value=dbops
```

### 3. Register Cluster in DBOps

Use the Web UI (Clusters page) or API:

```bash
curl -X POST https://<api-url>/api/clusters \
  -H "Content-Type: application/json" \
  -d '{
    "cluster_id": "<cluster-id>",
    "account_id": "<target-account-id>",
    "region": "<target-region>",
    "engine": "aurora-postgresql",
    "spoke_role_arn": "arn:aws:iam::<target-account-id>:role/dbops-spoke-role"
  }'
```

## Security Notes

- The spoke role trusts the hub **account**; access still requires the calling
  hub role's own `sts:AssumeRole` identity policy (trust + identity must both
  allow). Each platform role is scoped to exactly this spoke role ARN, so the
  account-root trust does not widen who can actually assume it.
- Write operations (ModifyDBCluster) require the `ManagedBy=dbops` tag on the cluster
- Read-only operations work on all RDS resources in the account
- SecretsManager access is limited to RDS-managed secrets
