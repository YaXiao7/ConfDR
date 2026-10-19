"""Add the 32 `--healthy` variants to dataset-sls/ so detection is scored on 191.

WHY
---
The ConfDR run (`outputs/D1-26-10-method-v1`) covers 191 cases: the 159 pending
ones from `Dataset/D1-26-10` plus 32 `--healthy` variants (same manifests with the
injected fault removed, `is_pending=false`). The baseline was adapted from the
159 only, so its Precision and Accuracy were computed with TN=0 -- structurally
lower than ConfDR's row, on a different case set. Detection is being extended to
the full 191 so RQ1 is comparable; the repair cohort stays at the 127 UNSAT cases
and that denominator difference is declared separately (README).

WHAT IT DOES
------------
Second source, same recipe as adapt_dataset.py's SLS branch: for each healthy case
bundle `envs/` + `apps/` into one multi-document deploy.yaml in the same POSIX
order, and copy truth.json. Writes into the EXISTING dataset-sls/ without wiping
it (adapt_dataset.py rmtree's it). Idempotent: a case is overwritten in place.

The healthy bundle lives in the ConfDR output tree, not in Dataset/, because it is
a ConfDR-side derivation of the pending cases -- its `case.json`/`result.json`/
`res.txt` are ConfDR run artifacts and are deliberately not read here.

Usage:
  python tmp/baseline-adapt/adapt-healthy.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# The frozen v3 collection, not the method-v1 run tree: it carries the same 32 healthy
# cases (byte-identical truth.json) and is what ships with the tree.  A run tree is an
# output of running the method, so it cannot be a dependency of adapting a baseline.
SRC = ROOT / "outputs" / "D1-collect-26-10-v3" / "dataset"
SLS = HERE / "dataset-sls"
PENDING = ROOT / "Dataset" / "D1-26-10"

sys.path.insert(0, str(HERE))
from adapt_dataset import manifest_paths  # noqa: E402  -- one ordering rule, shared


def main() -> None:
    if not SRC.is_dir():
        sys.exit(f"missing healthy source: {SRC}")
    healthy = sorted(
        p for p in SRC.iterdir() if p.is_dir() and p.name.endswith("--healthy") and (p / "truth.json").is_file()
    )
    if not healthy:
        sys.exit(f"no --healthy cases under {SRC}")

    # A healthy case must have a pending sibling: it is the same manifests with the
    # fault removed, and emit-run.py joins predictions to truth by directory name.
    pending = {p.name for p in PENDING.iterdir() if p.is_dir() and (p / "truth.json").is_file()}
    orphans = [c.name for c in healthy if c.name[: -len("--healthy")] not in pending]
    if orphans:
        sys.exit(f"{len(orphans)} healthy cases have no pending sibling: {orphans[:5]}")

    written, empty, not_healthy = 0, [], []
    for case in healthy:
        paths = manifest_paths(case)
        if not paths:
            empty.append(case.name)
            continue
        raw = json.loads((case / "truth.json").read_text(encoding="utf-8"))
        # Guard the premise: these are here to be counted as true negatives.
        if raw.get("is_pending") is not False or raw.get("pending_labels"):
            not_healthy.append(case.name)
            continue

        d = SLS / case.name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

        parts = []
        for p in paths:
            text = p.read_text(encoding="utf-8", errors="ignore").rstrip("\n")
            parts.append(f"# {p.relative_to(case).as_posix()}\n{text}")
        (d / "deploy.yaml").write_text("\n---\n".join(parts) + "\n", encoding="utf-8")
        # Same normalized shape emit-run.py expects (it only reads is_pending +
        # pending_labels, both already correct here).
        (d / "truth.json").write_text(
            json.dumps(
                {
                    **raw,
                    "category_ids": [],
                    "category_labelled": True,
                    "scope": raw.get("scope", "original_pending"),
                    "status": "healthy",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        written += 1

    total = sum(1 for p in SLS.iterdir() if p.is_dir() and (p / "deploy.yaml").is_file())
    print(f"healthy src : {SRC}  ({len(healthy)} cases)")
    print(f"added       : {written} -> dataset-sls/")
    print(f"dataset-sls : {total} deploy.yaml total")
    if empty:
        print(f"[WARN] no manifests: {empty}")
    if not_healthy:
        print(f"[SKIP] truth is not healthy: {not_healthy}")


if __name__ == "__main__":
    main()
