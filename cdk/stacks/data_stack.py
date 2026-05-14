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
from config.settings import Settings
from constructs import Construct


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
        migrate_resource = cdk.CustomResource(
            self, "SchemaMigratorRun",
            service_token=migrate_provider.service_token,
            properties={
                # Bumping this string forces re-run on next deploy if you need to.
                "schema_version": "v9",
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

        events.Rule(
            self, "ETLSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Settings.STATS_COLLECTION_INTERVAL_MIN)
            ),
            targets=[targets.LambdaFunction(self.etl_lambda)],
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
                "ALERT_SNS_TOPIC_ARN": self.alert_topic.topic_arn,
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

        cdk.CfnOutput(self, "CacheDbClusterArn", value=self.cache_db.cluster_arn)
        cdk.CfnOutput(self, "CacheDbEndpoint", value=self.cache_db.cluster_endpoint.hostname)
