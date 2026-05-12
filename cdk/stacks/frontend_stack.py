import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    custom_resources as cr,
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

        rewrite_function = cloudfront.Function(
            self, "PathRewrite",
            code=cloudfront.FunctionCode.from_inline("""
function handler(event) {
    var request = event.request;
    var uri = request.uri;
    if (uri === '/' || uri === '') {
        request.uri = '/index.html';
    } else if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
    } else if (!uri.includes('.')) {
        request.uri = uri + '.html';
    }
    return request;
}
"""),
        )

        distribution = cloudfront.Distribution(
            self, "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    ),
                ],
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        # NOTE: Cognito callback URL for the CloudFront domain must be added manually after
        # first deploy. Open this stack output (DistributionUrl), then add `<url>/callback`
        # to Settings.CALLBACK_URLS in cdk/config/settings.py and redeploy foundation stack.
        # We avoid wiring this automatically because it would create a cyclic
        # frontend->foundation->frontend dependency at synth time.

        # Single BucketDeployment: ship the static SPA + runtime config.json in one shot.
        # Doing them as two deployments lets the first one prune the second's artifact.
        s3_deploy.BucketDeployment(
            self, "DeployFrontend",
            sources=[
                s3_deploy.Source.asset("../frontend/out"),
                s3_deploy.Source.json_data("config.json", {
                    "apiUrl": agent.api.api_endpoint,
                    "frontendUrl": f"https://{distribution.distribution_domain_name}",
                    "region": Settings.REGION,
                    "cognitoDomain": f"https://{Settings.COGNITO_DOMAIN_PREFIX}.auth.{Settings.REGION}.amazoncognito.com",
                    "cognitoClientId": foundation.user_pool_client.user_pool_client_id,
                "cognitoUserPoolId": foundation.user_pool.user_pool_id,
                    "agentRuntimeArn": agent.runtime.agent_runtime_arn,
                }),
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # Auto-register the CloudFront callback URL with the Cognito user pool client
        # using an AWS Custom Resource. This avoids the cyclic frontend->foundation
        # dependency that L1 property overrides would create, because the Custom
        # Resource only reads cognito_client_id at deploy time (frontend -> foundation,
        # one-way direction the dependency graph already supports).
        callback_urls = [
            f"https://{distribution.distribution_domain_name}/callback",
            *Settings.CALLBACK_URLS,
        ]
        logout_urls = [
            f"https://{distribution.distribution_domain_name}",
            *[u.rsplit("/callback", 1)[0] for u in Settings.CALLBACK_URLS],
        ]
        update_cognito = cr.AwsCustomResource(
            self, "UpdateCognitoCallbacks",
            on_update=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="updateUserPoolClient",
                parameters={
                    "UserPoolId": foundation.user_pool.user_pool_id,
                    "ClientId": foundation.user_pool_client.user_pool_client_id,
                    "CallbackURLs": callback_urls,
                    "LogoutURLs": logout_urls,
                    "AllowedOAuthFlows": ["implicit"],
                    "AllowedOAuthScopes": ["openid", "profile", "email"],
                    "AllowedOAuthFlowsUserPoolClient": True,
                    "SupportedIdentityProviders": ["COGNITO"],
                    "ExplicitAuthFlows": [
                        "ALLOW_USER_SRP_AUTH",
                        "ALLOW_REFRESH_TOKEN_AUTH",
                        "ALLOW_USER_PASSWORD_AUTH",
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"cognito-callback-{distribution.distribution_domain_name}"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[foundation.user_pool.user_pool_arn]
            ),
            install_latest_aws_sdk=False,
        )
        update_cognito.node.add_dependency(distribution)

        # NOTE: ALLOWED_ORIGINS auto-injection is intentionally omitted.
        # Direct env injection from frontend onto agent Lambdas creates a cyclic
        # frontend->agent->frontend dependency via CloudFormation cross-stack exports
        # (frontend already imports agent's api_endpoint for config.json). Lambdas
        # default to "" → echo request Origin (safe baseline); to harden CORS in
        # production, set ALLOWED_ORIGINS env manually on dashboard/alerts Lambdas
        # to "https://<your-cloudfront>,http://localhost:3000".

        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "ConfigJsonUrl", value=f"https://{distribution.distribution_domain_name}/config.json")
