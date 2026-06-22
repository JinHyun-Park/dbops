class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    COGNITO_DOMAIN_PREFIX = "dbops-dev"
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    # AWS MCP Server (AWS-managed, SigV4) — official AWS/Aurora docs. The agent
    # runtime signs requests with its IAM role and exposes ONLY the read-only
    # doc tools. Regional endpoint (us-east-1 / eu-central-1 only). Empty = doc
    # tools not registered. Replaces the deprecated public knowledge-mcp server.
    AWS_MCP_URL = "https://aws-mcp.us-east-1.api.aws/mcp"
    AWS_MCP_REGION = "us-east-1"

    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4

    # Archive bucket (S3 Tables / Iceberg + reports) retention. Objects always
    # tier down to cheaper storage (IA → Glacier Instant Retrieval → Deep
    # Archive). Set to a positive number of days to EXPIRE (delete) archived
    # objects after that age for your org's retention policy; 0 = keep forever
    # (default — never auto-delete the audit archive).
    ARCHIVE_RETENTION_DAYS = 0

    # Frontend deep-link base URL used by alert dispatchers (Slack button,
    # PagerDuty links). Fill in your CloudFront domain after the first
    # frontend stack deploy — leave empty to disable the deep-link.
    FRONTEND_URL = ""

    # PagerDuty dedup key bucket width. Same rule firing within the window
    # groups into one incident; after the window elapses, a fresh incident
    # opens so on-call sees the alert is still active.
    ALERT_DEDUP_WINDOW_MINUTES = 30

    # Slack signing secret — used by /api/slack/interactive to verify v0
    # HMAC signatures on Block Kit ack button posts. Leave empty to
    # disable Slack two-way ack; the endpoint will refuse all calls with
    # a friendly "not configured" message until you set this. Get it
    # from your Slack app's Basic Information → Signing Secret.
    SLACK_SIGNING_SECRET = ""

    # Shared secret for the inbound incident webhook (/api/incident-webhook).
    # Datadog / PagerDuty send it in the X-DBOps-Webhook-Token header; the
    # handler compares it in constant time. Leave empty to disable the endpoint
    # (it returns 503 until set). Use a long random string.
    INCIDENT_WEBHOOK_SECRET = ""

    # Ticketing provider for completed agent tasks (auto-RCA, scheduled
    # reports). "none" (default) keeps ticketing disabled — the task worker's
    # ticketing seam is inert and nothing is created. The provider integration
    # itself is not shipped yet; this is the config switch that will turn it on
    # once a provider is wired (e.g. "jira"). Setting an unwired provider name
    # makes the worker fail the ticketing step loudly rather than silently drop
    # tickets.
    TICKETING_PROVIDER = "none"
