"""Replace the notebook's %%writefile cells with the checked-in modules.

The batch notebook writes importable modules for Windows multiprocessing. Run
this utility after editing a module so the notebook cannot overwrite the edit
with an older embedded copy.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOTEBOOK = HERE / "WiseOwl_4metric_BioPortal_batch_v1_1.ipynb"
MODULES = ("owl4_core.py", "owl4_worker.py", "owl4_pipeline.py")


def main() -> int:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    waiting = set(MODULES)
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        first = source.splitlines()[0] if source else ""
        for name in tuple(waiting):
            if first.strip() == f"%%writefile {name}":
                module = (HERE / name).read_text(encoding="utf-8")
                cell["source"] = [f"%%writefile {name}\n", *module.splitlines(keepends=True)]
                waiting.remove(name)
    if waiting:
        raise RuntimeError(f"Notebook is missing writefile cells for: {sorted(waiting)}")
    temp = NOTEBOOK.with_suffix(".ipynb.tmp")
    temp.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(NOTEBOOK)
    print(f"Synchronized {len(MODULES)} modules into {NOTEBOOK.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
