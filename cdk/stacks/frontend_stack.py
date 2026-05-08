import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from config.settings import Settings


class FrontendStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, agent, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self, "FrontendBucket",
            bucket_name=f"dbops-{Settings.ENV}-frontend-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        distribution = cloudfront.Distribution(
            self, "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        s3_deploy.BucketDeployment(
            self, "DeployFrontend",
            sources=[s3_deploy.Source.asset("../frontend/out")],
            destination_bucket=bucket,
            distribution=distribution,
        )

        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{distribution.distribution_domain_name}")
