"""owl4_worker: background process that evaluates ontologies one at a time.

The notebook (parent) starts this process, sends one task at a time, and
watches its memory and run time. If an ontology hangs, uses too much memory,
or crashes Python, the parent kills this process and starts a new one; the
notebook itself keeps running.

Must live in a .py file: Windows starts child processes with "spawn", which
imports the target function by module name.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import traceback


def _setup_logging(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("owl4")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(os.path.join(log_dir, "worker.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [pid %(process)d] %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(fh)
    return logger


def _write_stage(path: str, payload: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _peak_rss_mb() -> dict:
    try:
        import psutil
        mi = psutil.Process().memory_info()
        out = {"rss_mb_now": round(mi.rss / 2**20, 1)}
        peak = getattr(mi, "peak_wset", None)  # Windows only: lifetime peak
        if peak:
            out["process_lifetime_peak_mb"] = round(peak / 2**20, 1)
        return out
    except Exception:  # noqa: BLE001
        return {}


def worker_main(task_q, result_q, cfg: dict) -> None:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    log = _setup_logging(cfg["log_dir"])
    logging.getLogger("rdflib").setLevel(logging.ERROR)
    try:
        import torch
        import owl4_core as core

        t0 = time.perf_counter()
        device_spec = cfg.get("device", "auto")
        if device_spec == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            device = device_spec
            if device.startswith("cuda") and not torch.cuda.is_available():
                log.warning("CUDA requested but not available; using CPU")
                device = "cpu"
        if cfg.get("cpu_threads"):
            torch.set_num_threads(int(cfg["cpu_threads"]))
        batch = cfg["batch_size_gpu"] if device.startswith("cuda") else cfg["batch_size_cpu"]
        encoder = core.load_encoder(
            cfg["bert_name"], device, max_length=cfg["bert_max_length"], batch_size=batch,
            mixed_precision=cfg["mixed_precision"],
            vectors_on_device_max_gb=cfg["define_vectors_on_gpu_max_gb"])
        load_s = round(time.perf_counter() - t0, 2)
        info = {
            "type": "ready", "pid": os.getpid(), "device": device, "batch_size": batch,
            "model_load_seconds": load_s, "torch": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "python": sys.version.split()[0], "platform": platform.platform(),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        }
        log.info("worker ready: %s", info)
        result_q.put(info)
    except Exception as exc:  # noqa: BLE001
        result_q.put({"type": "fatal", "error": f"{type(exc).__name__}: {exc}",
                      "traceback": traceback.format_exc()})
        return

    cpu_encoder_holder: dict = {}

    def cpu_fallback():
        if "enc" not in cpu_encoder_holder:
            cpu_encoder_holder["enc"] = core.load_encoder(
                cfg["bert_name"], "cpu", max_length=cfg["bert_max_length"],
                batch_size=cfg["batch_size_cpu"], mixed_precision=False,
                vectors_on_device_max_gb=1e9)
        return cpu_encoder_holder["enc"]

    while True:
        task = task_q.get()
        if task is None:
            log.info("worker stopping")
            break
        result_q.put(run_task(task, encoder, cpu_fallback if cfg.get("define_cpu_fallback") else None,
                              cfg, log, core, torch))


def run_task(task: dict, encoder, fallback, cfg: dict, log, core, torch) -> dict:
    key = task["key"]
    out_dir = task["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    stage_file = cfg["stage_file"]
    started = time.time()

    def on_stage(name: str) -> None:
        _write_stage(stage_file, {"key": key, "stage": name, "since": time.time(), "started": started})
        log.info("%s: stage %s", key, name)

    on_stage("start")
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    base = {"type": "result", "key": key, "pid": os.getpid()}
    try:
        res = core.evaluate_file(
            task["path"], encoder,
            min_tokens=cfg["define_min_tokens"], depth_target=cfg["depth_target"],
            branch_target=cfg["branch_target"], suggestion_threshold=cfg["suggestion_threshold"],
            want_entities=cfg["save_entity_tables"], entity_rows_max=cfg["entity_rows_max"],
            fallback_encoder=fallback, on_stage=on_stage,
            extra_definition_props=task.get("extra_definition_props"),
            use_owlready2=cfg.get("use_owlready2", True))
    except core.OntologyParseError as exc:
        attempts = getattr(exc, "attempts", [])
        try:
            core.dump_json({"task": task, "error": str(exc), "attempts": attempts},
                           os.path.join(out_dir, "parse_failure.json"))
        except Exception:  # noqa: BLE001
            pass
        return {**base, "status": "parse_failed", "error_type": "OntologyParseError",
                "error": str(exc)[:2000], "traceback": json.dumps(attempts)[:4000],
                "stage": "parse", **_peak_rss_mb()}
    except MemoryError as exc:
        return {**base, "status": "memory_error", "error_type": "MemoryError",
                "error": str(exc)[:2000], "traceback": traceback.format_exc()[-4000:], **_peak_rss_mb()}
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "error_type": type(exc).__name__,
                "error": str(exc)[:2000], "traceback": traceback.format_exc()[-4000:], **_peak_rss_mb()}

    on_stage("write_outputs")
    t_w = time.perf_counter()
    summary = res["summary"]
    details = res["details"]
    if torch.cuda.is_available():
        summary["gpu_peak_allocated_mb"] = round(torch.cuda.max_memory_allocated() / 2**20, 1)
        summary["gpu_peak_reserved_mb"] = round(torch.cuda.max_memory_reserved() / 2**20, 1)
    summary.update(_peak_rss_mb())
    summary["bert_name"] = cfg["bert_name"]
    summary["mixed_precision"] = cfg["mixed_precision"]
    summary["encoder_device"] = str(encoder.device)
    summary["encoder_batch_size"] = encoder.batch_size
    summary["torch_version"] = torch.__version__
    summary["python_hash_seed"] = os.environ.get("PYTHONHASHSEED")
    try:
        import transformers
        summary["transformers_version"] = transformers.__version__
    except Exception:  # noqa: BLE001
        pass
    import rdflib
    summary["rdflib_version"] = rdflib.__version__

    result_path = os.path.join(out_dir, "result.json")
    core.dump_json({"task": {k: v for k, v in task.items() if k != "catalog"},
                    "summary": summary, "details": details}, result_path)
    entities_path = None
    if res["entities"] is not None:
        entities_path = os.path.join(out_dir, "entities.csv.gz")
        tmp = entities_path + ".tmp"
        res["entities"].to_csv(tmp, index=False, compression="gzip")
        os.replace(tmp, entities_path)
    summary["time_write_outputs"] = round(time.perf_counter() - t_w, 3)
    del res
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    on_stage("done")
    return {**base, "status": "done", "summary": summary, "result_path": result_path,
            "entities_path": entities_path}
