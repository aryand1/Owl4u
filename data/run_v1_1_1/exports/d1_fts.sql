-- Optional full-text index for the local fallback search.
CREATE VIRTUAL TABLE IF NOT EXISTS ontology_search USING fts5(provider, acronym, name, description, categories);
