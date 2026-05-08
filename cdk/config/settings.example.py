class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    COGNITO_DOMAIN_PREFIX = "dbops-dev"
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4
