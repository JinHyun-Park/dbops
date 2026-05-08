from mcp_servers.shared.cache_client import CacheClient


def find_similar_incidents_impl(
    cache: CacheClient,
    cluster_id: str,
    symptoms: str,
) -> dict:
    return {
        "cluster_id": cluster_id,
        "symptoms": symptoms,
        "similar_incidents": [],
        "note": "Bedrock KB retrieve integration - requires STRANDS_KNOWLEDGE_BASE_ID env var",
    }
