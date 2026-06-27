-- v21: pgvector semantic search for incident similarity (find_similar_incidents).
-- pgvector 0.8 is available on Aurora PG. Embeddings are amazon.titan-embed-text-v2
-- (1024-dim), populated by the incident_embeddings collector; the tool falls back
-- to keyword ILIKE search when a row has no embedding yet or Bedrock is down.
-- ponytail: brute-force cosine (no ANN index) — event_log/runbooks are small, so a
-- sequential scan is fast. Add `USING hnsw (embedding vector_cosine_ops)` if/when
-- row counts make the scan matter.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE runbooks ADD COLUMN IF NOT EXISTS embedding vector(1024);
