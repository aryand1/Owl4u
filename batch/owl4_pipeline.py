"""owl4_pipeline: BioPortal catalog, downloads, resumable evaluation, and exports.

Everything that happens is written to a SQLite file (state.sqlite) right away,
so the run can stop at any point (crash, restart, Ctrl+C, power loss) and
continue from where it left off when the same cell is run again.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import platform
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

import owl4_core as core

NOTEBOOK_VERSION = "owl4-batch-1.0"

DEFAULT_CONFIG: dict = {
    # where everything goes
    "work_dir": str(Path.home() / "owl4_run"),
    # BioPortal
    "provider": "bioportal",
    "bioportal_api": "https://data.bioontology.org",
    "bioportal_ui": "https://bioportal.bioontology.org/ontologies",
    "requests_per_second": 5.0,
    "http_timeout_s": 60,
    "download_read_timeout_s": 600,
    "max_http_retries": 5,
    "include_views": False,
    "download_format": "rdf",
    "download_fallback_original": True,
    "max_download_gb": None,
    "keep_downloads": True,
    # model and device
    "device": "auto",
    "bert_name": "bert-base-uncased",
    "bert_max_length": 128,
    "batch_size_gpu": 128,
    "batch_size_cpu": 32,
    "mixed_precision": False,
    "cpu_threads": 0,
    "define_vectors_on_gpu_max_gb": 4.0,
    "define_cpu_fallback": True,
    # WiseOwl metric settings (same defaults as WiseOwl config.toml)
    "define_min_tokens": 12,
    "depth_target": 5,
    "branch_target": 3,
    "suggestion_threshold": 4.0,
    # safety limits per ontology
    "eval_timeout_min": 120,
    "worker_ram_limit_gb": None,
    "max_attempts": 2,
    "worker_ready_timeout_s": 1800,
    "heartbeat_s": 60,
    # extra outputs
    "save_entity_tables": True,
    "entity_rows_max": None,
    "keep_awake": True,
}

RETRYABLE_EVAL = {"error", "crashed", "interrupted", "memory_error"}
TERMINAL_EVAL = {"done", "parse_failed", "timeout", "memory_limit", "gave_up", "skipped"}

log = logging.getLogger("owl4.pipeline")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(text))[:120]


# =============================================================================
# State database
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, phase TEXT,
  notebook_version TEXT, config_json TEXT, env_json TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS catalog(
  acronym TEXT PRIMARY KEY, name TEXT, submission_id INTEGER, status TEXT, error TEXT,
  fetched_at TEXT, api_seconds REAL,
  ontology_json TEXT, submission_json TEXT, metrics_json TEXT);
CREATE TABLE IF NOT EXISTS downloads(
  acronym TEXT, submission_id INTEGER, status TEXT, variant TEXT, http_status INTEGER,
  url TEXT, raw_path TEXT, eval_path TEXT, bytes INTEGER, eval_bytes INTEGER, sha256 TEXT,
  content_type TEXT, server_filename TEXT, sniffed_format TEXT, archive_type TEXT,
  archive_members TEXT, attempts INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT,
  seconds REAL, error TEXT, attempts_log TEXT, run_id TEXT,
  PRIMARY KEY(acronym, submission_id));
CREATE TABLE IF NOT EXISTS evaluations(
  acronym TEXT, submission_id INTEGER, status TEXT, attempts INTEGER DEFAULT 0,
  started_at TEXT, finished_at TEXT, seconds REAL, error_type TEXT, error TEXT,
  traceback TEXT, stage_at_failure TEXT,
  describe_score REAL, define_score REAL, connection_score REAL, flat_score REAL,
  core_average REAL, summary_json TEXT, result_path TEXT, entities_path TEXT,
  parent_peak_rss_mb REAL, timeout_min REAL, run_id TEXT,
  PRIMARY KEY(acronym, submission_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, phase TEXT,
  acronym TEXT, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS search_benchmark(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT, endpoint TEXT, keyword TEXT,
  http_status INTEGER, seconds REAL, result_count INTEGER, distinct_ontologies INTEGER,
  top_ontologies TEXT, error TEXT);
"""


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def exec(self, sql: str, args: tuple = ()) -> None:
        self.conn.execute(sql, args)
        self.conn.commit()

    def upsert(self, table: str, row: dict, keys: tuple) -> None:
        cols = list(row)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT({','.join(keys)}) DO UPDATE SET "
               + ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys))
        self.conn.execute(sql, tuple(row[c] for c in cols))
        self.conn.commit()


# =============================================================================
# Context
# =============================================================================

@dataclass
class Ctx:
    cfg: dict
    work: Path
    state: State
    run_id: str
    api_key: str | None = None
    _api: Any = None
    paths: dict = field(default_factory=dict)

    @property
    def api(self) -> "BioPortal":
        if self._api is None:
            if not self.api_key:
                raise RuntimeError("No BioPortal API key. Set it in the API key cell.")
            self._api = BioPortal(self.api_key, self.cfg)
        return self._api

    def event(self, phase: str, acronym: str | None, level: str, message: str) -> None:
        self.state.conn.execute(
            "INSERT INTO events(ts, run_id, phase, acronym, level, message) VALUES (?,?,?,?,?,?)",
            (now(), self.run_id, phase, acronym, level, message[:4000]))
        self.state.conn.commit()
        getattr(log, level.lower() if level.lower() in ("info", "warning", "error") else "info")(
            "[%s] %s: %s", phase, acronym or "-", message)


def init(cfg_overrides: dict | None = None, api_key: str | None = None) -> Ctx:
    cfg = {**DEFAULT_CONFIG, **(cfg_overrides or {})}
    work = Path(cfg["work_dir"]).expanduser().resolve()
    paths = {name: work / name for name in
             ("logs", "raw", "downloads", "results", "exports", "environment")}
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("owl4")
    logger.setLevel(logging.INFO)
    log_file = str(paths["logs"] / "pipeline.log")
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == log_file
               for h in logger.handlers):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(fh)
    state = State(work / "state.sqlite")
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    state.exec("INSERT INTO runs(run_id, started_at, phase, notebook_version, config_json) VALUES (?,?,?,?,?)",
               (run_id, now(), "init", NOTEBOOK_VERSION, json.dumps(cfg, default=str)))
    ctx = Ctx(cfg=cfg, work=work, state=state, run_id=run_id, api_key=api_key, paths=paths)
    ctx.event("init", None, "INFO", f"session started, work_dir={work}")
    return ctx


# =============================================================================
# Environment report
# =============================================================================

def environment_report(ctx: Ctx) -> dict:
    env: dict = {"time": now(), "run_id": ctx.run_id, "notebook_version": NOTEBOOK_VERSION,
                 "python": sys.version, "executable": sys.executable,
                 "platform": platform.platform(), "machine": platform.machine(),
                 "processor": platform.processor(), "cpu_count": os.cpu_count(),
                 "conda_env": os.environ.get("CONDA_DEFAULT_ENV")}
    try:
        import psutil
        vm = psutil.virtual_memory()
        env["ram_total_gb"] = round(vm.total / 2**30, 1)
        env["ram_available_gb"] = round(vm.available / 2**30, 1)
        du = shutil.disk_usage(str(ctx.work))
        env["disk_free_gb"] = round(du.free / 2**30, 1)
    except Exception as exc:  # noqa: BLE001
        env["psutil_error"] = str(exc)
    for mod in ("torch", "transformers", "rdflib", "numpy", "pandas", "requests", "psutil", "pyarrow"):
        try:
            env[f"{mod}_version"] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            env[f"{mod}_version"] = None
    try:
        import torch
        env["cuda_available"] = torch.cuda.is_available()
        env["torch_cuda_build"] = torch.version.cuda
        env["torch_arch_list"] = torch.cuda.get_arch_list() if torch.cuda.is_available() else []
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            env["gpu_name"] = props.name
            env["gpu_memory_gb"] = round(props.total_memory / 2**30, 1)
            env["gpu_capability"] = f"{props.major}.{props.minor}"
            arch = f"sm_{props.major}{props.minor}"
            env["gpu_arch_supported_by_this_torch"] = arch in env["torch_arch_list"]
            env["bf16_supported"] = torch.cuda.is_bf16_supported()
    except Exception as exc:  # noqa: BLE001
        env["torch_error"] = str(exc)
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        env["nvidia_smi"] = out.stdout.strip() or out.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        env["nvidia_smi"] = f"not available: {exc}"
    path = ctx.paths["environment"] / f"environment_{ctx.run_id}.json"
    core.dump_json(env, str(path))
    ctx.state.exec("UPDATE runs SET env_json=? WHERE run_id=?", (json.dumps(env, default=str), ctx.run_id))
    return env


# =============================================================================
# BioPortal client
# =============================================================================

class AuthError(RuntimeError):
    pass


class BioPortal:
    def __init__(self, api_key: str, cfg: dict) -> None:
        self.base = cfg["bioportal_api"].rstrip("/")
        self.cfg = cfg
        self.min_interval = 1.0 / max(0.1, float(cfg["requests_per_second"]))
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"apikey token={api_key}",
            "Accept": "application/json",
            "User-Agent": f"{NOTEBOOK_VERSION} (WiseOwl batch evaluation)",
        })

    def _wait(self) -> None:
        delay = self._last + self.min_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def request(self, path: str, params: dict | None = None, *, stream: bool = False,
                read_timeout: float | None = None) -> tuple:
        """Returns (response or None, info). Retries 429/5xx/network errors."""
        url = path if path.startswith("http") else f"{self.base}{path}"
        info: dict = {"url": url, "params": params or {}, "tries": []}
        retries = int(self.cfg["max_http_retries"])
        for attempt in range(retries + 1):
            self._wait()
            t0 = time.perf_counter()
            try:
                resp = self.session.get(url, params=params, stream=stream,
                                        timeout=(30, read_timeout or self.cfg["http_timeout_s"]))
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                info["tries"].append({"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                      "seconds": round(time.perf_counter() - t0, 3)})
                if attempt < retries:
                    time.sleep(min(60, 2 ** attempt) + random.random())
                    continue
                info["error"] = info["tries"][-1]["error"]
                return None, info
            info["tries"].append({"status": resp.status_code,
                                  "seconds": round(time.perf_counter() - t0, 3)})
            info["status"] = resp.status_code
            info["seconds"] = info["tries"][-1]["seconds"]
            if resp.status_code == 401:
                raise AuthError("BioPortal returned 401: check the API key.")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = resp.headers.get("Retry-After")
                try:
                    wait_s = float(wait) if wait else min(60, 2 ** attempt)
                except ValueError:
                    wait_s = min(60, 2 ** attempt)
                resp.close()
                time.sleep(wait_s + random.random())
                continue
            return resp, info
        return None, info

    def get_json(self, path: str, params: dict | None = None) -> tuple:
        resp, info = self.request(path, params)
        if resp is None:
            return None, info
        if resp.status_code != 200:
            info["body"] = resp.text[:1000]
            return None, info
        try:
            return resp.json(), info
        except ValueError:
            info["body"] = resp.text[:1000]
            info["error"] = "response was not JSON"
            return None, info


def _tail(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("acronym") or value.get("name") or value.get("@id") or ""
    return str(value).rstrip("/").split("/")[-1]


# =============================================================================
# Phase A: catalog
# =============================================================================

def catalog_phase(ctx: Ctx, refresh: bool = False, only: list | None = None,
                  limit: int | None = None) -> dict:
    """List BioPortal ontologies and store metadata, latest submission, and metrics."""
    api = ctx.api
    params = {"display": "all", "display_context": "false", "display_links": "false"}
    if ctx.cfg["include_views"]:
        params["also_include_views"] = "true"
    listing, info = api.get_json("/ontologies", params)
    if listing is None:
        ctx.event("catalog", None, "WARNING", f"display=all listing failed ({info.get('status')}); retrying plain")
        listing, info = api.get_json("/ontologies", {"display_context": "false", "display_links": "false"})
    if listing is None:
        raise RuntimeError(f"Could not list ontologies: {info}")
    core.dump_json(listing, str(ctx.paths["raw"] / "ontologies_list.json"))
    ctx.event("catalog", None, "INFO", f"{len(listing)} ontologies listed")

    todo = sorted(listing, key=lambda o: o.get("acronym", ""))
    if only:
        wanted = {a.upper() for a in only}
        todo = [o for o in todo if str(o.get("acronym", "")).upper() in wanted]
    if limit:
        todo = todo[:limit]
    done_rows = {r["acronym"] for r in ctx.state.q("SELECT acronym FROM catalog WHERE status IN ('ok','no_submission')")}
    counts: dict = {}
    t_start = time.time()
    for i, onto in enumerate(todo, 1):
        acr = onto.get("acronym")
        if not acr or (not refresh and acr in done_rows):
            counts["skipped_already_done"] = counts.get("skipped_already_done", 0) + 1
            continue
        t0 = time.perf_counter()
        raw_dir = ctx.paths["raw"] / safe_name(acr)
        raw_dir.mkdir(exist_ok=True)
        core.dump_json(onto, str(raw_dir / "ontology.json"))
        status, error, sub_id = "ok", None, -1
        try:
            sub, sinfo = api.get_json(f"/ontologies/{acr}/latest_submission",
                                      {"display": "all", "display_context": "false", "display_links": "false"})
            if not sub or not isinstance(sub, dict) or sub.get("submissionId") is None:
                status = "no_submission"
                error = f"latest_submission http={sinfo.get('status')} {str(sinfo.get('body',''))[:200]}"
                sub = sub if isinstance(sub, dict) else None
            else:
                sub_id = int(sub["submissionId"])
                core.dump_json(sub, str(raw_dir / "latest_submission.json"))
            metrics = None
            if status == "ok":
                metrics, _ = api.get_json(f"/ontologies/{acr}/metrics", {"display_context": "false", "display_links": "false"})
                if metrics is not None:
                    core.dump_json(metrics, str(raw_dir / "metrics.json"))
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            status, error, sub, metrics = "error", f"{type(exc).__name__}: {exc}", None, None
        ctx.state.upsert("catalog", {
            "acronym": acr, "name": onto.get("name"), "submission_id": sub_id, "status": status,
            "error": error, "fetched_at": now(), "api_seconds": round(time.perf_counter() - t0, 3),
            "ontology_json": json.dumps(onto), "submission_json": json.dumps(sub) if sub else None,
            "metrics_json": json.dumps(metrics) if metrics else None}, ("acronym",))
        counts[status] = counts.get(status, 0) + 1
        if i % 50 == 0 or i == len(todo):
            print(f"  catalog {i}/{len(todo)}  {counts}  ({time.time() - t_start:.0f}s)")
    ctx.event("catalog", None, "INFO", f"catalog phase finished: {counts}")
    return counts


def catalog_frame(ctx: Ctx):
    """Catalog as a DataFrame with the useful BioPortal fields pulled out."""
    import pandas as pd
    rows = []
    for r in ctx.state.q("SELECT * FROM catalog"):
        o = json.loads(r["ontology_json"] or "{}")
        s = json.loads(r["submission_json"] or "null") or {}
        m = json.loads(r["metrics_json"] or "null") or {}
        contacts = s.get("contact") or []
        rows.append({
            "acronym": r["acronym"], "name": r["name"], "submission_id": r["submission_id"],
            "catalog_status": r["status"], "catalog_error": r["error"], "catalog_fetched_at": r["fetched_at"],
            "bp_viewing_restriction": o.get("viewingRestriction"),
            "bp_summary_only": o.get("summaryOnly"),
            "bp_flat": o.get("flat"),
            "bp_is_view": bool(o.get("viewOf")),
            "bp_view_of": _tail(o.get("viewOf")) if o.get("viewOf") else None,
            "bp_categories": ";".join(sorted({_tail(x) for x in (o.get("hasDomain") or [])})),
            "bp_groups": ";".join(sorted({_tail(x) for x in (o.get("group") or [])})),
            "bp_ontology_type": _tail(o.get("ontologyType")) if o.get("ontologyType") else None,
            "bp_language": s.get("hasOntologyLanguage"),
            "bp_version": s.get("version"),
            "bp_released": s.get("released"),
            "bp_creation_date": s.get("creationDate"),
            "bp_modification_date": s.get("modificationDate"),
            "bp_submission_status": ";".join(_tail(x) for x in (s.get("submissionStatus") or [])),
            "bp_status": s.get("status"),
            "bp_description": (s.get("description") or "")[:5000],
            "bp_homepage": s.get("homepage"),
            "bp_documentation": s.get("documentation"),
            "bp_publication": s.get("publication") if isinstance(s.get("publication"), str) else json.dumps(s.get("publication")),
            "bp_ontology_uri": s.get("URI"),
            "bp_natural_language": ";".join(map(str, s.get("naturalLanguage") or [])) if isinstance(s.get("naturalLanguage"), list) else s.get("naturalLanguage"),
            "bp_master_file_name": s.get("masterFileName"),
            "bp_pull_location": s.get("pullLocation"),
            "bp_contact_names": ";".join(str(c.get("name")) for c in contacts if isinstance(c, dict)),
            "bp_classes": m.get("classes"), "bp_individuals": m.get("individuals"),
            "bp_properties": m.get("properties"), "bp_max_depth": m.get("maxDepth"),
            "bp_max_child_count": m.get("maxChildCount"), "bp_average_child_count": m.get("averageChildCount"),
            "bp_classes_with_one_child": m.get("classesWithOneChild"),
            "bp_classes_with_more_than_25_children": m.get("classesWithMoreThan25Children"),
            "bp_classes_with_no_definition": m.get("classesWithNoDefinition"),
            "bp_url": f"{ctx.cfg['bioportal_ui']}/{r['acronym']}",
        })
    return pd.DataFrame(rows)


def select_pilot(ctx: Ctx, n: int = 20, seed: int = 7) -> list:
    """Pick ontologies spread over size bins (BioPortal class counts) for a benchmark run."""
    df = catalog_frame(ctx)
    df = df[(df.catalog_status == "ok") & (df.bp_viewing_restriction != "private")
            & (df.bp_summary_only != True) & df.bp_classes.notna()]  # noqa: E712
    bins = [(0, 1_000), (1_000, 20_000), (20_000, 200_000), (200_000, 10**12)]
    rng = random.Random(seed)
    per_bin = max(1, n // len(bins))
    chosen: list = []
    for lo, hi in bins:
        pool = sorted(df[(df.bp_classes >= lo) & (df.bp_classes < hi)].acronym.tolist())
        rng.shuffle(pool)
        chosen += pool[:per_bin]
    rest = sorted(set(df.acronym) - set(chosen))
    rng.shuffle(rest)
    chosen += rest[: max(0, n - len(chosen))]
    return sorted(chosen)


# =============================================================================
# Phase B: downloads
# =============================================================================

def _stream_to_file(resp, dest: Path, max_bytes: int | None) -> tuple:
    digest = hashlib.sha256()
    size = 0
    part = dest.with_name(dest.name + ".part")
    with open(part, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            fh.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            if max_bytes and size > max_bytes:
                fh.close()
                part.unlink(missing_ok=True)
                return None, size
    os.replace(part, dest)
    return digest.hexdigest(), size


def _server_filename(resp) -> str | None:
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else None


def _prepare_eval_file(raw_path: Path, master_name: str | None) -> dict:
    """Unpack zip/gzip if needed and pick the file to evaluate."""
    fmt = core.sniff_format(str(raw_path))
    out = {"eval_path": str(raw_path), "archive_type": None, "archive_members": None, "sniffed_format": fmt}
    if fmt == "zip":
        target = raw_path.parent / "extracted"
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(raw_path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            zf.extractall(target)
        names = [m.filename for m in members]
        pick = None
        if master_name:
            pick = next((m for m in members if Path(m.filename).name == master_name), None)
        if pick is None:
            ok_ext = (".owl", ".rdf", ".ttl", ".xml", ".nt", ".n3", ".jsonld", ".trig", ".obo")
            cands = [m for m in members if m.filename.lower().endswith(ok_ext)] or members
            pick = max(cands, key=lambda m: m.file_size)
        eval_path = target / pick.filename
        out.update(eval_path=str(eval_path), archive_type="zip", archive_members=json.dumps(names[:500]),
                   sniffed_format=core.sniff_format(str(eval_path)))
    elif fmt == "gzip":
        eval_path = raw_path.with_suffix(raw_path.suffix + ".unzipped")
        with gzip.open(raw_path, "rb") as src, open(eval_path, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        out.update(eval_path=str(eval_path), archive_type="gzip",
                   sniffed_format=core.sniff_format(str(eval_path)))
    return out


def download_phase(ctx: Ctx, only: list | None = None, retry_failed: bool = False,
                   limit: int | None = None) -> dict:
    """Download each ontology's latest submission (RDF first, original as fallback)."""
    api = ctx.api
    df = catalog_frame(ctx)
    if df.empty:
        print("Catalog is empty: run the catalog phase first.")
        return {}
    df = df[df.catalog_status == "ok"]
    if only:
        wanted = {a.upper() for a in only}
        df = df[df.acronym.str.upper().isin(wanted)]
    df = df.assign(_size=df.bp_classes.fillna(10**12)).sort_values("_size")
    existing = {(r["acronym"], r["submission_id"]): r for r in ctx.state.q("SELECT * FROM downloads")}
    max_bytes = int(ctx.cfg["max_download_gb"] * 2**30) if ctx.cfg["max_download_gb"] else None
    counts: dict = {}
    rows = df.to_dict("records")
    if limit:
        rows = rows[:limit]
    _keep_awake(ctx, True)
    t_start = time.time()
    try:
        for i, row in enumerate(rows, 1):
            acr, sub = row["acronym"], int(row["submission_id"])
            prev = existing.get((acr, sub))
            if prev:
                ok_on_disk = prev["status"] == "ok" and prev["eval_path"] and Path(prev["eval_path"]).exists()
                if ok_on_disk or (prev["status"] != "ok" and not retry_failed):
                    counts["already_" + prev["status"]] = counts.get("already_" + prev["status"], 0) + 1
                    continue
            base = {"acronym": acr, "submission_id": sub, "run_id": ctx.run_id, "started_at": now(),
                    "attempts": (prev["attempts"] if prev else 0) + 1}
            if row["bp_viewing_restriction"] == "private":
                ctx.state.upsert("downloads", {**base, "status": "private", "finished_at": now()},
                                 ("acronym", "submission_id"))
                counts["private"] = counts.get("private", 0) + 1
                continue
            if str(row["bp_summary_only"]).lower() == "true":
                ctx.state.upsert("downloads", {**base, "status": "summary_only", "finished_at": now()},
                                 ("acronym", "submission_id"))
                counts["summary_only"] = counts.get("summary_only", 0) + 1
                continue
            dest_dir = ctx.paths["downloads"] / safe_name(acr) / str(sub)
            dest_dir.mkdir(parents=True, exist_ok=True)
            variants = []
            fmt = ctx.cfg["download_format"]
            if fmt:
                variants.append((f"{fmt}:submission", f"/ontologies/{acr}/submissions/{sub}/download", {"download_format": fmt}))
                variants.append((f"{fmt}:latest", f"/ontologies/{acr}/download", {"download_format": fmt}))
            if ctx.cfg["download_fallback_original"] or not fmt:
                variants.append(("original:submission", f"/ontologies/{acr}/submissions/{sub}/download", {}))
            attempts_log = []
            result = None
            t0 = time.perf_counter()
            for variant, path, params in variants:
                try:
                    resp, info = api.request(path, params, stream=True,
                                             read_timeout=ctx.cfg["download_read_timeout_s"])
                except AuthError:
                    raise
                entry = {"variant": variant, "url": f"{api.base}{path}", "params": params,
                         "status": info.get("status"), "error": info.get("error")}
                if resp is None or resp.status_code != 200:
                    if resp is not None:
                        entry["body"] = resp.text[:500]
                        resp.close()
                    attempts_log.append(entry)
                    if resp is not None and resp.status_code in (401, 403):
                        result = {"status": "not_downloadable", "http_status": resp.status_code,
                                  "error": entry.get("body")}
                        break
                    continue
                ctype = resp.headers.get("Content-Type")
                clen = resp.headers.get("Content-Length")
                if max_bytes and clen and int(clen) > max_bytes:
                    resp.close()
                    entry["skipped"] = f"content-length {clen} over limit"
                    attempts_log.append(entry)
                    result = {"status": "skipped_too_large", "http_status": 200, "bytes": int(clen)}
                    break
                server_name = _server_filename(resp)
                ext = Path(server_name).suffix if server_name else ".download"
                raw_path = dest_dir / f"{safe_name(acr)}_{sub}_{variant.split(':')[0]}{ext or '.download'}"
                try:
                    sha, size = _stream_to_file(resp, raw_path, max_bytes)
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"stream failed: {type(exc).__name__}: {exc}"
                    attempts_log.append(entry)
                    continue
                finally:
                    resp.close()
                if sha is None:
                    entry["skipped"] = f"stream exceeded limit at {size} bytes"
                    attempts_log.append(entry)
                    result = {"status": "skipped_too_large", "http_status": 200, "bytes": size}
                    break
                prep = _prepare_eval_file(raw_path, row.get("bp_master_file_name"))
                entry.update(bytes=size, sniffed=prep["sniffed_format"])
                attempts_log.append(entry)
                if prep["sniffed_format"] == "html":
                    result = {"status": "html_response", "http_status": 200, "error": "server returned HTML"}
                    continue
                result = {"status": "ok", "variant": variant, "http_status": 200, "url": entry["url"],
                          "raw_path": str(raw_path), "bytes": size, "sha256": sha, "content_type": ctype,
                          "server_filename": server_name, **prep,
                          "eval_bytes": Path(prep["eval_path"]).stat().st_size}
                break
            if result is None:
                last = attempts_log[-1] if attempts_log else {}
                result = {"status": "not_found" if last.get("status") == 404 else "failed",
                          "http_status": last.get("status"), "error": last.get("body") or last.get("error")}
            ctx.state.upsert("downloads", {**base, **result, "finished_at": now(),
                                           "seconds": round(time.perf_counter() - t0, 3),
                                           "attempts_log": json.dumps(attempts_log)},
                             ("acronym", "submission_id"))
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] != "ok":
                ctx.event("download", acr, "WARNING", f"{result['status']}: {str(result.get('error'))[:300]}")
            if i % 25 == 0 or i == len(rows):
                print(f"  downloads {i}/{len(rows)}  {counts}  ({time.time() - t_start:.0f}s)")
    finally:
        _keep_awake(ctx, False)
    ctx.event("download", None, "INFO", f"download phase finished: {counts}")
    return counts


def add_local_file(ctx: Ctx, path: str, acronym: str, submission_id: int = 0, name: str | None = None) -> None:
    """Register a local ontology file so it goes through the same evaluation and export."""
    p = Path(path).resolve()
    prep = _prepare_eval_file(p, None)
    ctx.state.upsert("catalog", {"acronym": acronym, "name": name or acronym, "submission_id": submission_id,
                                 "status": "ok", "error": None, "fetched_at": now(), "api_seconds": 0,
                                 "ontology_json": json.dumps({"acronym": acronym, "name": name or acronym, "local_file": True}),
                                 "submission_json": None, "metrics_json": None}, ("acronym",))
    ctx.state.upsert("downloads", {"acronym": acronym, "submission_id": submission_id, "status": "ok",
                                   "variant": "local", "raw_path": str(p), "bytes": p.stat().st_size,
                                   "sha256": core.file_sha256(str(p), length=None), **prep,
                                   "eval_bytes": Path(prep["eval_path"]).stat().st_size,
                                   "started_at": now(), "finished_at": now(), "run_id": ctx.run_id,
                                   "attempts": 1}, ("acronym", "submission_id"))


# =============================================================================
# Phase C: evaluation with a restartable worker process
# =============================================================================

def _keep_awake(ctx: Ctx, on: bool) -> None:
    if not ctx.cfg.get("keep_awake") or os.name != "nt":
        return
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))
    except Exception:  # noqa: BLE001
        pass


def _read_stage(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


class EvalWorker:
    """One background process holding BERT. Killed and restarted when needed."""

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.proc = None
        self.tq = None
        self.rq = None
        self.info: dict | None = None
        self.restarts = 0
        self.stage_file = ctx.paths["logs"] / "current_stage.json"
        cfg = ctx.cfg
        self.wcfg = {k: cfg[k] for k in (
            "device", "bert_name", "bert_max_length", "batch_size_gpu", "batch_size_cpu",
            "mixed_precision", "cpu_threads", "define_vectors_on_gpu_max_gb", "define_cpu_fallback",
            "define_min_tokens", "depth_target", "branch_target", "suggestion_threshold",
            "save_entity_tables", "entity_rows_max")}
        self.wcfg.update(log_dir=str(ctx.paths["logs"]), stage_file=str(self.stage_file))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def start(self) -> dict:
        import multiprocessing as mp
        import owl4_worker
        code_dir = str(Path(core.__file__).resolve().parent)
        if code_dir not in sys.path:
            sys.path.insert(0, code_dir)
        mpc = mp.get_context("spawn")
        self.tq, self.rq = mpc.Queue(), mpc.Queue()
        self.proc = mpc.Process(target=owl4_worker.worker_main, args=(self.tq, self.rq, self.wcfg),
                                daemon=True, name="owl4-worker")
        self.proc.start()
        deadline = time.time() + self.ctx.cfg["worker_ready_timeout_s"]
        while True:
            try:
                msg = self.rq.get(timeout=2)
            except queue.Empty:
                if not self.proc.is_alive():
                    raise RuntimeError(f"worker exited while starting (exit code {self.proc.exitcode}); see logs/worker.log")
                if time.time() > deadline:
                    self.kill()
                    raise RuntimeError("worker did not become ready in time")
                continue
            if msg.get("type") == "ready":
                self.info = msg
                self.ctx.event("evaluate", None, "INFO", f"worker ready: {json.dumps(msg)}")
                return msg
            if msg.get("type") == "fatal":
                self.kill()
                raise RuntimeError(f"worker failed to start: {msg.get('error')}\n{msg.get('traceback')}")

    def kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.join(10)
            except Exception:  # noqa: BLE001
                pass
        for qobj in (self.tq, self.rq):
            try:
                qobj.cancel_join_thread()
                qobj.close()
            except Exception:  # noqa: BLE001
                pass
        self.proc = self.tq = self.rq = None

    def stop(self) -> None:
        if self.alive():
            try:
                self.tq.put(None)
                self.proc.join(30)
            except Exception:  # noqa: BLE001
                pass
        self.kill()

    def run(self, task: dict, timeout_s: float, ram_limit_bytes: int | None) -> dict:
        import psutil
        if not self.alive():
            if self.proc is not None:
                self.restarts += 1
            self.start()
        self.tq.put(task)
        t0 = time.time()
        peak = 0
        last_hb = t0
        pid = self.proc.pid
        while True:
            try:
                msg = self.rq.get(timeout=1.0)
                msg["parent_peak_rss_mb"] = round(peak / 2**20, 1)
                msg["wall_seconds"] = round(time.time() - t0, 3)
                return msg
            except queue.Empty:
                pass
            elapsed = time.time() - t0
            if not self.proc.is_alive():
                code = self.proc.exitcode
                stage = _read_stage(self.stage_file)
                self.kill()
                return {"status": "crashed", "error_type": "WorkerExit",
                        "error": f"worker process exited with code {code}", "stage": stage.get("stage"),
                        "parent_peak_rss_mb": round(peak / 2**20, 1), "wall_seconds": round(elapsed, 3)}
            try:
                rss = psutil.Process(pid).memory_info().rss
            except Exception:  # noqa: BLE001
                rss = 0
            peak = max(peak, rss)
            reason = None
            if ram_limit_bytes and rss > ram_limit_bytes:
                reason = ("memory_limit", f"worker RAM {rss / 2**30:.1f} GB over limit {ram_limit_bytes / 2**30:.1f} GB")
            elif elapsed > timeout_s:
                reason = ("timeout", f"no result after {elapsed / 60:.1f} min (limit {timeout_s / 60:.1f} min)")
            if reason:
                stage = _read_stage(self.stage_file)
                self.kill()
                return {"status": reason[0], "error_type": reason[0], "error": reason[1],
                        "stage": stage.get("stage"), "parent_peak_rss_mb": round(peak / 2**20, 1),
                        "wall_seconds": round(elapsed, 3)}
            if time.time() - last_hb >= self.ctx.cfg["heartbeat_s"]:
                stage = _read_stage(self.stage_file).get("stage")
                print(f"      ... {task['key']} running {elapsed / 60:.1f} min, stage={stage}, worker RAM {rss / 2**30:.1f} GB")
                last_hb = time.time()


def _mark_interrupted(ctx: Ctx) -> None:
    for r in ctx.state.q("SELECT acronym, submission_id, attempts FROM evaluations WHERE status='running'"):
        new = "gave_up" if r["attempts"] >= ctx.cfg["max_attempts"] else "interrupted"
        ctx.state.exec("UPDATE evaluations SET status=?, error=COALESCE(error,'') || ' [session ended while running]' "
                       "WHERE acronym=? AND submission_id=?", (new, r["acronym"], r["submission_id"]))
        ctx.event("evaluate", r["acronym"], "WARNING", f"found unfinished run from an earlier session -> {new}")


def evaluate_phase(ctx: Ctx, only: list | None = None, limit: int | None = None,
                   timeout_min: float | None = None) -> dict:
    """Evaluate every downloaded ontology that has no final result yet. Safe to re-run."""
    import psutil
    _mark_interrupted(ctx)
    rows = ctx.state.q(
        "SELECT d.acronym, d.submission_id, d.eval_path, d.eval_bytes, d.sniffed_format, "
        "e.status AS eval_status, e.attempts AS eval_attempts "
        "FROM downloads d LEFT JOIN evaluations e ON d.acronym=e.acronym AND d.submission_id=e.submission_id "
        "WHERE d.status='ok' ORDER BY d.eval_bytes ASC")
    if only:
        wanted = {a.upper() for a in only}
        rows = [r for r in rows if r["acronym"].upper() in wanted]
    todo = []
    for r in rows:
        st, att = r["eval_status"], r["eval_attempts"] or 0
        if st in TERMINAL_EVAL:
            continue
        if st in RETRYABLE_EVAL and att >= ctx.cfg["max_attempts"]:
            ctx.state.exec("UPDATE evaluations SET status='gave_up' WHERE acronym=? AND submission_id=?",
                           (r["acronym"], r["submission_id"]))
            continue
        todo.append(r)
    if limit:
        todo = todo[:limit]
    total_bytes = sum(r["eval_bytes"] or 0 for r in todo)
    print(f"{len(todo)} ontologies to evaluate ({total_bytes / 2**30:.2f} GB of files). "
          f"Already final: {len(rows) - len(todo)}.")
    if not todo:
        return {}
    ram_gb = ctx.cfg["worker_ram_limit_gb"] or round(psutil.virtual_memory().total * 0.85 / 2**30, 1)
    ram_limit = int(ram_gb * 2**30)
    tmin = timeout_min or ctx.cfg["eval_timeout_min"]
    print(f"Limits per ontology: {tmin:.0f} min, {ram_gb} GB worker RAM. Interrupt the kernel to pause; re-run to resume.")
    worker = EvalWorker(ctx)
    counts: dict = {}
    t_start = time.time()
    done_bytes = 0
    _keep_awake(ctx, True)
    current = None
    try:
        info = worker.start()
        print(f"Worker ready on {info['device']} ({info.get('gpu_name')}), model load {info['model_load_seconds']} s")
        for i, r in enumerate(todo, 1):
            acr, sub = r["acronym"], r["submission_id"]
            key = f"{acr}@{sub}"
            current = r
            attempts = (r["eval_attempts"] or 0) + 1
            ctx.state.upsert("evaluations", {"acronym": acr, "submission_id": sub, "status": "running",
                                             "attempts": attempts, "started_at": now(), "run_id": ctx.run_id,
                                             "timeout_min": tmin, "error": None, "error_type": None,
                                             "traceback": None, "stage_at_failure": None},
                             ("acronym", "submission_id"))
            out_dir = ctx.paths["results"] / safe_name(acr) / str(sub)
            task = {"key": key, "acronym": acr, "submission_id": sub, "path": r["eval_path"],
                    "out_dir": str(out_dir)}
            msg = worker.run(task, tmin * 60, ram_limit)
            status = msg.get("status", "error")
            upd = {"acronym": acr, "submission_id": sub, "status": status, "finished_at": now(),
                   "seconds": msg.get("wall_seconds"), "parent_peak_rss_mb": msg.get("parent_peak_rss_mb"),
                   "error_type": msg.get("error_type"), "error": msg.get("error"),
                   "traceback": msg.get("traceback"), "stage_at_failure": msg.get("stage")}
            if status == "done":
                s = msg["summary"]
                s["wall_seconds"] = msg.get("wall_seconds")
                s["parent_peak_rss_mb"] = msg.get("parent_peak_rss_mb")
                upd.update(describe_score=s["describe_score"], define_score=s["define_score"],
                           connection_score=s["connection_score"], flat_score=s["flat_score"],
                           core_average=s["core_average"], summary_json=json.dumps(core.to_jsonable(s)),
                           result_path=msg.get("result_path"), entities_path=msg.get("entities_path"))
            elif status in RETRYABLE_EVAL and attempts >= ctx.cfg["max_attempts"]:
                upd["status"] = "gave_up"
            ctx.state.upsert("evaluations", upd, ("acronym", "submission_id"))
            if status == "done" and not ctx.cfg.get("keep_downloads", True):
                shutil.rmtree(ctx.paths["downloads"] / safe_name(acr) / str(sub), ignore_errors=True)
            counts[upd["status"]] = counts.get(upd["status"], 0) + 1
            done_bytes += r["eval_bytes"] or 0
            elapsed = time.time() - t_start
            if status == "done":
                line = (f"avg {s['core_average']:.2f} (Describe {s['describe_score']}, Define {s['define_score']}, "
                        f"Connection {s['connection_score']}, Flat {s['flat_score']})")
            else:
                line = f"{upd['status'].upper()}: {str(upd['error'])[:160]}"
                ctx.event("evaluate", acr, "WARNING", f"{upd['status']} at stage {upd['stage_at_failure']}: {upd['error']}")
            print(f"[{i}/{len(todo)}] {acr:<18} {msg.get('wall_seconds', 0):>8.1f}s  {line}")
            if i % 25 == 0:
                rate = done_bytes / max(elapsed, 1)
                left = (total_bytes - done_bytes) / rate if rate > 0 else float("nan")
                print(f"    progress: {counts}; elapsed {elapsed / 3600:.2f} h; rough time left by bytes "
                      f"{left / 3600:.1f} h (large files are slower per byte, so treat as a lower bound)")
        current = None
    except KeyboardInterrupt:
        print("\nPaused. The ontology that was running goes back to the queue. Re-run this cell to continue.")
        if current is not None:
            ctx.state.exec("UPDATE evaluations SET status='interrupted', attempts=MAX(attempts-1,0), "
                           "error='paused by user' WHERE acronym=? AND submission_id=?",
                           (current["acronym"], current["submission_id"]))
    finally:
        worker.stop()
        _keep_awake(ctx, False)
    ctx.event("evaluate", None, "INFO", f"evaluate phase finished: {counts}; worker restarts {worker.restarts}")
    ctx.state.exec("UPDATE runs SET finished_at=?, phase=? WHERE run_id=?", (now(), "evaluate", ctx.run_id))
    return counts


def requeue(ctx: Ctx, statuses: tuple = ("timeout", "memory_limit", "gave_up", "error", "crashed"),
            acronyms: list | None = None) -> int:
    """Put finished-but-failed evaluations back in the queue (attempts reset)."""
    sql = f"SELECT acronym, submission_id FROM evaluations WHERE status IN ({','.join('?' * len(statuses))})"
    rows = ctx.state.q(sql, tuple(statuses))
    if acronyms:
        wanted = {a.upper() for a in acronyms}
        rows = [r for r in rows if r["acronym"].upper() in wanted]
    for r in rows:
        ctx.state.exec("UPDATE evaluations SET status='pending', attempts=0 WHERE acronym=? AND submission_id=?",
                       (r["acronym"], r["submission_id"]))
    ctx.event("evaluate", None, "INFO", f"requeued {len(rows)} evaluations with status in {statuses}")
    return len(rows)


def status(ctx: Ctx) -> dict:
    out = {}
    for table, col in (("catalog", "status"), ("downloads", "status"), ("evaluations", "status")):
        out[table] = {r[col]: r["n"] for r in ctx.state.q(f"SELECT {col}, COUNT(*) AS n FROM {table} GROUP BY {col}")}
    t = ctx.state.one("SELECT SUM(seconds) AS s, COUNT(*) AS n FROM evaluations WHERE status='done'")
    out["evaluation_hours_done"] = round((t["s"] or 0) / 3600, 2)
    return out


# =============================================================================
# Parity check against WiseOwl's own test files
# =============================================================================

WISEOWL_RAW = "https://raw.githubusercontent.com/aryand1/WiseOwl/main/tests/data"


def parity_check(ctx: Ctx) -> dict:
    """Run tiny.owl and GoodRelations.owl through the worker and compare with WiseOwl's golden scores."""
    pdir = ctx.work / "parity"
    pdir.mkdir(exist_ok=True)
    for fname in ("tiny.owl", "GoodRelations.owl", "golden_tiny.json", "golden_goodrelations.json"):
        dest = pdir / fname
        if not dest.exists():
            r = requests.get(f"{WISEOWL_RAW}/{fname}", timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
    worker = EvalWorker(ctx)
    report: dict = {"time": now(), "cases": []}
    try:
        info = worker.start()
        report["worker"] = info
        for name, fname, golden in (("tiny", "tiny.owl", "golden_tiny.json"),
                                    ("goodrelations", "GoodRelations.owl", "golden_goodrelations.json")):
            expected = json.loads((pdir / golden).read_text())["scores"]
            msg = worker.run({"key": f"parity:{name}", "path": str(pdir / fname),
                              "out_dir": str(pdir / f"out_{name}")}, 600, None)
            if msg.get("status") != "done":
                report["cases"].append({"name": name, "ok": False, "error": msg.get("error"),
                                        "traceback": msg.get("traceback")})
                continue
            got = {k: msg["summary"][k] for k in core.CORE_LABELS}
            exp = {k: expected[k] for k in core.CORE_LABELS}
            report["cases"].append({"name": name, "ok": got == exp, "got": got, "expected": exp,
                                    "seconds": msg.get("wall_seconds")})
    finally:
        worker.stop()
    report["all_ok"] = all(c["ok"] for c in report["cases"])
    core.dump_json(report, str(ctx.paths["exports"] / "parity_check.json"))
    ctx.event("parity", None, "INFO" if report["all_ok"] else "ERROR", json.dumps(report["cases"]))
    return report


# =============================================================================
# Search latency benchmark (for the 2 second web target)
# =============================================================================

DEFAULT_KEYWORDS = [
    "diabetes", "cancer", "melanoma", "heart", "kidney", "liver", "asthma", "influenza",
    "gene", "protein", "cell", "neuron", "brain", "bone", "blood pressure", "vaccine",
    "antibiotic", "drug", "pregnancy", "obesity", "sleep", "anxiety", "depression",
    "mouse", "zebrafish", "plant", "soil", "climate", "food", "nutrition",
    "clinical trial", "imaging", "surgery", "pathology", "genome", "rna", "enzyme",
    "infection", "covid-19", "rare disease",
]


def search_benchmark(ctx: Ctx, keywords: list | None = None, repeats: int = 1) -> Any:
    """Time BioPortal /search and /recommender for each keyword."""
    import pandas as pd
    api = ctx.api
    rows = []
    for rep in range(repeats):
        for kw in keywords or DEFAULT_KEYWORDS:
            for endpoint, path, params in (
                    ("search", "/search", {"q": kw, "pagesize": 100, "display_context": "false",
                                           "display_links": "false", "include": "prefLabel"}),
                    ("recommender", "/recommender", {"input": kw, "input_type": 2, "output_type": 1,
                                                     "display_context": "false", "display_links": "false"})):
                data, info = api.get_json(path, params)
                count, onts, top = None, None, None
                try:
                    if endpoint == "search" and isinstance(data, dict):
                        coll = data.get("collection") or []
                        count = data.get("totalCount", len(coll))
                        acrs = [_tail((c.get("links") or {}).get("ontology") or "") for c in coll]
                        acrs = [a for a in acrs if a]
                        onts = len(set(acrs))
                        top = ";".join(pd.Series(acrs).value_counts().head(10).index) if acrs else ""
                    elif endpoint == "recommender" and isinstance(data, list):
                        count = len(data)
                        names = []
                        for rec in data:
                            for o in rec.get("ontologies") or []:
                                names.append(o.get("acronym") or _tail(o.get("@id", "")))
                        onts = len(set(names))
                        top = ";".join(names[:10])
                except Exception as exc:  # noqa: BLE001
                    info["error"] = f"parse: {exc}"
                row = {"ts": now(), "run_id": ctx.run_id, "endpoint": endpoint, "keyword": kw,
                       "http_status": info.get("status"), "seconds": info.get("seconds"),
                       "result_count": count, "distinct_ontologies": onts, "top_ontologies": top,
                       "error": info.get("error") or (info.get("body") if data is None else None)}
                ctx.state.conn.execute(
                    "INSERT INTO search_benchmark(ts, run_id, endpoint, keyword, http_status, seconds, "
                    "result_count, distinct_ontologies, top_ontologies, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    tuple(row.values()))
                ctx.state.conn.commit()
                rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(ctx.paths["exports"] / f"search_benchmark_{ctx.run_id}.csv", index=False)
    return df


# =============================================================================
# Phase D: exports
# =============================================================================

COLUMN_DOCS = {
    "acronym": "BioPortal ontology acronym (primary key per provider).",
    "name": "Ontology name from BioPortal.",
    "submission_id": "BioPortal submission id that was downloaded and scored (-1 if none).",
    "catalog_status": "ok, no_submission, or error when fetching metadata.",
    "download_status": "ok, not_downloadable (license 401/403), private, summary_only, skipped_too_large, not_found, html_response, failed.",
    "download_variant": "Which download worked: rdf:submission, rdf:latest, original:submission, or local.",
    "download_bytes": "Size of the downloaded file in bytes.",
    "eval_bytes": "Size of the file that was parsed (after unzip).",
    "download_sha256": "SHA-256 of the downloaded file.",
    "eval_status": "done, parse_failed, timeout, memory_limit, memory_error, error, crashed, interrupted, gave_up, pending.",
    "eval_attempts": "How many times evaluation was started for this ontology.",
    "eval_seconds": "Wall time of the evaluation seen by the notebook, in seconds.",
    "web_status": "Status for the web page: scored, not_downloadable, parse_failed, timed_out, too_large, failed, pending, no_submission.",
    "describe_score": "WiseOwl Describe (0-10): 10 x share of entities with a descriptive annotation.",
    "define_score": "WiseOwl Define (0-10): 10 x mean of 0.4 x match + 0.6 x adequacy; undefined entities count as 0.",
    "connection_score": "WiseOwl Connection (0-10): 10 x (0.7 coverage + 0.2 diversity + 0.1 richness).",
    "flat_score": "WiseOwl Flat (integer 0-10): round((depth score + breadth score) / 2).",
    "core_average": "Mean of the four core scores, full precision (the WiseOwl average).",
    "core_average_2dp": "core_average rounded to 2 decimals, as the WiseOwl dashboard shows it.",
    "weakest_metric": "Core metric with the lowest score.",
    "suggestions": "WiseOwl suggestion sentences for metrics below the threshold.",
    "summary_banner": "WiseOwl summary sentence (core part).",
    "ontology_iri": "owl:Ontology IRI in the file, or a file hash if none is declared.",
    "version_iri": "owl:versionIRI, or a file hash if none is declared.",
    "sniffed_format": "Format detected from file content before parsing.",
    "parse_format": "rdflib format that parsed the file.",
    "triple_count": "Number of RDF triples in the parsed file (imports are not followed).",
    "entities": "Classes plus individuals: the population the metrics score.",
    "classes": "WiseOwl class set (typed classes, subClassOf participants, SKOS concepts).",
    "named_classes": "classes minus owl:Thing and owl:Nothing.",
    "individuals": "Subjects typed with a class that are not classes.",
    "deprecated_entities": "Entities marked owl:deprecated true (still counted by WiseOwl).",
    "owl_imports_count": "Number of owl:imports; imported ontologies are not loaded or scored.",
    "describe_described": "Entities counted as described.",
    "define_defined": "Entities with a non-empty definition.",
    "define_cosine_mean": "Mean BERT label/definition cosine (used for the z-score).",
    "define_cosine_std": "Std of the cosines (used for the z-score).",
    "define_definitions_truncated": "Definitions cut at the BERT max length (128 tokens).",
    "define_device": "Device used for Define (cuda or cpu; cpu after a GPU memory fallback).",
    "connection_coverage": "Share of entities with at least one object-property link.",
    "connection_diversity": "Mean of min(distinct properties / 5, 1).",
    "connection_richness": "Mean of min(log11(links + 1), 1).",
    "flat_max_depth": "Longest root-to-leaf path in the class taxonomy (in nodes).",
    "flat_avg_branching": "Mean children per parent in the class taxonomy.",
    "gpu_peak_allocated_mb": "Peak GPU memory allocated by PyTorch during this ontology.",
    "parent_peak_rss_mb": "Peak worker RAM seen by the notebook while this ontology ran.",
    "wiseowl_source_version": "WiseOwl version the metric code was copied from.",
    "core4_version": "Version of this four-metric copy.",
    "provider": "Ontology source (bioportal for now).",
    "catalog_error": "Error text when BioPortal metadata could not be fetched.",
    "catalog_fetched_at": "When the BioPortal metadata was fetched (UTC).",
    "parse_attempts": "How many rdflib formats were tried before one worked.",
    "subjects": "Distinct RDF subjects in the file.",
    "distinct_predicates": "Distinct RDF predicates in the file.",
    "annotation_properties": "Subjects typed owl:AnnotationProperty (they count as Describe signals).",
    "object_properties_declared": "Subjects typed owl:ObjectProperty.",
    "datatype_properties_declared": "Subjects typed owl:DatatypeProperty.",
    "owl_classes_typed": "Subjects typed owl:Class.",
    "rdfs_classes_typed": "Subjects typed rdfs:Class.",
    "skos_concepts": "Subjects typed skos:Concept (WiseOwl counts them as classes).",
    "owl_restrictions": "Subjects typed owl:Restriction.",
    "subclassof_triples": "rdfs:subClassOf triples.",
    "equivalentclass_triples": "owl:equivalentClass triples.",
    "entity_rows_written": "Rows written to entities.csv.gz for this ontology.",
    "rss_mb_now": "Worker RAM right after this ontology finished.",
    "process_lifetime_peak_mb": "Windows only: peak RAM of the worker process since it started.",
    "bert_name": "Hugging Face model used by Define.",
    "mixed_precision": "Whether fp16/bf16 autocast was on (off keeps WiseOwl parity).",
    "encoder_device": "Device the BERT encoder was loaded on.",
    "encoder_batch_size": "BERT batch size.",
    "torch_version": "PyTorch version in the worker.",
    "transformers_version": "transformers version in the worker.",
    "rdflib_version": "rdflib version in the worker.",
    "wall_seconds": "Evaluation wall time seen by the notebook.",
    "gpu_peak_reserved_mb": "Peak GPU memory reserved by PyTorch during this ontology.",
}
PREFIX_DOCS = {
    "bp_": "BioPortal metadata (ontology record, latest submission, or BioPortal metrics endpoint).",
    "time_": "Seconds spent in this stage of the evaluation.",
    "define_": "Define metric detail.",
    "connection_": "Connection metric detail.",
    "flat_": "Flat metric detail.",
    "describe_": "Describe metric detail.",
    "download_": "Download detail.",
    "eval_": "Evaluation bookkeeping (status, times, errors, file paths).",
}
WEB_STATUS = {
    "done": "scored", "parse_failed": "parse_failed", "timeout": "timed_out", "memory_limit": "too_large",
    "memory_error": "too_large", "gave_up": "failed", "error": "failed", "crashed": "failed",
}


def results_frame(ctx: Ctx):
    import pandas as pd
    cat = catalog_frame(ctx)
    dl = pd.DataFrame(ctx.state.q("SELECT * FROM downloads"))
    ev = pd.DataFrame(ctx.state.q("SELECT * FROM evaluations"))
    if not dl.empty:
        dl = dl.rename(columns={c: f"download_{c}" for c in dl.columns if c not in ("acronym", "submission_id")})
        dl = dl.rename(columns={"download_eval_bytes": "eval_bytes", "download_sha256": "download_sha256"})
        cat = cat.merge(dl, on=["acronym", "submission_id"], how="left")
    if not ev.empty:
        summaries = []
        for sj in ev["summary_json"].fillna("{}"):
            summaries.append(json.loads(sj))
        sdf = pd.DataFrame(summaries)
        drop = [c for c in ("describe_score", "define_score", "connection_score", "flat_score", "core_average") if c in sdf.columns]
        sdf = sdf.drop(columns=drop)
        ev = ev.drop(columns=["summary_json"])
        ev = ev.rename(columns={c: f"eval_{c}" for c in ev.columns
                                if c not in ("acronym", "submission_id", "describe_score", "define_score",
                                             "connection_score", "flat_score", "core_average")})
        ev = pd.concat([ev.reset_index(drop=True), sdf.reset_index(drop=True)], axis=1)
        cat = cat.merge(ev, on=["acronym", "submission_id"], how="left")
    if "core_average" in cat.columns:
        cat["core_average_2dp"] = cat["core_average"].round(2)

    def web_status(row):
        if row.get("catalog_status") == "no_submission":
            return "no_submission"
        es = row.get("eval_status")
        if isinstance(es, str) and es in WEB_STATUS:
            return WEB_STATUS[es]
        ds = row.get("download_status")
        if ds in ("not_downloadable", "private", "summary_only"):
            return "not_downloadable"
        if ds == "skipped_too_large":
            return "too_large"
        if isinstance(ds, str) and ds not in ("ok",):
            return "failed"
        return "pending"
    cat["web_status"] = cat.apply(web_status, axis=1)
    cat.insert(0, "provider", ctx.cfg["provider"])
    return cat


def _sql(v: Any) -> str:
    import math
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return "NULL" if math.isnan(v) else repr(v)
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, bool):
        return "1" if v else "0"
    return "'" + str(v).replace("'", "''") + "'"


D1_SCHEMA = """-- Cloudflare D1 / SQLite schema for the web search endpoint.
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
  connection_score REAL,
  flat_score REAL,
  core_average REAL,
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
CREATE INDEX IF NOT EXISTS idx_scores_status ON ontology_scores(status);
CREATE TABLE IF NOT EXISTS keyword_cache (
  keyword TEXT NOT NULL,
  provider TEXT NOT NULL,
  results_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (keyword, provider)
);
"""

D1_FTS = """-- Optional full-text index for the local fallback search.
CREATE VIRTUAL TABLE IF NOT EXISTS ontology_search USING fts5(provider, acronym, name, description, categories);
"""


def export_phase(ctx: Ctx) -> dict:
    """Write every table and summary file to exports/. Safe to run any time."""
    import pandas as pd
    exp = ctx.paths["exports"]
    df = results_frame(ctx)
    files: dict = {}

    full_csv = exp / "ontology_results_full.csv"
    df.to_csv(full_csv, index=False)
    files["ontology_results_full.csv"] = "Every ontology: BioPortal metadata, download, evaluation, all summary fields."
    try:
        df.to_parquet(exp / "ontology_results_full.parquet", index=False)
        files["ontology_results_full.parquet"] = "Same as the CSV, typed columns (needs pyarrow)."
    except Exception as exc:  # noqa: BLE001
        ctx.event("export", None, "WARNING", f"parquet skipped: {exc}")

    score_cols = ["provider", "acronym", "name", "web_status", "describe_score", "define_score",
                  "connection_score", "flat_score", "core_average", "core_average_2dp", "weakest_metric",
                  "bp_categories", "bp_language", "submission_id", "bp_version", "bp_released", "bp_url"]
    df[[c for c in score_cols if c in df.columns]].to_csv(exp / "scores_compact.csv", index=False)
    files["scores_compact.csv"] = "One row per ontology with the four scores and web status."

    bench_cols = ["acronym", "eval_status", "eval_bytes", "triple_count", "entities", "classes",
                  "individuals", "bp_classes", "sniffed_format", "parse_format", "eval_seconds",
                  "wall_seconds", "parent_peak_rss_mb", "gpu_peak_allocated_mb", "define_device",
                  "define_defined"] + [c for c in df.columns if c.startswith("time_")] + [
                  "define_seconds_encode_labels", "define_seconds_encode_definitions", "download_seconds"]
    bench = df[[c for c in bench_cols if c in df.columns]]
    if "eval_status" in bench.columns:
        bench = bench[bench.eval_status.notna()]
    bench.to_csv(exp / "timings_and_memory.csv", index=False)
    files["timings_and_memory.csv"] = "Per-ontology size, stage timings, RAM and GPU use (the benchmark table)."

    fail_cols = ["acronym", "name", "web_status", "catalog_status", "catalog_error", "download_status",
                 "download_http_status", "download_error", "eval_status", "eval_error_type", "eval_error",
                 "eval_stage_at_failure", "eval_attempts", "eval_bytes", "bp_classes", "bp_language"]
    fails = df[df.web_status != "scored"]
    fails[[c for c in fail_cols if c in fails.columns]].to_csv(exp / "not_scored.csv", index=False)
    files["not_scored.csv"] = "Every ontology without scores and the reason."

    with open(exp / "results_detail.jsonl", "w", encoding="utf-8") as fh:
        for r in ctx.state.q("SELECT result_path FROM evaluations WHERE status='done' AND result_path IS NOT NULL"):
            p = Path(r["result_path"])
            if p.exists():
                fh.write(json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False) + "\n")
    files["results_detail.jsonl"] = "Full nested result.json of each scored ontology, one per line."

    pd.DataFrame(ctx.state.q("SELECT * FROM events ORDER BY id")).to_csv(exp / "events.csv", index=False)
    files["events.csv"] = "Log of warnings, errors and phase summaries across all sessions."
    pd.DataFrame(ctx.state.q("SELECT * FROM runs ORDER BY started_at")).to_csv(exp / "sessions.csv", index=False)
    files["sessions.csv"] = "Every notebook session: config and environment."
    sb = ctx.state.q("SELECT * FROM search_benchmark ORDER BY id")
    if sb:
        pd.DataFrame(sb).to_csv(exp / "search_benchmark_all.csv", index=False)
        files["search_benchmark_all.csv"] = "BioPortal /search and /recommender latency per keyword."

    # data dictionary
    dd = []
    for col in df.columns:
        desc = COLUMN_DOCS.get(col)
        if desc is None:
            for pref, pdesc in PREFIX_DOCS.items():
                if col.startswith(pref):
                    desc = pdesc
                    break
        dd.append({"column": col, "dtype": str(df[col].dtype), "non_null": int(df[col].notna().sum()),
                   "description": desc or ""})
    pd.DataFrame(dd).to_csv(exp / "data_dictionary.csv", index=False)
    files["data_dictionary.csv"] = "Every column in ontology_results_full.csv with type, fill count, meaning."

    # D1 SQL
    (exp / "d1_schema.sql").write_text(D1_SCHEMA, encoding="utf-8")
    (exp / "d1_fts.sql").write_text(D1_FTS, encoding="utf-8")
    lines = []
    fts_lines = []
    for r in df.to_dict("records"):
        def g(k):
            v = r.get(k)
            try:
                import math
                if isinstance(v, float) and math.isnan(v):
                    return None
            except Exception:  # noqa: BLE001
                pass
            return v
        detail = g("eval_error") or g("download_error") or g("catalog_error")
        row = {
            "provider": g("provider"), "acronym": g("acronym"), "name": g("name"),
            "description": (g("bp_description") or "")[:2000] or None, "categories": g("bp_categories"),
            "ontology_language": g("bp_language"),
            "submission_id": int(g("submission_id")) if g("submission_id") is not None else None,
            "version": g("bp_version"), "released": g("bp_released"), "bioportal_url": g("bp_url"),
            "file_sha256": g("download_sha256"), "status": g("web_status"),
            "status_detail": str(detail)[:300] if detail else None,
            "describe_score": g("describe_score"), "define_score": g("define_score"),
            "connection_score": g("connection_score"),
            "flat_score": float(g("flat_score")) if g("flat_score") is not None else None,
            "core_average": round(g("core_average"), 4) if g("core_average") is not None else None,
            "logical_consistency_score": None, "structural_dist_score": None, "semantic_dist_score": None,
            "triple_count": int(g("triple_count")) if g("triple_count") is not None else None,
            "entity_count": int(g("entities")) if g("entities") is not None else None,
            "class_count": int(g("classes")) if g("classes") is not None else None,
            "wiseowl_version": g("wiseowl_source_version"), "core4_version": g("core4_version"),
            "device": g("define_device"), "evaluated_at": g("eval_finished_at"), "updated_at": now(),
        }
        lines.append(f"INSERT OR REPLACE INTO ontology_scores ({', '.join(row)}) VALUES "
                     f"({', '.join(_sql(v) for v in row.values())});")
        fts_lines.append("INSERT INTO ontology_search (provider, acronym, name, description, categories) VALUES "
                         f"({_sql(row['provider'])}, {_sql(row['acronym'])}, {_sql(row['name'])}, "
                         f"{_sql(row['description'])}, {_sql(row['categories'])});")
    (exp / "d1_data.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (exp / "d1_fts_data.sql").write_text("DELETE FROM ontology_search;\n" + "\n".join(fts_lines) + "\n", encoding="utf-8")
    files["d1_schema.sql"] = "CREATE TABLE statements for Cloudflare D1 (includes empty columns for the 3 later metrics)."
    files["d1_data.sql"] = "INSERT OR REPLACE rows for ontology_scores."
    files["d1_fts.sql"] = "Optional full-text search table (fallback search)."
    files["d1_fts_data.sql"] = "Rows for the full-text search table."

    # run summary
    scored = df[df.web_status == "scored"]
    summary = {
        "time": now(), "run_id": ctx.run_id, "notebook_version": NOTEBOOK_VERSION,
        "work_dir": str(ctx.work), "ontologies_in_catalog": int(len(df)),
        "web_status_counts": df.web_status.value_counts().to_dict(),
        "status": status(ctx),
        "score_stats": {c: core._stats(scored[c].dropna().to_numpy()) for c in
                        ("describe_score", "define_score", "connection_score", "flat_score", "core_average")
                        if c in scored.columns},
        "config": ctx.cfg,
    }
    if "eval_seconds" in df.columns:
        summary["evaluation_seconds_total"] = float(df.eval_seconds.fillna(0).sum())
    core.dump_json(summary, str(exp / "run_summary.json"))
    files["run_summary.json"] = "Counts, score distributions, total time, config."

    readme = ["# Output files", "", f"Generated {now()} by {NOTEBOOK_VERSION}.", "",
              "## exports/", ""] + [f"- `{k}`: {v}" for k, v in files.items()] + [
        "", "## Other folders", "",
        "- `state.sqlite`: the resume database (catalog, downloads, evaluations, events, runs, search_benchmark).",
        "- `raw/<ACRONYM>/`: raw BioPortal JSON (ontology record, latest submission, metrics).",
        "- `raw/ontologies_list.json`: the full BioPortal listing at catalog time.",
        "- `downloads/<ACRONYM>/<submission>/`: downloaded files (and `extracted/` for zip archives).",
        "- `results/<ACRONYM>/<submission>/result.json`: full nested result for one ontology.",
        "- `results/<ACRONYM>/<submission>/entities.csv.gz`: one row per entity with every per-entity value.",
        "- `logs/pipeline.log`, `logs/worker.log`: text logs.",
        "- `environment/environment_<run>.json`: hardware and library versions per session.",
        "- `parity/`: WiseOwl test files and outputs used for the parity check.",
    ]
    (exp / "README_outputs.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    ctx.event("export", None, "INFO", f"exported {len(files)} files to {exp}")
    return {"export_dir": str(exp), "files": files, "web_status_counts": summary["web_status_counts"]}
