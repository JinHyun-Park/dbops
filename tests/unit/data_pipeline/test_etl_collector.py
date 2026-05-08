from unittest.mock import MagicMock
from data_pipeline.etl_collector.collectors.meta_collector import collect_cluster_meta


def test_collect_cluster_meta_stores_in_cache():
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "DBClusterIdentifier": "prod-pg-1",
            "Engine": "aurora-postgresql",
            "EngineVersion": "15.10",
            "Status": "available",
            "Endpoint": "prod-pg-1.cluster-xxx.ap-northeast-2.rds.amazonaws.com",
            "ReaderEndpoint": "prod-pg-1.cluster-ro-xxx.ap-northeast-2.rds.amazonaws.com",
            "AllocatedStorage": 100,
        }]
    }
    mock_cache_execute = MagicMock()

    result = collect_cluster_meta(
        rds_client=mock_rds,
        cache_execute=mock_cache_execute,
        cluster_id="prod-pg-1",
        account_id="123456789012",
        region="ap-northeast-2",
    )

    assert result["status"] == "available"
    mock_cache_execute.assert_called_once()
