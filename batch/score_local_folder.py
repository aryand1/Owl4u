"""Recursively score ontology files from a local folder.

This entry point registers supported ontology files with the resumable Owl4u
pipeline, evaluates them with WiseOwl unless ``--fair-only`` is supplied, and
exports the Owl4u FAIR assessment for every registered file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import owl4_pipeline as pipeline


SUPPORTED_SUFFIXES = {
    ".owl", ".rdf", ".ttl", ".xml", ".nt", ".n3", ".jsonld", ".trig",
    ".trix", ".obo", ".zip", ".gz",
}


def is_ontology_candidate(path: Path) -> bool:
    """Reject container metadata while retaining RDF/OWL XML documents.

    ``.xml`` is deliberately treated more carefully than ontology-specific
    suffixes.  Office Open XML packages also contain many XML files (for
    example ``xl/styles.xml``), but those files are not semantic artefacts.
    """
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        return False
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return "[Content_Types].xml" not in archive.namelist()
        except (OSError, zipfile.BadZipFile):
            return True
    if suffix != ".xml":
        return True
    try:
        head = path.read_bytes()[:65536].decode("utf-8", errors="ignore").casefold()
    except OSError:
        return False
    markers = (
        "<rdf:rdf", "<owl:ontology", "<ontology",
        "<trix", "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2002/07/owl#",
    )
    return any(marker in head for marker in markers)


def discover_files(root: Path) -> list[Path]:
    """Return supported ontology/archive files below ``root`` in stable order."""
    return sorted(
        (path.resolve() for path in root.rglob("*")
         if path.is_file() and is_ontology_candidate(path)),
        key=lambda path: path.as_posix().casefold(),
    )


def stable_acronym(root: Path, path: Path) -> str:
    """Build a readable, collision-resistant identifier from a relative path."""
    relative = path.relative_to(root).as_posix()
    readable = re.sub(r"[^A-Za-z0-9]+", "_", str(Path(relative).with_suffix(""))).strip("_").upper()
    digest = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()[:10].upper()
    return f"LOCAL_{(readable or 'ONTOLOGY')[:48]}_{digest}"


def _path_key(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def prune_false_registrations(ctx: pipeline.Ctx, root: Path, files: list[Path]) -> int:
    """Remove local rows that an older run mistook for ontology XML files."""
    accepted = {_path_key(path) for path in files}
    root_key = _path_key(root)
    doomed: list[tuple[str, int]] = []
    for row in ctx.state.q(
        "SELECT d.acronym, d.submission_id, d.raw_path, c.ontology_json "
        "FROM downloads d JOIN catalog c ON c.acronym=d.acronym"
    ):
        raw_path = row.get("raw_path")
        if not raw_path:
            continue
        try:
            local_file = bool(json.loads(row.get("ontology_json") or "{}").get("local_file"))
            path_key = _path_key(raw_path)
            inside_root = os.path.commonpath((root_key, path_key)) == root_key
        except (OSError, ValueError, TypeError):
            continue
        if local_file and inside_root and path_key not in accepted and not is_ontology_candidate(Path(raw_path)):
            doomed.append((row["acronym"], int(row["submission_id"])))
    if not doomed:
        return 0
    with ctx.state.conn:
        for acronym, submission_id in doomed:
            ctx.state.conn.execute(
                "DELETE FROM evaluations WHERE acronym=? AND submission_id=?", (acronym, submission_id)
            )
            ctx.state.conn.execute(
                "DELETE FROM downloads WHERE acronym=? AND submission_id=?", (acronym, submission_id)
            )
            ctx.state.conn.execute("DELETE FROM catalog WHERE acronym=?", (acronym,))
    ctx.event("local", None, "INFO", f"removed {len(doomed)} non-ontology XML registrations")
    return len(doomed)


def register_files(ctx: pipeline.Ctx, root: Path, files: list[Path]) -> tuple[list[str], dict[str, int]]:
    """Register files while preserving completed converted/repaired inputs."""
    import owl4_pipeline as pipeline

    downloads = ctx.state.q("SELECT acronym, submission_id, raw_path FROM downloads")
    catalog = ctx.state.q("SELECT acronym, name FROM catalog")
    evaluations = {
        (row["acronym"], row["submission_id"]): row["status"]
        for row in ctx.state.q("SELECT acronym, submission_id, status FROM evaluations")
    }
    by_path = {_path_key(row["raw_path"]): row for row in downloads if row.get("raw_path")}
    by_name: dict[str, list[str]] = defaultdict(list)
    for row in catalog:
        if row.get("name"):
            by_name[str(row["name"]).casefold()].append(row["acronym"])

    targets: list[str] = []
    counts = {"discovered": len(files), "registered": 0, "reused": 0, "already_done": 0}
    for path in files:
        existing = by_path.get(_path_key(path))
        acronym = existing["acronym"] if existing else None
        submission_id = int(existing["submission_id"]) if existing else 0
        if acronym is None:
            same_name = by_name.get(path.stem.casefold(), [])
            if len(same_name) == 1:
                acronym = same_name[0]
        if acronym is None:
            acronym = stable_acronym(root, path)

        targets.append(acronym)
        if evaluations.get((acronym, submission_id)) == "done":
            counts["already_done"] += 1
            continue
        pipeline.add_local_file(ctx, str(path), acronym, submission_id=submission_id, name=path.stem)
        counts["reused" if existing else "registered"] += 1
    return sorted(set(targets)), counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Folder containing ontology files (searched recursively).")
    parser.add_argument("--work-dir", type=Path, default=Path.home() / "owl4_local_run",
                        help="Resumable state and export folder (default: ~/owl4_local_run).")
    parser.add_argument("--fair-only", action="store_true",
                        help="Register and export FAIR assessments without running WiseOwl evaluation.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Evaluate at most this many pending ontologies in this invocation.")
    parser.add_argument("--timeout-min", type=float, default=None,
                        help="Per-ontology evaluation timeout in minutes.")
    return parser


def main() -> int:
    import owl4_pipeline as pipeline

    args = build_parser().parse_args()
    root = args.source.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Source folder does not exist: {root}")
    files = discover_files(root)
    if not files:
        suffixes = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise SystemExit(f"No supported ontology files found under {root}. Supported: {suffixes}")

    ctx = pipeline.init({
        "work_dir": str(args.work_dir.expanduser().resolve()),
        "provider": "local",
        "device": "auto",
        "keep_awake": True,
    })
    pruned = prune_false_registrations(ctx, root, files)
    targets, registration = register_files(ctx, root, files)
    print(f"Source: {root}")
    print(f"Ignored/pruned non-ontology files: {pruned}")
    print(f"Registration: {registration}")
    if args.fair_only:
        print("FAIR-only mode: WiseOwl evaluation skipped.")
    else:
        print(pipeline.evaluate_phase(ctx, only=targets, limit=args.limit,
                                      timeout_min=args.timeout_min))
    exported = pipeline.export_phase(ctx)
    print(f"Exports: {exported['export_dir']}")
    print(f"Statuses: {exported['web_status_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
