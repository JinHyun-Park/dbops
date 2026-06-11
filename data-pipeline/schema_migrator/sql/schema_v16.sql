-- v16: 비관계형 엔진(DocumentDB·DynamoDB)을 위한 중립 리소스 메타 컬럼.
--
-- cluster_meta는 RDS 모양(instance_class·storage_size_gb·max_connections·
-- serverlessv2_*)이라 DynamoDB 테이블 메타(billing_mode·item_count·
-- table_size_bytes·GSI·streams·TTL·PITR)나 DocumentDB 인스턴스 목록이 들어갈
-- 자리가 없다. 관계형 행은 기존 타입 컬럼을 그대로 쓰고, 비관계형 행은
-- 엔진별 메타를 이 JSONB에 담는다. NULL = 관계형(또는 미수집).

ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS resource_details JSONB;
