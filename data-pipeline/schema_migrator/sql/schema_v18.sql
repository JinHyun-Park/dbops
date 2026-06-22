-- schema_v18 — per-instance comparison: cluster member list on cluster_meta.
-- Holds [{"id":"<DBInstanceIdentifier>","role":"writer|reader","class":"db.r6g.large"}]
-- so the Compare "instance" mode can populate its A/B pickers without a live
-- RDS describe. Populated each cycle by the meta collector.
ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS instances JSONB;
