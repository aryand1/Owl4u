# Output files

Generated 2026-09-23T23:25:10+00:00 by owl4-batch-1.1.1.

## exports/

- `ontology_results_full.csv`: Every ontology: BioPortal metadata, download, evaluation, all summary fields.
- `ontology_results_full.parquet`: Same as the CSV, typed columns (needs pyarrow).
- `scores_compact.csv`: One row per ontology with the four scores and web status.
- `timings_and_memory.csv`: Per-ontology size, stage timings, RAM and GPU use (the benchmark table).
- `not_scored.csv`: Every ontology without scores and the reason.
- `results_detail.jsonl`: Full nested result.json of each scored ontology, one per line.
- `events.csv`: Log of warnings, errors and phase summaries across all sessions.
- `sessions.csv`: Every notebook session: config and environment.
- `search_benchmark_all.csv`: BioPortal /search and /recommender latency per keyword.
- `data_dictionary.csv`: Every column in ontology_results_full.csv with type, fill count, meaning.
- `d1_schema.sql`: CREATE TABLE statements for Cloudflare D1 (includes empty columns for the 3 later metrics).
- `d1_data.sql`: INSERT OR REPLACE rows for ontology_scores.
- `d1_fts.sql`: Optional full-text search table (fallback search).
- `d1_fts_data.sql`: Rows for the full-text search table.
- `run_summary.json`: Counts, score distributions, total time, config.

## Other folders

- `state.sqlite`: the resume database (catalog, downloads, evaluations, events, runs, search_benchmark).
- `raw/<ACRONYM>/`: raw BioPortal JSON (ontology record, latest submission, metrics).
- `raw/ontologies_list.json`: the full BioPortal listing at catalog time.
- `downloads/<ACRONYM>/<submission>/`: downloaded files (and `extracted/` for zip archives).
- `results/<ACRONYM>/<submission>/result.json`: full nested result for one ontology.
- `results/<ACRONYM>/<submission>/entities.csv.gz`: one row per entity with every per-entity value.
- `logs/pipeline.log`, `logs/worker.log`: text logs.
- `environment/environment_<run>.json`: hardware and library versions per session.
- `parity/`: WiseOwl test files and outputs used for the parity check.
