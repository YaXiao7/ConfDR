"""Adapt the D2 collection (40 scheduling cases) into each baseline's input layout.

Same two-tree recipe as adapt_dataset.py, but the source is the FROZEN D2 collection
(`outputs/D2-collect-server-01/dataset`), not `Dataset/D2`.  The collection is what
the method was scored on, and it carries the two evidence files the repair baseline
now consumes instead of its own detector:

  case.json        the solver's cluster file (WaitingDeployPods + ClusterState)
  collection.json  the FailedScheduling reason KWOK actually observed for the case

README of the D2 bundle states apps/ is byte-identical between Dataset/D2 and the
collection, and envs/ differs only by a YAML round trip; so staging from the
collection is staging ConfDR's own input, which is what the new repair semantics
require.

Outputs (under tmp/baseline-adapt/):
  dataset-sls-d2/<case>/deploy.yaml   one multi-doc YAML, ConfDR document order
  dataset-sls-d2/<case>/truth.json    ground truth, unmodified
  dataset-repair-d2/<case>/           apps/, envs/, truth.json (normalized),
                                      case.json, collection.json

Usage:
  .venv/bin/python tmp/baseline-adapt/adapt-d2.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "outputs" / "D2-collect-server-01" / "dataset"
SLS = HERE / "dataset-sls-d2"
REPAIR = HERE / "dataset-repair-d2"

sys.path.insert(0, str(HERE))
from adapt_dataset import manifest_paths, normalize_truth  # noqa: E402


def main() -> None:
    if not SRC.is_dir():
        sys.exit(f"missing D2 collection: {SRC}")

    cases = sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "truth.json").is_file())
    if not cases:
        sys.exit(f"no cases under {SRC}")

    for tree in (SLS, REPAIR):
        if tree.exists():
            shutil.rmtree(tree)

    written = {"sls": 0, "repair": 0, "empty_manifest": [], "missing_case_json": []}
    for case in cases:
        paths = manifest_paths(case)
        if not paths:
            written["empty_manifest"].append(case.name)
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
        written["sls"] += 1

        # --- repair: verbatim apps/ + envs/, normalized truth, and the two evidence files ---
        r = REPAIR / case.name
        r.mkdir(parents=True)
        for part in ("apps", "envs"):
            if (case / part).is_dir():
                shutil.copytree(case / part, r / part)
        raw = json.loads((case / "truth.json").read_text(encoding="utf-8"))
        (r / "truth.json").write_text(
            json.dumps(normalize_truth(raw), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for ev in ("case.json", "collection.json"):
            if (case / ev).is_file():
                shutil.copy2(case / ev, r / ev)
            elif ev == "case.json":
                written["missing_case_json"].append(case.name)
        written["repair"] += 1

    print(f"source : {SRC.relative_to(ROOT)}  ({len(cases)} cases)")
    print(f"sls    : {SLS.relative_to(ROOT)}  ({written['sls']} deploy.yaml)")
    print(f"repair : {REPAIR.relative_to(ROOT)}  ({written['repair']} cases)")
    if written["empty_manifest"]:
        print(f"[WARN] no manifests: {written['empty_manifest']}")
    if written["missing_case_json"]:
        print(f"[WARN] no case.json: {written['missing_case_json']}")

    # Sanity: every deploy.yaml parses, doc count == source file count.
    import yaml

    bad = []
    for case in cases:
        p = SLS / case.name / "deploy.yaml"
        if not p.is_file():
            continue
        n_src = len(manifest_paths(case))
        try:
            n_doc = sum(1 for doc in yaml.safe_load_all(p.read_text(encoding="utf-8")) if doc is not None)
        except Exception as exc:  # noqa: BLE001
            bad.append((case.name, f"parse: {exc}"))
            continue
        if n_doc < n_src:
            bad.append((case.name, f"docs={n_doc} < files={n_src}"))
    if bad:
        print(f"[FAIL] {len(bad)} cases malformed, e.g. {bad[:3]}")
        sys.exit(1)
    print("verify : all deploy.yaml parse and document counts match source file counts")


if __name__ == "__main__":
    main()
