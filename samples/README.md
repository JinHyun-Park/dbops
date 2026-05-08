# Samples

테스트/데모용 리소스. DBOps 플랫폼 배포와는 별도로 관리됩니다.

## Sample Aurora Clusters

`cdk/` 디렉터리에 테스트용 Aurora PostgreSQL + MySQL 클러스터와 부하 생성기가 포함되어 있습니다.

### 배포

```bash
cd samples/cdk
cdk deploy dbops-dev-sample
```

### 정리

```bash
cd samples/cdk
cdk destroy dbops-dev-sample
```

### 포함 내용

- Aurora PostgreSQL 15 Serverless v2 (0.5-2 ACU)
- Aurora MySQL 3.08 Serverless v2 (0.5-2 ACU)
- Lambda 부하 생성기 (2분 주기로 INSERT/SELECT/JOIN 쿼리 실행)
- 샘플 테이블: orders, customers, inventory (PG) / products, sales (MySQL)
