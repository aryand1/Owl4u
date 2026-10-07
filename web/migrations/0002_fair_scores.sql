-- Apply once to a D1 database created by Owl4u before FAIR scoring was added.
ALTER TABLE ontology_scores ADD COLUMN fair_score REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_findable REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_accessible REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_interoperable REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_reusable REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_credits REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_max_credits REAL;
ALTER TABLE ontology_scores ADD COLUMN fair_version TEXT;
ALTER TABLE ontology_scores ADD COLUMN fair_assessment_json TEXT;
CREATE INDEX IF NOT EXISTS idx_scores_fair ON ontology_scores(fair_score);
