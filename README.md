# Owl4u

Find a BioPortal ontology for a topic, ranked by match and by quality.

Owl4u scores every public BioPortal ontology with the four WiseOwl core metrics (Describe, Define, Connection, Flat), adds a FAIR metadata/publication assessment, and serves them through a keyword search page. BioPortal finds the ontologies that match a keyword; Owl4u adds the precomputed scores, so the page answers in under 2 seconds.

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
- `fair_score` is a 0–100 assessment across Findable, Accessible, Interoperable, and Reusable. `batch/owl4_fair.py` evaluates the 61 O'FAIRe questions with the published 478-credit weighting, using BioPortal metadata plus evidence extracted from the ontology file. The output includes the four principle scores, all 15 FAIR sub-principles, evidence, and improvement recommendations.
- The method is identified as the **Owl4u FAIR profile** (`owl4-fair-1.1`). It is O'FAIRe-aligned but deliberately offline and reproducible: live URL resolution, content negotiation, search-engine indexing, and checks needing human judgement are marked `not_tested` and receive no credit. It does not claim to be a response from the hosted O'FAIRe service.

The full structured result is stored in the CSV export's `fair_assessment_json`; D1 keeps a compact principle/sub-principle summary and recommendations so searches stay small. The scalar columns make sorting and querying inexpensive. The shape follows FAIR-O's assessment → principle → sub-principle → evidence model, while the numeric values remain compatible with the O'FAIRe-style 0–100 presentation.

## Local ontology folders

`batch/score_local_folder.py` recursively discovers OWL/RDF files and archives, registers them in a resumable work directory, evaluates them, and exports WiseOwl plus FAIR scores. For example on Windows:

```bat
python batch\score_local_folder.py "C:\path\to\ontologies" --work-dir "C:\owl4_local_run"
```

Add `--fair-only` to register and export FAIR assessments without running the GPU-backed WiseOwl evaluation. Local files do not receive BioPortal-specific repository or accessibility credits; those checks require evidence of a real external metadata record and access protocol.

Method references: [O'FAIRe](https://github.com/agroportal/fairness), [FAIR-O](https://w3id.org/fair-o/1.0.0), and the [FAIR principles](https://www.go-fair.org/fair-principles/).
