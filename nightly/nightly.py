"""Nightly re-scoring for Owl4u (runs in GitHub Actions, CPU only).

1. Read which submission of each ontology is already scored in Cloudflare D1.
2. Fetch the BioPortal catalog and find new ontologies and new submissions.
3. Download and score only those, with the same batch code as the notebook
   (batch/owl4_pipeline.py), including the 1.1 repairs.
4. Write the new rows to D1. If a new submission could not be scored on the
   runner (too large, timed out, failed), the old scores stay and the row notes
   that a manual run on the local GPU machine is needed.

Environment variables:
  BIOPORTAL_API_KEY, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID  (required)
  MAX_ONTOLOGIES   most ontologies to score in one night (default 60; the rest wait for the next night)
  ONLY_ACRONYMS    comma-separated acronyms to re-score regardless of changes (optional)
  NIGHTLY_MAX_DOWNLOAD_GB  skip files larger than this (default 0.75; the 16 GB runner cannot parse the biggest)
  NIGHTLY_RAM_LIMIT_GB     kill an evaluation above this worker RAM (default 13)
  BIOPORTAL_API, CLOUDFLARE_API_BASE  override the API addresses (testing only)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

import owl4_pipeline as P  # noqa: E402

PROVIDER = "bioportal"
KEEP_OLD_WHEN = {"too_large", "timed_out", "failed"}


class D1:
    """Minimal client for the Cloudflare D1 REST API (one statement per call)."""

    def __init__(self) -> None:
        base = os.environ.get("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
        self.url = (f"{base}/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
                    f"/d1/database/{os.environ['D1_DATABASE_ID']}/query")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"

    def query(self, sql: str, params: list | None = None) -> list:
        for attempt in range(5):
            resp = self.session.post(self.url, json={"sql": sql, "params": params or []}, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 * 2 ** attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(f"D1 returned HTTP {resp.status_code}: {resp.text[:300]}") from None
            if not data.get("success"):
                raise RuntimeError(f"D1 error: {data.get('errors')}")
            return data["result"][0].get("results", [])
        raise RuntimeError("D1 kept failing")


SECRETS = {
    "BIOPORTAL_API_KEY": "your BioPortal API key (bioportal.bioontology.org > Account)",
    "CLOUDFLARE_API_TOKEN": "a Cloudflare API token with Account > D1 > Edit permission",
    "CLOUDFLARE_ACCOUNT_ID": "your Cloudflare account ID",
    "D1_DATABASE_ID": "the ID of the owl4u D1 database",
}


def fail(title: str, message: str) -> None:
    """Print a GitHub error annotation, shown at the top of the run page."""
    print(f"::error title={title}::{message}")


def preflight(bioportal_api: str) -> bool:
    """Check secrets and connections before any work, with a plain-English reason on failure."""
    # Pasted secrets often carry a stray line break or space, which breaks HTTP headers.
    for k in SECRETS:
        raw = os.environ.get(k) or ""
        if raw != raw.strip():
            print(f"::warning title=Trimmed secret::{k} had leading or trailing spaces or line breaks; they were removed for this run.")
        os.environ[k] = raw.strip()
        if any(ch.isspace() for ch in os.environ[k]):
            fail("Secret has more than one line", f"{k} contains a space or line break in the middle. Edit the secret and paste only the single value, on one line.")
            return False
    missing = [k for k in SECRETS if not (os.environ.get(k) or "").strip()]
    if missing:
        for k in missing:
            fail("Missing secret", f"{k} is empty. Add {SECRETS[k]} under Settings > Secrets and variables > Actions > New repository secret.")
        return False

    try:
        D1().query("SELECT 1 AS ok")
    except (RuntimeError, requests.RequestException) as e:
        msg = str(e)
        if "10000" in msg or "Authentication" in msg:
            fail("Cloudflare rejected the token", "CLOUDFLARE_API_TOKEN is wrong or lacks permission. Create a token with Account > D1 > Edit and update the secret.")
        elif "7003" in msg or "7404" in msg or "Could not route" in msg or "not found" in msg.lower():
            fail("D1 database not found", "Check CLOUDFLARE_ACCOUNT_ID and D1_DATABASE_ID; one of them does not match your Cloudflare account.")
        else:
            fail("Cannot reach the D1 database", msg[:400])
        return False
    print("Preflight: D1 database reachable.")

    try:
        r = requests.get(f"{bioportal_api.rstrip('/')}/ontologies",
                         params={"display_links": "false", "display_context": "false", "include": "acronym"},
                         headers={"Authorization": f"apikey token={os.environ['BIOPORTAL_API_KEY']}"}, timeout=60)
    except requests.RequestException as e:
        fail("Cannot reach BioPortal", f"{type(e).__name__}: {e}")
        return False
    if r.status_code in (401, 403):
        fail("BioPortal rejected the API key", "BIOPORTAL_API_KEY is wrong. Copy the key from your BioPortal account page and update the secret.")
        return False
    if not r.ok:
        fail("BioPortal error", f"BioPortal returned HTTP {r.status_code}; it may be down. Re-run the workflow later.")
        return False
    print("Preflight: BioPortal key accepted.")
    return True


def main() -> int:
    max_n = int(os.environ.get("MAX_ONTOLOGIES") or 60)
    only = {a.strip().upper() for a in (os.environ.get("ONLY_ACRONYMS") or "").split(",") if a.strip()}
    cfg = {
        "work_dir": str(ROOT / "nightly_work"),
        "bioportal_api": os.environ.get("BIOPORTAL_API", "https://data.bioontology.org"),
        "device": "cpu",
        "max_download_gb": float(os.environ.get("NIGHTLY_MAX_DOWNLOAD_GB") or 0.75),
        "worker_ram_limit_gb": float(os.environ.get("NIGHTLY_RAM_LIMIT_GB") or 13),
        "eval_timeout_min": 60,
        "save_entity_tables": False,
        "keep_awake": False,
        "heartbeat_s": 120,
    }
    if not preflight(cfg["bioportal_api"]):
        return 1
    ctx = P.init(cfg, api_key=os.environ["BIOPORTAL_API_KEY"])
    d1 = D1()

    current = {r["acronym"]: r for r in d1.query(
        "SELECT acronym, submission_id, status FROM ontology_scores WHERE provider = ?", [PROVIDER])}
    print(f"D1 has {len(current)} ontologies.")

    print("Fetching the BioPortal catalog...")
    P.catalog_phase(ctx)
    cat = P.catalog_frame(ctx)

    todo: list = []
    for r in cat.to_dict("records"):
        acr, sub = r["acronym"], int(r["submission_id"] if r["submission_id"] is not None else -1)
        old = current.get(acr)
        if only:
            if acr.upper() in only:
                todo.append((acr, "requested"))
        elif old is None:
            todo.append((acr, "new ontology"))
        elif r["catalog_status"] == "ok" and sub != int(old["submission_id"] if old["submission_id"] is not None else -1):
            todo.append((acr, f"new submission ({old['submission_id']} -> {sub})"))
    deferred = todo[max_n:]
    todo = todo[:max_n]
    acrs = [a for a, _ in todo]
    print(f"{len(todo)} to score tonight, {len(deferred)} deferred to the next run.")
    for a, why in todo:
        print(f"  {a:<20} {why}")

    if acrs:
        P.download_phase(ctx, only=acrs)
        P.evaluate_phase(ctx, only=acrs)
        P.repair_phase(ctx)

    df = P.results_frame(ctx)
    df = df[df.acronym.isin(acrs)]
    updated, kept, errors = [], [], []
    for r in df.to_dict("records"):
        row = P.d1_row(r)
        acr = row["acronym"]
        old = current.get(acr)
        try:
            if row["status"] in KEEP_OLD_WHEN and old and old.get("status") == "scored":
                d1.query("UPDATE ontology_scores SET status_detail = ?, updated_at = ? WHERE provider = ? AND acronym = ?",
                         [f"Submission {row['submission_id']} is not scored yet ({row['status']} on the nightly runner); "
                          f"the scores shown are for submission {old['submission_id']}. Run it on the GPU machine.",
                          P.now(), PROVIDER, acr])
                kept.append((acr, row["status"]))
                continue
            cols = list(row)
            d1.query(f"INSERT OR REPLACE INTO ontology_scores ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
                     [row[c] for c in cols])
            try:
                d1.query("DELETE FROM ontology_search WHERE provider = ? AND acronym = ?", [PROVIDER, acr])
                d1.query("INSERT INTO ontology_search (provider, acronym, name, description, categories) VALUES (?, ?, ?, ?, ?)",
                         [PROVIDER, acr, row["name"], row["description"], row["categories"]])
            except RuntimeError as exc:
                print(f"  (full-text index not updated for {acr}: {exc})")
            updated.append((acr, row["status"], row.get("core_average_bp")))
        except Exception as exc:  # noqa: BLE001
            errors.append((acr, str(exc)[:200]))

    P.export_phase(ctx)
    report = ["## Owl4u nightly re-scoring", "",
              f"- Ontologies in D1 before: {len(current)}",
              f"- Scored or updated tonight: {len(updated)}",
              f"- Kept old scores (new submission needs the GPU machine): {len(kept)}",
              f"- Deferred to the next run: {len(deferred)}",
              f"- Errors writing to D1: {len(errors)}", ""]
    if updated:
        report += ["| Ontology | Status | Average |", "|---|---|---|"]
        report += [f"| {a} | {s} | {'' if v is None else f'{v:.2f}'} |" for a, s, v in updated]
        report.append("")
    if kept:
        report += ["Run these on the GPU machine (notebook, `only=[...]`): " + ", ".join(f"{a} ({s})" for a, s in kept), ""]
    if errors:
        report += ["Errors:", *[f"- {a}: {e}" for a, e in errors], ""]
    text = "\n".join(report)
    print(text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    (ROOT / "nightly_work" / "exports" / "nightly_report.md").write_text(text, encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
