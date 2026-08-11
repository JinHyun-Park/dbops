import hashlib
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_rds as rds,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    custom_resources as cr,
)
from bundling import _PipLocalBundling
from config.settings import Settings
from constructs import Construct

# DocDB Mongo collector cadence, in minutes. ONE definition for both its
# EventBridge rate and the COLLECTOR_INTERVAL_MIN env var it derives its
# CloudWatch profiler-log read window from.
#
# Do NOT raise this without also revisiting the multi-writer findings freshness
# window: api/dashboard/handler.py and mcp_servers/incident/tools/
# maintenance_findings.py floor that window at 15 minutes precisely because this
# collector and rds_direct_collector are pinned to 5 (see commit 67d1c3e).
_DOCDB_MONGO_INTERVAL_MIN = 5


def _hash_schema_dir(rel_path: str) -> str:
    """Concatenate every .sql file in `rel_path` (sorted by name) and
    return the first 12 hex chars of its SHA-256. Used as the
    schema_version property on the Custom Resource so any edit to a
    migration file auto-triggers a re-run on the next `cdk deploy`.

    Falls back to a sentinel string if the directory is missing so synth
    doesn't fail in a partial checkout — the migrator will simply not
    pick up new schema until the directory exists again.
    """
    base = Path(__file__).resolve().parent.parent / rel_path
    if not base.is_dir():
        return "no-sql-dir"
    h = hashlib.sha256()
    for sql in sorted(base.glob("*.sql")):
        h.update(sql.name.encode())
        h.update(b"\x00")
        h.update(sql.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:12]


class DataStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=1,
        )

        self.cache_db = rds.DatabaseCluster(
            self, "CacheDB",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_15_10,
            ),
            serverless_v2_min_capacity=Settings.CACHE_DB_MIN_ACU,
            serverless_v2_max_capacity=Settings.CACHE_DB_MAX_ACU,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            vpc=self.vpc,
            default_database_name="dbops",
            enable_data_api=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Archive bucket (S3 Tables / Iceberg + report exports). Without a
        # lifecycle it grows unbounded — old archived metrics/reports are rarely
        # read, so tier them down to cheaper storage automatically. Retention
        # (object expiration) is OPT-IN per org via ARCHIVE_RETENTION_DAYS
        # (default 0 = keep forever — never delete a deployer's audit archive by
        # surprise); transitions always run (pure cost savings, no data loss).
        _retention_days = getattr(Settings, "ARCHIVE_RETENTION_DAYS", 0)
        self.archive_bucket = s3.Bucket(
            self, "ArchiveBucket",
            bucket_name=f"dbops-{Settings.ENV}-archive-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,  # cdk-nag AwsSolutions-S10
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="archive-tiering",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=cdk.Duration.days(30),
                        ),
                        # Glacier Instant Retrieval keeps mid-term data queryable
                        # (Athena / S3 Tables) at ~Glacier cost.
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=cdk.Duration.days(90),
                        ),
                        s3.Transition(
                            storage_class=s3.StorageClass.DEEP_ARCHIVE,
                            transition_after=cdk.Duration.days(365),
                        ),
                    ],
                    # Reclaim storage from failed multipart uploads.
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(7),
                    # Per-org retention: only expire when explicitly configured.
                    expiration=(
                        cdk.Duration.days(_retention_days)
                        if _retention_days and _retention_days > 0
                        else None
                    ),
                ),
            ],
        )

        # Schema migrator — runs all schema_v*.sql on stack create/update via Custom Resource.
        # This replaces the manual `aws rds-data execute-statement` loop from deploy.sh.
        self.schema_migrator = lambda_.Function(
            self, "SchemaMigrator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/schema_migrator"),
            timeout=cdk.Duration.minutes(5),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        self.cache_db.secret.grant_read(self.schema_migrator)
        self.cache_db.grant_data_api_access(self.schema_migrator)
        # Custom Resource: re-run migration on every stack deploy. Idempotent — all DDL
        # uses IF NOT EXISTS so reruns are safe.
        migrate_provider = cr.Provider(
            self, "SchemaMigratorProvider",
            on_event_handler=self.schema_migrator,
        )
        # Auto-derive schema_version from the SHA-256 of every SQL file in
        # schema_migrator/sql/. CloudFormation re-fires the Custom Resource
        # whenever any property changes, so this means: change a .sql file
        # → next `cdk deploy` migrates. No more "I added a column but
        # forgot to bump schema_version → migration never ran."
        migrate_resource = cdk.CustomResource(
            self, "SchemaMigratorRun",
            service_token=migrate_provider.service_token,
            properties={
                "schema_version": _hash_schema_dir(
                    "../data-pipeline/schema_migrator/sql",
                ),
            },
        )
        migrate_resource.node.add_dependency(self.cache_db)

        self.etl_lambda = lambda_.Function(
            self, "ETLCollector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/etl_collector"),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "HUB_ROLE_ARN": foundation.hub_role.role_arn,
                "APM_TARGETS_TABLE": foundation.apm_targets_table.table_name,
            },
        )

        self.cache_db.secret.grant_read(self.etl_lambda)
        self.cache_db.grant_data_api_access(self.etl_lambda)
        foundation.clusters_table.grant_read_data(self.etl_lambda)
        foundation.apm_targets_table.grant_read_data(self.etl_lambda)
        foundation.hub_role.grant(self.etl_lambda.role, "sts:AssumeRole")
        # Cross-account metric collection: assume each registered cluster's spoke
        # role DIRECTLY (same pattern as the dashboard/MCP _session_for) so RDS /
        # PI / CloudWatch / RDS-Data reads run in the cluster's OWN account.
        # Scoped to the documented spoke role name; same-account clusters carry
        # no spoke_role_arn and skip AssumeRole entirely (behaviour unchanged).
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/dbops-spoke-role"],
        ))

        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds:DescribeDBClusters", "rds:DescribeDBInstances", "rds:ListTagsForResource",
                     "rds:DescribeDBSubnetGroups",  # VPC/AZ context for the DB Map (meta_collector._vpc_info)
                     "pi:GetResourceMetrics", "pi:DescribeDimensionKeys",
                     "rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
                     "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
                     # Cost Explorer — Savings Plan / RI recommendations for the
                     # cost_savings_plan_opportunity finding. Cached 23h on the
                     # cache DB so the per-call $0.01 fee fires once per day at most.
                     "ce:GetSavingsPlansPurchaseRecommendation",
                     "ce:GetReservationPurchaseRecommendation"],
            resources=["*"],
        ))
        # APM collector: same-account targets (no spoke_role_arn) read CloudWatch
        # Logs under this role. Read-only; cross-account targets use the spoke role.
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["logs:StartQuery", "logs:GetQueryResults",
                     "logs:FilterLogEvents", "logs:DescribeLogGroups"],
            resources=["*"],
        ))
        # Target DB secrets are registry-defined (arbitrary ARNs, incl. RDS-managed),
        # so this can't be ARN-scoped, but it is bounded to the hub account: cross-
        # account targets read their secret via the assumed spoke role, not this one.
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:*"],
        ))
        # Titan text embeddings for the incident-similarity backfill
        # (incident_embeddings collector). Scoped to the embed model only.
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0"],
        ))
        # Multi-engine (DynamoDB/DocumentDB) discovery + metrics: the ETL collector
        # enumerates DynamoDB tables and DocumentDB clusters in each spoke account
        # to populate cluster_meta and collect CloudWatch metrics for non-Aurora engines.
        # Note: DocumentDB describe is covered by the existing rds:DescribeDBClusters above
        # (DocumentDB IAM uses the rds: prefix, not docdb:).
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:ListTables", "dynamodb:DescribeTable"],
            resources=["*"],
        ))
        # ElastiCache forward-compat: the EC-1 collector uses resource_name from
        # the registry (no describe calls at metric-pull time), but grant describe
        # here so future collectors can enumerate clusters without an IAM change.
        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
        ))

        events.Rule(
            self, "ETLSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Settings.STATS_COLLECTION_INTERVAL_MIN)
            ),
            targets=[targets.LambdaFunction(self.etl_lambda)],
        )

        # DocumentDB Mongo-protocol deep-diagnosis collector. UNLIKE the ETL
        # collector (which is NOT in a VPC and only calls public AWS APIs), this
        # one connects to DocumentDB over the Mongo wire protocol on TLS 27017,
        # which lives inside the private VPC — so it MUST be in-VPC and bundle
        # pymongo + the RDS/DocDB CA (not in the Lambda runtime). It scans the
        # registry for documentdb rows carrying `mongo_secret_arn`, runs a
        # read-only command allowlist, and writes findings/metrics to the cache.
        docdb_mongo_sg = ec2.SecurityGroup(
            self, "DocDBMongoCollectorSG",
            vpc=self.vpc,
            description="DocumentDB Mongo deep-diagnosis collector - egress to DocDB 27017",
            allow_all_outbound=True,
        )
        self.docdb_mongo_lambda = lambda_.Function(
            self, "DocDBMongoCollector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                "../data-pipeline/docdb_mongo_collector",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    # Prefer local pip bundling (Docker-free CI / demo host); CDK
                    # falls back to the Docker command below if local returns False.
                    local=_PipLocalBundling("../data-pipeline/docdb_mongo_collector"),
                    command=[
                        "bash", "-c",
                        # pip-install pymongo + copy source, then fetch the
                        # RDS/DocDB CA bundle into the asset. The CA fetch is
                        # best-effort (|| true): if the build host has no network
                        # a committed global-bundle.pem fallback is used instead.
                        "pip install -r requirements.txt -t /asset-output "
                        "&& cp -au . /asset-output "
                        "&& (curl -fsSL -o /asset-output/global-bundle.pem "
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem "
                        "|| true)",
                    ],
                ),
            ),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
            security_groups=[docdb_mongo_sg],
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                # The collector derives its CloudWatch profiler-log read window
                # from its own cadence, so the schedule below and this value MUST
                # come from the same constant: a mismatch would make consecutive
                # windows overlap (inflating cumulative query_stats counters) or
                # gap (silently dropping slow ops).
                "COLLECTOR_INTERVAL_MIN": str(_DOCDB_MONGO_INTERVAL_MIN),
            },
        )
        self.cache_db.secret.grant_read(self.docdb_mongo_lambda)
        self.cache_db.grant_data_api_access(self.docdb_mongo_lambda)
        foundation.clusters_table.grant_read_data(self.docdb_mongo_lambda)
        # Profiler ingestion (E1-6). DocumentDB slow ops are not reachable over
        # the Mongo wire protocol; they are exported to CloudWatch Logs at
        # /aws/docdb/{cluster}/profiler, and whether they are exported AT ALL is
        # a cluster-parameter-group + log-export question. Both reads are scoped:
        # the log grant to the /aws/docdb/ prefix (same prefix the incident MCP
        # Lambda already reads), and DocumentDB authorizes its control plane
        # under the rds: action prefix, so no docdb:* actions are needed.
        self.docdb_mongo_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["logs:FilterLogEvents"],
            resources=[
                f"arn:aws:logs:*:*:log-group:/aws/docdb/*{suffix}"
                for suffix in ("", ":*")
            ],
        ))
        self.docdb_mongo_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:DescribeDBClusterParameters",
                # Registered clusters can live in spoke accounts; the profiler
                # reads above must run in the CLUSTER'S account or they read the
                # hub and find nothing. Same hub-spoke chaining every other
                # cross-account Lambda in this project uses.
                "sts:AssumeRole",
            ],
            resources=["*"],
        ))
        # Per-cluster read-only Mongo creds live in arbitrary Secrets Manager
        # secrets whose ARNs are on the registry rows (mongo_secret_arn), so this
        # must be resource "*" — the deployer scopes each secret to one RO user.
        self.docdb_mongo_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:*"],
        ))

        events.Rule(
            self, "DocDBMongoCollectorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(_DOCDB_MONGO_INTERVAL_MIN)),
            targets=[targets.LambdaFunction(self.docdb_mongo_lambda)],
        )

        # RDS-instance direct collector. Like the DocDB Mongo collector (and
        # UNLIKE the ETL collector, which only calls public AWS APIs), this one
        # connects to the target MySQL over the wire protocol on 3306, which
        # lives inside the private VPC — so it MUST be in-VPC and bundle pymysql
        # + the RDS CA (not in the Lambda runtime). It scans the registry for
        # rows carrying a db_secret_arn and pulls activity/InnoDB/lock stats.
        rds_direct_sg = ec2.SecurityGroup(
            self, "RdsDirectCollectorSG",
            vpc=self.vpc,
            description="dbops rds-instance direct collector - egress to mysql 3306",
            allow_all_outbound=True,
        )
        self.rds_direct_lambda = lambda_.Function(
            self, "RdsDirectCollector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                "../data-pipeline/rds_direct_collector",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    # Prefer local pip bundling (Docker-free CI / demo host); CDK
                    # falls back to the Docker command below if local returns False.
                    local=_PipLocalBundling("../data-pipeline/rds_direct_collector"),
                    command=[
                        "bash", "-c",
                        # pip-install pymysql + copy source, then fetch the
                        # RDS CA bundle into the asset. The CA fetch is
                        # best-effort (|| true): if the build host has no network
                        # a committed global-bundle.pem fallback is used instead.
                        "pip install -r requirements.txt -t /asset-output "
                        "&& cp -au . /asset-output "
                        "&& (curl -fsSL -o /asset-output/global-bundle.pem "
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem "
                        "|| true)",
                    ],
                ),
            ),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
            security_groups=[rds_direct_sg],
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        self.cache_db.secret.grant_read(self.rds_direct_lambda)
        self.cache_db.grant_data_api_access(self.rds_direct_lambda)
        foundation.clusters_table.grant_read_data(self.rds_direct_lambda)
        # Per-cluster DB creds live in arbitrary Secrets Manager secrets whose
        # ARNs are on the registry rows (db_secret_arn), so this must be
        # resource "*" — the deployer scopes each secret to one cluster.
        self.rds_direct_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:*"],
        ))

        events.Rule(
            self, "RdsDirectCollectorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.rds_direct_lambda)],
        )

        self.alert_topic = sns.Topic(self, "AlertTopic", topic_name=f"dbops-{Settings.ENV}-alerts", enforce_ssl=True)  # cdk-nag AwsSolutions-SNS3

        self.alert_evaluator = lambda_.Function(
            self, "AlertEvaluator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/alert_evaluator"),
            timeout=cdk.Duration.seconds(60),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                # Historical name kept here for alert_evaluator/handler.py;
                # everywhere else uses ALERT_TOPIC_ARN. The evaluator's
                # env-var lookup will be normalized in a follow-up so this
                # alias can go away.
                "ALERT_SNS_TOPIC_ARN": self.alert_topic.topic_arn,
                "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
                # Slack button / PagerDuty link target. Empty disables the deep-link.
                "FRONTEND_URL": Settings.FRONTEND_URL,
                # PagerDuty dedup TTL — same rule re-opens an incident every N minutes.
                "ALERT_DEDUP_WINDOW_MINUTES": str(Settings.ALERT_DEDUP_WINDOW_MINUTES),
            },
        )
        self.cache_db.secret.grant_read(self.alert_evaluator)
        self.cache_db.grant_data_api_access(self.alert_evaluator)
        self.alert_topic.grant_publish(self.alert_evaluator)
        # Instant in-app push of fired alerts over the WS channel (foundation).
        foundation.grant_alert_broadcast(self.alert_evaluator)
        # Event-based auto-RCA: on trigger, enqueue a pending agent task; the
        # agent-tasks stream then drives the task_worker (agent stack). data
        # can't invoke the worker directly, so the table is the decoupling point.
        foundation.grant_task_enqueue(self.alert_evaluator)

        events.Rule(
            self, "AlertEvaluatorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.alert_evaluator)],
        )

        # High-resolution active-session sampler (~5s near-ASH). Like the ETL
        # collector it's NOT in a VPC (RDS Data API only); it self-loops within
        # each 1-min invocation to sample far faster than the 5-min ETL, writing
        # to active_session_samples (which it also prunes to 7d).
        self.ash_sampler = lambda_.Function(
            self, "AshSampler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/ash_sampler"),
            timeout=cdk.Duration.seconds(60),
            memory_size=256,
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        self.cache_db.secret.grant_read(self.ash_sampler)
        self.cache_db.grant_data_api_access(self.ash_sampler)
        foundation.clusters_table.grant_read_data(self.ash_sampler)
        # Sample each relational target's pg_stat_activity / processlist via the
        # Data API; target cluster ARNs + secrets are registry-defined, so this is
        # resource "*" (same as the ETL collector's target access).
        self.ash_sampler.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-data:ExecuteStatement"],
            resources=["*"],
        ))
        self.ash_sampler.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:*"],
        ))
        events.Rule(
            self, "AshSamplerSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(1)),
            targets=[targets.LambdaFunction(self.ash_sampler)],
        )

        # Recurring agent work — reads due scheduled_tasks and enqueues a pending
        # agent-tasks row for each (the worker then runs the report). Public
        # endpoints only (Data API + DynamoDB), no agent-stack dependency.
        self.task_scheduler = lambda_.Function(
            self, "TaskScheduler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/task_scheduler"),
            timeout=cdk.Duration.seconds(60),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        self.cache_db.secret.grant_read(self.task_scheduler)
        self.cache_db.grant_data_api_access(self.task_scheduler)
        foundation.grant_task_enqueue(self.task_scheduler)  # agent-tasks put + AGENT_TASKS_TABLE env
        events.Rule(
            self, "TaskSchedulerSchedule",
            schedule=events.Schedule.rate(cdk.Duration.hours(1)),
            targets=[targets.LambdaFunction(self.task_scheduler)],
        )

        # Remediation Outcome Loop — opens a case per emitted recommendation and
        # judges whether the symptom resolved (baseline recovery / finding
        # clearance), feeding remediation_outcomes_agg. Public endpoints only.
        self.outcome_evaluator = lambda_.Function(
            self, "OutcomeEvaluator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/outcome_evaluator"),
            timeout=cdk.Duration.seconds(120),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        self.cache_db.secret.grant_read(self.outcome_evaluator)
        self.cache_db.grant_data_api_access(self.outcome_evaluator)
        # Phase 2: RCA-sourced cases read recently completed agent tasks.
        foundation.agent_tasks_table.grant_read_data(self.outcome_evaluator)
        self.outcome_evaluator.add_environment(
            "AGENT_TASKS_TABLE", foundation.agent_tasks_table.table_name)
        events.Rule(
            self, "OutcomeEvaluatorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(20)),
            targets=[targets.LambdaFunction(self.outcome_evaluator)],
        )

        self.event_processor = lambda_.Function(
            self, "EventProcessor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/event_processor"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
            },
        )
        self.cache_db.secret.grant_read(self.event_processor)
        self.cache_db.grant_data_api_access(self.event_processor)
        self.alert_topic.grant_publish(self.event_processor)

        events.Rule(self, "RDSEventRule",
            event_pattern=events.EventPattern(source=["aws.rds"]),
            targets=[targets.LambdaFunction(self.event_processor)],
        )
        events.Rule(self, "CloudWatchAlarmRule",
            event_pattern=events.EventPattern(source=["aws.cloudwatch"], detail_type=["CloudWatch Alarm State Change"]),
            targets=[targets.LambdaFunction(self.event_processor)],
        )

        self.report_generator = lambda_.Function(
            self, "ReportGenerator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/report_generator"),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "ARCHIVE_BUCKET": self.archive_bucket.bucket_name,
                "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
                "REPORT_DELIVERY_ENABLED": "true" if getattr(Settings, "REPORT_DELIVERY_ENABLED", False) else "false",
                # handler.py defaults this to an `apac.`-prefixed inference profile.
                # Cross-region inference profiles are REGIONAL: an apac. profile does not
                # resolve for a deployer in us-east-1 or eu-central-1, so every daily
                # report summary would fail for them while the rest of the platform
                # looked healthy. Pass the operator's own choice, the same value the
                # agent and the RCA narrative already get.
                "REPORT_SUMMARY_MODEL_ID": Settings.AGENT_MODEL_ID,
            },
        )
        self.cache_db.secret.grant_read(self.report_generator)
        self.cache_db.grant_data_api_access(self.report_generator)
        foundation.grant_app_config_read(self.report_generator)  # DB-backed REPORT_DELIVERY_ENABLED
        self.archive_bucket.grant_write(self.report_generator)
        self.alert_topic.grant_publish(self.report_generator)
        # NL summary path invokes a Bedrock Claude model. Failure to invoke
        # falls back to a template, so this permission is best-effort —
        # but without it every report is template-summary which is dull.
        self.report_generator.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                "arn:aws:bedrock:*:*:inference-profile/*",
            ],
        ))

        events.Rule(self, "DailyReportSchedule",
            schedule=events.Schedule.cron(hour="0", minute="0"),
            targets=[targets.LambdaFunction(self.report_generator)],
        )

        self.proactive_monitor = lambda_.Function(
            self, "ProactiveMonitor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/proactive_monitor"),
            timeout=cdk.Duration.minutes(2),
            memory_size=256,
            vpc=self.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "ALERT_TOPIC_ARN": self.alert_topic.topic_arn,
            },
        )
        self.cache_db.secret.grant_read(self.proactive_monitor)
        self.cache_db.grant_data_api_access(self.proactive_monitor)
        self.alert_topic.grant_publish(self.proactive_monitor)

        events.Rule(self, "ProactiveMonitorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.proactive_monitor)],
        )

        # Restore finalizer (phase 3). RestoreDBCluster* only restores the
        # cluster volume — the writer instance must be added AFTER the cluster
        # reaches `available`, which outlasts the synchronous restore request.
        # This scheduled Lambda scans the registry for `pending_instance` rows
        # and finishes the job: create one db.serverless writer + backfill the
        # connection coordinates so the restored cluster becomes queryable.
        self.restore_finalizer = lambda_.Function(
            self, "RestoreFinalizer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/restore_finalizer"),
            # 150s > the operations Lambda's 120s: the scale-out warm pass invokes
            # prewarm_reader SYNCHRONOUSLY (Lambda only delivers ClientContext —
            # which carries the tool name — on RequestResponse, not async Event),
            # and dispatches at most one warm per tick.
            timeout=cdk.Duration.seconds(150),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                # Scale-out + auto-warmup (N-④ Phase 1): the second pass drives
                # prewarm approval rows (awaiting_instance→pending→approved) and
                # invokes the operations Lambda's prewarm_reader on approval. The
                # operations function NAME is a literal derived from Settings.ENV
                # (the SAME literal agent_stack sets as its function_name) so this
                # stack takes NO cross-stack reference on agent_stack — that would
                # be a dependency cycle (agent already depends on data).
                "APPROVALS_TABLE": foundation.approvals_table.table_name,
                "OPERATIONS_FUNCTION_NAME": f"dbops-{Settings.ENV}-operations-mcp",
            },
        )
        self.cache_db.secret.grant_read(self.restore_finalizer)
        self.cache_db.grant_data_api_access(self.restore_finalizer)
        foundation.clusters_table.grant_read_write_data(self.restore_finalizer)
        # Scale-out prewarm state machine lives in the approvals table.
        foundation.approvals_table.grant_read_write_data(self.restore_finalizer)
        self.restore_finalizer.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                # Scale-out pass polls the new reader INSTANCE for `available`.
                "rds:DescribeDBInstances",
                # Adds the writer instance to the restored cluster once it is
                # available. AddTags stamps dbops:type=restored on the instance.
                "rds:CreateDBInstance",
                "rds:AddTagsToResource",
                # Cross-account restores: assume the cluster's spoke role
                # (carried on the registry row) to finalize a cluster that
                # landed in a spoke account.
                "sts:AssumeRole",
            ],
            resources=["*"],
        ))
        # Invoke the operations MCP Lambda's prewarm_reader on an approved
        # scale-out warm. Scoped to the deterministic operations function name
        # (built from Settings.ENV + pseudo account/region intrinsics — no
        # agent_stack token, so no dependency cycle).
        self.restore_finalizer.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:"
                f"function:dbops-{Settings.ENV}-operations-mcp"
            ],
        ))
        events.Rule(self, "RestoreFinalizerSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(2)),
            targets=[targets.LambdaFunction(self.restore_finalizer)],
        )

        cdk.CfnOutput(self, "CacheDbClusterArn", value=self.cache_db.cluster_arn)
        cdk.CfnOutput(self, "CacheDbEndpoint", value=self.cache_db.cluster_endpoint.hostname)
