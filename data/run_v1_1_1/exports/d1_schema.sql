-- Cloudflare D1 / SQLite schema for the web search endpoint.
CREATE TABLE IF NOT EXISTS ontology_scores (
  provider TEXT NOT NULL,
  acronym TEXT NOT NULL,
  name TEXT,
  description TEXT,
  categories TEXT,
  ontology_language TEXT,
  submission_id INTEGER,
  version TEXT,
  released TEXT,
  bioportal_url TEXT,
  file_sha256 TEXT,
  status TEXT NOT NULL,
  status_detail TEXT,
  describe_score REAL,
  define_score REAL,
  define_bp_score REAL,
  define_bp_source TEXT,
  connection_score REAL,
  flat_score REAL,
  core_average REAL,
  core_average_bp REAL,
  logical_consistency_score REAL,
  structural_dist_score REAL,
  semantic_dist_score REAL,
  triple_count INTEGER,
  entity_count INTEGER,
  class_count INTEGER,
  wiseowl_version TEXT,
  core4_version TEXT,
  device TEXT,
  evaluated_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (provider, acronym)
);
CREATE INDEX IF NOT EXISTS idx_scores_avg ON ontology_scores(core_average);
CREATE INDEX IF NOT EXISTS idx_scores_avg_bp ON ontology_scores(core_average_bp);
CREATE INDEX IF NOT EXISTS idx_scores_status ON ontology_scores(status);
CREATE TABLE IF NOT EXISTS keyword_cache (
  keyword TEXT NOT NULL,
  provider TEXT NOT NULL,
  results_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (keyword, provider)
);
