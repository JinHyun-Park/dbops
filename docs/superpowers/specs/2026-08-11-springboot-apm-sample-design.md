# Sample Spring Boot App for EC2 APM Testing — Design

**Date:** 2026-08-11
**Status:** Approved (brainstorming), pending implementation plan
**Scope:** A deployable sample Java/Spring Boot CRUD application on EC2, with intentional
backend bugs, used to exercise and validate the DBOps **APM** feature (application-log
tracking + host metrics) end-to-end. Lives entirely under `samples/springboot/`.

## 1. Goal & Non-Goals

### Goal

Give DBOps a real thing to monitor. The APM feature (`api/apm`, `/apm` page,
`apm_collector`) is already built and reads CloudWatch **read-only**; it assumes the user
has pre-installed instrumentation on EC2. This sample IS that pre-installed target:

- A small Spring Boot **To-Do/Task CRUD** app that emits plain-text file logs.
- **CloudWatch Agent** installed on the EC2 host, shipping the app log file to a
  CloudWatch Log Group and publishing EC2 host metrics (CPU/mem/disk via `CWAgent`).
- **Three intentional backend bugs** that surface as `ERROR`/`WARN` log lines (and a
  memory trend on host metrics), so we can prove the `/apm` dashboard tracks them.
- Deploys to **the same AWS account + region as DBOps** (571850511781 / us-east-1), so
  no cross-account spoke role is needed — the APM Lambda reads CloudWatch locally.

### Non-Goals (YAGNI)

- **No ADOT / Application Signals.** Only CloudWatch Agent (logs + host metrics). The
  APM/latency metric cards may read empty; that is acceptable for this sample.
- **No public inbound.** The app EC2 has no public IP and no inbound security-group
  rules. Access is via SSM Session Manager only.
- Not wired into the DBOps main test suite (`tests/cdk`, parity tests). `samples/` is
  managed separately from the platform deployment, like the existing `samples/cdk`.
- No changes to `api/apm`, `apm_collector`, or the `/apm` frontend. If a same-account
  IAM read grant turns out to be missing on the APM Lambda, that is a one-line follow-up
  in `cdk/stacks/agent_stack.py` handled after a deploy-time check — out of this spec.

## 2. Key Decisions (from brainstorming)

| Decision | Choice |
| --- | --- |
| Instrumentation scope | CloudWatch Agent only → **app logs + EC2 host metrics** (no ADOT) |
| Network | **Own VPC**, private subnet for the app EC2 (no public IP, egress only) |
| Outbound path | **NAT Gateway** in a public subnet (so yum/Maven Central/CloudWatch reachable) |
| CRUD domain | **To-Do/Task API**, H2 in-memory DB with a UNIQUE constraint |
| Intentional bugs | NPE (500), unvalidated input → DB constraint violation, resource leak |
| Build/deploy | **Fat jar as CDK S3 asset** (build `mvn package` before `cdk deploy`) |
| Traffic generation | VPC-internal load-generator Lambda (private), 2-min EventBridge schedule |
| Same-account | Yes → APM target registered with `spoke_role_arn` blank; no cross-account |

### Why same-account works without a spoke role

`api/apm/handler.py::_session_for()` (lines ~234–245) returns a **local** boto3 session
when `role_arn` is empty, and only calls `sts:assume_role` when a `spoke_role_arn` is
present. The APM target is matched by **log group name + instance_id + region**, not by
stack. So a separate stack in the same account/region is fully visible: register the
target with `region=us-east-1` and `spoke_role_arn=""`.

## 3. Repository Layout

```
samples/springboot/
├── README.md                     # deploy + APM-target registration walkthrough (KST, 해요체)
├── app/                          # Spring Boot source (Maven)
│   ├── pom.xml                   # Spring Boot 3.x, Java 17, fat jar
│   └── src/
│       ├── main/java/com/dbops/todo/
│       │   ├── TodoApplication.java
│       │   ├── Task.java                 # @Entity: id, title (UNIQUE), done, note
│       │   ├── TaskRepository.java
│       │   ├── TaskController.java       # /api/tasks CRUD + /api/health
│       │   └── LeakController.java        # /api/leak — the resource-leak bug
│       ├── main/resources/
│       │   ├── application.yml           # H2 in-memory, logging to file
│       │   └── logback-spring.xml        # plain-text file appender (ISO8601 [LEVEL])
│       └── test/java/com/dbops/todo/     # unit tests incl. each bug trigger
└── cdk/
    ├── cdk.json                  # standalone app entry (avoids samples/cdk limitation)
    ├── app.py
    ├── requirements.txt
    └── springboot_apm_stack.py   # VPC + NAT + EC2 + CW Agent + load-gen Lambda
```

Standalone `cdk.json` + `app.py` so `cd samples/springboot/cdk && cdk deploy` works on
its own, sidestepping the known `samples/cdk` "no cdk.json / needs a VPC" limitation.

## 4. Application Design

### Domain — To-Do/Task CRUD (H2 in-memory)

`Task` entity: `id` (auto), `title` (VARCHAR, **UNIQUE, NOT NULL**), `done` (bool),
`note` (nullable). Repository is Spring Data JPA over H2 in-memory (resets on restart —
fine for a demo target).

| Route | Method | Behavior |
| --- | --- | --- |
| `/api/health` | GET | 200 `{status:"UP"}` |
| `/api/tasks` | GET | list all |
| `/api/tasks/{id}` | GET | one, 404 if missing |
| `/api/tasks` | POST | create |
| `/api/tasks/{id}` | PUT | update |
| `/api/tasks/{id}` | DELETE | delete |
| `/api/leak` | GET | resource-leak trigger (see bugs) |

### Logging — the axis the dashboard tracks

- Logback **file appender** to `/var/log/todoapp/app.log`, plain text:
  `%d{ISO8601} [%level] %logger{0} - %msg%n`. Level token is bracketed (`[ERROR]`,
  `[WARN]`) so the APM Logs-Insights level filter (`@message like /ERROR/`) matches.
- Console appender too (visible via `journalctl` under systemd), but CloudWatch tails
  the **file**.
- Uncaught exceptions logged with full stack trace at `ERROR` by a
  `@ControllerAdvice` handler that returns HTTP 500.

### Intentional bugs (normal traffic stays 200; bugs fire on specific inputs)

1. **NullPointerException → 500.** `POST /api/tasks` with `note` present but `title`
   omitted hits a code path that calls `title.trim()` on a null → NPE. The advice logs
   an `ERROR` stack trace and returns 500. (A guarded path would 400; we deliberately
   skip the guard.)
2. **Unvalidated input → DB constraint violation.** No pre-check for duplicate `title`;
   creating a second task with an existing title throws
   `DataIntegrityViolationException` (UNIQUE) → logged `ERROR`, 500. Demonstrates a
   missing-validation bug distinct from the NPE.
3. **Resource leak.** `GET /api/leak` opens a JDBC `Connection` (or allocates a
   retained buffer into a static list) and never closes/releases it. Each call logs a
   `WARN` ("leaked resource, open handles=N") and, as N grows, an `ERROR` when a
   threshold is crossed. Over time this shows as a rising memory trend on the EC2 host
   metrics and a growing WARN/ERROR count — the multi-signal case.

Bugs are triggered by the load generator at a low rate (see §5) so the dashboard shows a
steady trickle of ERROR/WARN against a majority of healthy 200s.

## 5. Infrastructure Design (`springboot_apm_stack.py`)

Single stack `dbops-dev-springboot-apm`.

### Network (own VPC, private-only app)

- `ec2.Vpc` with 2 AZs: **public** subnets (NAT Gateway lives here) + **private
  (with-egress)** subnets (app EC2 + load-gen Lambda). `natGateways=1`.
- App EC2: **no public IP**, security group with **no inbound rules** (egress all).
- Load-gen Lambda: in the private subnets, its SG allowed to reach the app SG on 8080.

### App EC2

- `t3.small`, Amazon Linux 2023, in a private subnet.
- IAM instance role: `AmazonSSMManagedInstanceCore` (Session Manager) +
  `CloudWatchAgentServerPolicy` (agent push).
- **user-data**: install Java 17 (`dnf install -y java-17-amazon-corretto`), download
  the fat jar from the CDK S3 asset, write a `systemd` unit (`todoapp.service`) running
  the jar on :8080, install & configure the CloudWatch Agent from an SSM parameter /
  inline config that (a) tails `/var/log/todoapp/app.log` → Log Group
  `/dbops/apm/todoapp`, (b) publishes CPU/mem/disk under the `CWAgent` namespace.
- Log Group `/dbops/apm/todoapp` created by CDK (retention 1 week) so it exists before
  first boot.

### Load generator (traffic)

- Python Lambda, private subnets, `EventBridge` rate(2 min). Resolves the app's private
  IP (via a tag/SSM parameter the stack writes, or EC2 describe) and issues a mix:
  mostly valid CRUD (200s), plus a low rate of the 3 bug triggers. Keeps ERROR/WARN and
  host metrics continuously flowing.

### Outputs (for APM target registration)

`CfnOutput`: `InstanceId`, `LogGroup` (`/dbops/apm/todoapp`), `Region`, `VpcId`,
`AppPrivateIpHint`. README shows pasting these into `/apm` → register target with
`spoke_role_arn` blank.

## 6. DBOps APM Integration Flow

1. `cd samples/springboot/app && mvn -q package` → fat jar in `target/`.
2. `cd samples/springboot/cdk && cdk deploy dbops-dev-springboot-apm`.
3. Wait for the app to boot (SSM Session Manager: `systemctl status todoapp`,
   `journalctl -u todoapp`), and for the load generator to run once.
4. In DBOps `/apm`: **register target** with the CfnOutput `InstanceId`, `LogGroup`,
   `Region=us-east-1`, `spoke_role_arn` empty.
5. Verify: overview cards populate (host CPU/mem; latency cards may be empty — no ADOT),
   and **log search** with default ERROR+WARN returns the injected bug lines.
6. **Deploy-time IAM check (follow-up, not this spec):** if `/apm` log search returns an
   AccessDenied, add same-account CloudWatch read (`logs:StartQuery`/`GetQueryResults`/
   `FilterLogEvents`, `cloudwatch:GetMetricData`) to the APM Lambda role in
   `cdk/stacks/agent_stack.py`.

## 7. Testing

- **App:** `cd samples/springboot/app && mvn test` — CRUD happy paths + one test per bug
  proving it throws/logs as designed (NPE path, duplicate-title constraint, leak counter
  increment).
- **CDK:** `cd samples/springboot/cdk && cdk synth` succeeds (standalone app).
- **Post-deploy (hard rule):** SSM into the box, confirm `todoapp.service` is active and
  `/var/log/todoapp/app.log` grows; then browser-test the DBOps `/apm` page — select the
  target, run an ERROR+WARN log search, confirm the injected bug lines appear.
- Not added to `tests/unit` or `tests/cdk` (samples are out of the platform suite).

## 8. Cost & Cleanup

- Running cost dominated by **1 NAT Gateway** (~$32/mo + data) + 1 `t3.small`.
- Cleanup: `cd samples/springboot/cdk && cdk destroy dbops-dev-springboot-apm`. Fully
  self-contained (own VPC), so no ordering dependency with the main DBOps stacks —
  unlike `samples/cdk`, which shares the main Data VPC.
