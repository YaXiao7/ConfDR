"""Rebuild the baseline input trees from the FROZEN v3 collection.

Why this exists.  `adapt_dataset.py` reads `Dataset/D1-26-10`, which is a live
directory: it was silently re-collected under the adapters' feet.  The trees the
baselines were actually scored on (dataset-sls, dataset-repair) were generated on
10-08 from a pre-v3 state, so they disagree with the frozen input the METHOD is
scored on (`outputs/D1-collect-26-10-v3/dataset`).  Measured on 10-12:

  * dataset-sls/deploy.yaml differs for 17 cases -- 12 quote-only (YAML-neutral)
    and 5 genuinely rebuilt (v3's `rebuilt_cases`);
  * dataset-sls/truth.json is stale for 3 cases (S2 -> S3, S3; S2 -> S4);
  * dataset-repair carries the same 3 stale labels;
  * Dataset/D1-26-10 now has NO semantic difference from v3 (159/159 equal), so
    re-running the old adapter would already have been closer -- but it still only
    holds 159 cases (no --healthy controls) and it is not frozen.

Sourcing from v3 instead makes the baseline inputs a function of the same frozen
artifact the method consumes, and it is the only tree that carries all 191 cases
(159 pending + 32 healthy), which is the detection denominator.

Outputs, replacing the old trees (backed up to backup/v3-rebuild/):
  dataset-sls/<case>/deploy.yaml   multi-doc YAML in ConfDR2.0 document order
  dataset-sls/<case>/truth.json    verbatim
  dataset-repair/<case>/apps,envs  verbatim
  dataset-repair/<case>/truth.json normalized (category_ids/category_labelled/...)
  dataset-repair/<case>/case.json  verbatim -- the legacy Z3 solver's only input

The legacy Z3 verdict (res.txt) is derived from case.json ALONE, and v3's
case.json differs semantically from the old collection's for dozens of cases, so
the repair cohort has to be re-derived after this.  Run run-legacy-detect.py next.

Usage:
  python tmp/baseline-adapt/rebuild-v3-inputs.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
V3 = ROOT / "outputs" / "D1-collect-26-10-v3" / "dataset"
SLS = HERE / "dataset-sls"
REPAIR = HERE / "dataset-repair"

sys.path.insert(0, str(HERE))
from adapt_dataset import manifest_paths, normalize_truth  # noqa: E402


def main() -> None:
    if not V3.is_dir():
        sys.exit(f"missing frozen collection: {V3}")
    cases = sorted(p for p in V3.iterdir() if p.is_dir())
    if len(cases) != 191:
        sys.exit(f"expected 191 cases in the frozen tree, found {len(cases)}")

    for tree in (SLS, REPAIR):
        if tree.exists():
            shutil.rmtree(tree)

    pending = healthy = 0
    empty = []
    for case in cases:
        paths = manifest_paths(case)
        if not paths:
            empty.append(case.name)
            continue

        # --- SlsDetector: one concatenated multi-document YAML ---
        d = SLS / case.name
        d.mkdir(parents=True)
        parts = []
        for p in paths:
            text = p.read_text(encoding="utf-8", errors="ignore").rstrip("\n")
            parts.append(f"# {p.relative_to(case).as_posix()}\n{text}")
        (d / "deploy.yaml").write_text("\n---\n".join(parts) + "\n", encoding="utf-8")
        shutil.copy2(case / "truth.json", d / "truth.json")

        # --- repair: only the Pending cases enter the repair cohort ---
        raw = json.loads((case / "truth.json").read_text(encoding="utf-8"))
        if not raw.get("is_pending"):
            healthy += 1
            continue
        r = REPAIR / case.name
        r.mkdir(parents=True)
        for part in ("apps", "envs"):
            if (case / part).is_dir():
                shutil.copytree(case / part, r / part)
        (r / "truth.json").write_text(
            json.dumps(normalize_truth(raw), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        shutil.copy2(case / "case.json", r / "case.json")
        pending += 1

    print(f"source : {V3.relative_to(ROOT)}  ({len(cases)} cases)")
    print(f"sls    : {SLS.name}  ({pending + healthy} deploy.yaml)")
    print(f"repair : {REPAIR.name}  ({pending} cases, {healthy} healthy controls kept out)")
    if empty:
        print(f"[WARN] no manifests for: {empty}")

    # The solver's case.json must exist for every repair case or run-legacy-detect
    # will silently skip it and shrink the cohort.
    missing = [p.name for p in REPAIR.iterdir() if p.is_dir() and not (p / "case.json").is_file()]
    if missing:
        sys.exit(f"[FAIL] {len(missing)} repair cases without case.json: {missing[:5]}")
    print("verify : every repair case has apps/, envs/, truth.json and case.json")


if __name__ == "__main__":
    main()
