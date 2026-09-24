# Owl4u

Find a BioPortal ontology for a topic, ranked by match and by quality.

Owl4u scores every public BioPortal ontology with the four WiseOwl core metrics (Describe, Define, Connection, Flat) and serves them through a keyword search page. BioPortal finds the ontologies that match a keyword; Owl4u adds their precomputed quality scores, so the page answers in under 2 seconds.

## Repository layout

| Folder | What it holds |
|---|---|
| `batch/` | The scoring notebook (`WiseOwl_4metric_BioPortal_batch_v1_1.ipynb`) and its three modules. The notebook writes the modules on each run, so its code and the `.py` files are the same. Run it on the GPU machine for a full scoring run. |
| `data/` | Exported results of each full run. |
| `web/` | The Cloudflare Worker (search API) and the web page. |
| `nightly/` | The nightly re-scoring script used by GitHub Actions. |
| `.github/workflows/` | The nightly schedule. |

See `DEPLOY.md` to publish the page and turn on the nightly job.

## Scores

- `define_score` and `core_average` are strict WiseOwl scores, identical to WiseOwl 0.10.0 (checked on WiseOwl's own test ontologies at the start of every run).
- `define_bp_score` and `core_average_bp` also read each ontology's own definition property (for example NCIT's). The page ranks by `core_average_bp`.
