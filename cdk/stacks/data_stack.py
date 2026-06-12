import hashlib
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import jsii
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
from config.settings import Settings
from constructs import Construct


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


@jsii.implements(cdk.ILocalBundling)
class _PipLocalBundling:
    """Local bundling fallback for the DocDB Mongo collector asset.

    The collector needs pymongo (not in the Lambda runtime) + the RDS/DocDB CA
    bundle. CDK's default path bundles inside Docker, but Docker isn't always
    available (CI / the demo host) — and the existing tests/cdk synth smoke test
    must stay Docker-free. If local `pip` is present we build the asset on the
    host (pip install the linux manylinux wheels for py3.12 + copy source + fetch
    the CA); otherwise try_bundle returns False and CDK falls back to Docker.

    pymongo ships pure-Python with optional C extensions; we install the
    manylinux2014_x86_64 wheels for the Lambda runtime so the host arch/OS
    doesn't leak into the asset.
    """

    def __init__(self, source_dir: str):
        self._source_dir = Path(source_dir).resolve()

    def try_bundle(self, output_dir: str, *_args, **_kwargs) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if pip is None:
            return False  # no local pip → let CDK use Docker
        out = Path(output_dir)
        try:
            subprocess.run(
                [
                    pip, "install",
                    "-r", str(self._source_dir / "requirements.txt"),
                    "-t", str(out),
                    "--platform", "manylinux2014_x86_64",
                    "--implementation", "cp",
                    "--python-version", "3.12",
                    "--only-binary=:all:",
                    "--upgrade",
                ],
                check=True,
                capture_output=True,
            )
            # Copy the handler source (everything but requirements.txt) into the asset.
            for item in self._source_dir.iterdir():
                if item.name == "requirements.txt":
                    continue
                dest = out / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            # Fetch the RDS/DocDB CA bundle (best-effort: a committed pem in the
            # source dir, copied above, is the fallback when the host is offline).
            curl = shutil.which("curl")
            if curl is not None:
                subprocess.run(
                    [
                        curl, "-fsSL", "-o", str(out / "global-bundle.pem"),
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
                    ],
                    check=False,
                    capture_output=True,
                )
            return True
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"[DocDBMongoCollector] local bundling failed, falling back to Docker: {e}")
            return False


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

        self.archive_bucket = s3.Bucket(
            self, "ArchiveBucket",
            bucket_name=f"dbops-{Settings.ENV}-archive-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
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
            },
        )

        self.cache_db.secret.grant_read(self.etl_lambda)
        self.cache_db.grant_data_api_access(self.etl_lambda)
        foundation.clusters_table.grant_read_data(self.etl_lambda)
        foundation.hub_role.grant(self.etl_lambda.role, "sts:AssumeRole")

        self.etl_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds:DescribeDBClusters", "rds:DescribeDBInstances", "rds:ListTagsForResource",
                     "pi:GetResourceMetrics", "pi:DescribeDimensionKeys",
                     "rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
                     "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
                     "secretsmanager:GetSecretValue",
                     # Cost Explorer — Savings Plan / RI recommendations for the
                     # cost_savings_plan_opportunity finding. Cached 23h on the
                     # cache DB so the per-call $0.01 fee fires once per day at most.
                     "ce:GetSavingsPlansPurchaseRecommendation",
                     "ce:GetReservationPurchaseRecommendation"],
            resources=["*"],
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
            },
        )
        self.cache_db.secret.grant_read(self.docdb_mongo_lambda)
        self.cache_db.grant_data_api_access(self.docdb_mongo_lambda)
        foundation.clusters_table.grant_read_data(self.docdb_mongo_lambda)
        # Per-cluster read-only Mongo creds live in arbitrary Secrets Manager
        # secrets whose ARNs are on the registry rows (mongo_secret_arn), so this
        # must be resource "*" — the deployer scopes each secret to one RO user.
        self.docdb_mongo_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=["*"],
        ))

        events.Rule(
            self, "DocDBMongoCollectorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.docdb_mongo_lambda)],
        )

        self.alert_topic = sns.Topic(self, "AlertTopic", topic_name=f"dbops-{Settings.ENV}-alerts")

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

        events.Rule(
            self, "AlertEvaluatorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.alert_evaluator)],
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
            },
        )
        self.cache_db.secret.grant_read(self.report_generator)
        self.cache_db.grant_data_api_access(self.report_generator)
        self.archive_bucket.grant_write(self.report_generator)
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
            timeout=cdk.Duration.seconds(60),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        self.cache_db.secret.grant_read(self.restore_finalizer)
        self.cache_db.grant_data_api_access(self.restore_finalizer)
        foundation.clusters_table.grant_read_write_data(self.restore_finalizer)
        self.restore_finalizer.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
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
        events.Rule(self, "RestoreFinalizerSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(2)),
            targets=[targets.LambdaFunction(self.restore_finalizer)],
        )

        cdk.CfnOutput(self, "CacheDbClusterArn", value=self.cache_db.cluster_arn)
        cdk.CfnOutput(self, "CacheDbEndpoint", value=self.cache_db.cluster_endpoint.hostname)
