# Phase 1 Performance Analysis Agent: AS-BUILT DEPLOY CHECKLIST

> # THE BUILD IS DONE. THESE ARE DEPLOY STEPS, NOT BUILD STEPS.
>
> Phase 1 was built starting 2026-05-08 and has been live and deployed since.
> Verified as shipped on 2026-07-27. The original 15-step implementation plan
> that used to live in this file was deleted on purpose: it told an agent to run
> `cdk init` inside an already populated `cdk/`, to create a Bedrock Knowledge
> Base that no stack has, to implement an `explain_query` tool that does not
> exist (it is `explain_plan`), to configure Cedar as READ-ONLY (it is LOG_ONLY,
> and writes ship), and to scaffold Next.js 15 (`frontend/package.json` pins
> 16.2.9). Executing it would have damaged a working codebase.
>
> **Do not scaffold, re-initialize, or re-implement anything in this repository
> from this spec.** What follows is the only task list a newcomer needs: how to
> deploy the existing system into a fresh AWS account. The boxes are unchecked
> because a newcomer genuinely has not done them yet, not because code is
> missing.
>
> If you were asked to change behavior rather than deploy, start from
> `README.md`, `AGENTS.md`, and `.kiro/steering/`, then the newest matching spec
> in `docs/superpowers/specs/`.

## Deploy checklist

Prerequisites (from `README.md`): an AWS account with AdministratorAccess,
Node.js 20+, Python 3.10+, `npm install -g aws-cdk`, and Bedrock model access
enabled for Claude Sonnet in your region.

- [ ] 1. Configure the deployment

  - `cp cdk/config/settings.example.py cdk/config/settings.py`
  - Set `ACCOUNT_ID` and `REGION`. Everything else in that file has a working
    default, including `COGNITO_DOMAIN_PREFIX`, which must stay empty unless you
    want a specific prefix (the stack derives a globally unique one).
  - Check `AGENT_MODEL_ID` carries the inference-profile prefix for your region
    (`apac.`, `us.`, `eu.`, or `global.`). A bare model ID fails every chat turn.
  - `cdk/config/settings.py` is gitignored. It is your real config, never commit
    or overwrite it.

- [ ] 2. Bootstrap CDK once per account and region

  - `cd cdk && cdk bootstrap && cd ..`

- [ ] 3. Run the deployer from the repo root

  - `./deploy.sh`
  - It bundles the SQL schemas, builds the frontend (`npm install` plus
    `npm run build`, required because the frontend stack ships a prebuilt
    `out/`), builds the ARM64 agent dependencies via `agent/build-deps.sh`,
    installs `cdk/requirements.txt`, then runs `cdk deploy --all` over the four
    stacks in dependency order: `dbops-{ENV}-foundation`, `-data`, `-agent`,
    `-frontend`.
  - Schema migrations are automatic. The SchemaMigrator custom resource in the
    data stack creates the tables idempotently. Do not run SQL by hand.
  - Never run two `cdk deploy` processes at once. They collide in `cdk.out` and
    one fails quietly.
  - The final output prints your Web UI URL, API URL, Runtime ARN, and Gateway
    ID, then runs `smoke-test.sh`. Some smoke checks are expected to fail before
    the first cluster is registered.

- [ ] 4. Create the first login

  - Cognito self-signup is disabled (`cdk/stacks/foundation_stack.py`), so the
    first user must be created with the AWS CLI. Get the pool ID from the
    foundation stack's `UserPoolId` output:

    ```bash
    aws cognito-idp admin-create-user \
      --user-pool-id <UserPoolId> \
      --username <you@example.com> \
      --user-attributes Name=email,Value=<you@example.com> Name=email_verified,Value=true
    aws cognito-idp admin-set-user-password \
      --user-pool-id <UserPoolId> \
      --username <you@example.com> \
      --password '<password>' --permanent
    ```

  - Password policy: at least 8 characters with lowercase, uppercase, digit, and
    symbol.
  - No group assignment is needed for an admin. The RBAC model is opt-in
    restriction: a user with no groups has full access, and `dbops-viewer` is
    what makes someone read-only.

- [ ] 5. Log in and register a cluster

  - Open the Web UI URL from step 3 and log in through the Cognito Hosted UI.
    Callback URLs are auto-registered at deploy time, so no manual Cognito work
    is required.
  - Go to Clusters, then Register. Use "test connection only" first: it runs a
    3-step pre-flight (STS AssumeRole, DescribeDBClusters, master secret).
  - For cross-account targets, deploy the spoke role first
    (`cdk/cross-account/spoke-role-template.yaml`) and register with its ARN. See
    `README.md` and `cdk/cross-account/README.md`.
  - Production hardening: create a dedicated `dbops_readonly` user per cluster
    and store it as `dbops/<cluster_id>/readonly` in Secrets Manager so DBOps
    stops using the master secret. The Clusters page has a setup guide button
    with the exact SQL.

- [ ] 6. Wait for the first ETL cycle

  - The ETL collector runs on an EventBridge schedule every
    `STATS_COLLECTION_INTERVAL_MIN` minutes (default 5), so the dashboard is
    empty until the first cycle after registration completes. This is expected,
    not a bug. The Fleet page shows an ETL freshness badge per cluster.
  - Re-run `./smoke-test.sh` afterwards for a clean end-to-end result.

- [ ] 7. Optional post-deploy steps

  - Activate the `Application` cost allocation tag in the billing console, or
    the `/cost` page shows $0. Cost Explorer does not backfill.
  - Set `SLACK_SIGNING_SECRET` and redeploy the agent stack if you want two-way
    Slack ack. Outbound Slack alerts work without it.
  - Tighten CORS by setting `ALLOWED_ORIGINS` on the dashboard and alerts
    Lambdas for production.
  - All detail is in `README.md` under Quick Start.

## What is actually running after this

- 4 MCP servers behind one AgentCore Gateway, 63 tools total: performance 11,
  incident 9, operations 34, simulation 9 (`cdk/tool_definitions.py`). Official
  AWS documentation comes live from the AWS-managed docs MCP server over SigV4,
  not from a Bedrock Knowledge Base. No Knowledge Base or S3 Vectors resource
  exists in any stack, and semantic incident search runs on pgvector plus
  `amazon.titan-embed-text-v2:0` embeddings.
- Reads are automatic. Writes and changes require human approval, enforced
  server side by the tool-level `approval_guard` (fail closed, payload-hash
  bound, single use). The Cedar Policy Engine is bound at the Gateway in
  LOG_ONLY as defense in depth, so it is not the enforcement point.
- Next.js 16 static export on CloudFront plus S3, fetching `/config.json` at
  boot so the bundle stays portable.
