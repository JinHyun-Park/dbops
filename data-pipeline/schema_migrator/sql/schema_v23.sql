-- v23: VPC / network context for the DB Map architecture view. The Map nests
-- Region → VPC → DB from these. Populated by meta_collector via
-- describe_db_subnet_groups (Aurora / DocumentDB) and the ElastiCache collector
-- via describe_cache_subnet_groups. DynamoDB stays NULL (serverless, no VPC) and
-- renders in the Map's regional/serverless lane.
ALTER TABLE cluster_meta
  ADD COLUMN IF NOT EXISTS vpc_id VARCHAR(40),
  ADD COLUMN IF NOT EXISTS availability_zones VARCHAR(255);
