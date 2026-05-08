import aws_cdk as cdk
from aws_cdk import (
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_sns as sns,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
)
from constructs import Construct
from config.settings import Settings


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

        self.etl_lambda = lambda_.Function(
            self, "ETLCollector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/etl_collector"),
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
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

        events.Rule(
            self, "ETLSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Settings.STATS_COLLECTION_INTERVAL_MIN)
            ),
            targets=[targets.LambdaFunction(self.etl_lambda)],
        )

        self.alert_topic = sns.Topic(self, "AlertTopic", topic_name=f"dbops-{Settings.ENV}-alerts")

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

        cdk.CfnOutput(self, "CacheDbClusterArn", value=self.cache_db.cluster_arn)
        cdk.CfnOutput(self, "CacheDbEndpoint", value=self.cache_db.cluster_endpoint.hostname)
