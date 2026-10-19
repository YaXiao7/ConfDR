"""Stage a baseline's input trees from a FROZEN ConfDR collection.

Generalizes adapt-d2.py so D1 and D2 use one recipe: the source is always the
collection the method was scored on, which is the input the new repair semantics
require (error evidence comes from collection.json, not the baseline's detector).

  --src     the collection root, i.e. <collection>/dataset
  --repair  build the repair tree (apps/, envs/, truth.json normalized, case.json,
            collection.json) -- what setup-llmsec.py copies from
  --sls     optionally build the SlsDetector tree (one concatenated deploy.yaml per case)

D1's detection tree (dataset-sls, 191 cases incl. the 32 healthy controls) is built by
adapt_dataset.py + adapt-healthy.py and is left alone; this script is used for D1 only to
rebuild the repair tree, and for D2 for both trees.

Usage:
  .venv/bin/python tmp/baseline-adapt/adapt-collection.py \
      --src outputs/D1-collect-26-10-v3/dataset --repair tmp/baseline-adapt/dataset-repair
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from adapt_dataset import manifest_paths, normalize_truth  # noqa: E402


def build_sls(case: Path, out: Path) -> bool:
    paths = manifest_paths(case)
    if not paths:
        return False
    d = out / case.name
    d.mkdir(parents=True, exist_ok=True)
    parts = []
    for p in paths:
        text = p.read_text(encoding="utf-8", errors="ignore").rstrip("\n")
        parts.append(f"# {p.relative_to(case).as_posix()}\n{text}")
    (d / "deploy.yaml").write_text("\n---\n".join(parts) + "\n", encoding="utf-8")
    shutil.copy2(case / "truth.json", d / "truth.json")
    return True


def build_repair(case: Path, out: Path) -> bool:
    if not manifest_paths(case):
        return False
    r = out / case.name
    r.mkdir(parents=True, exist_ok=True)
    for part in ("apps", "envs"):
        if (case / part).is_dir():
            shutil.copytree(case / part, r / part, dirs_exist_ok=True)
    raw = json.loads((case / "truth.json").read_text(encoding="utf-8"))
    (r / "truth.json").write_text(
        json.dumps(normalize_truth(raw), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for ev in ("case.json", "collection.json"):
        if (case / ev).is_file():
            shutil.copy2(case / ev, r / ev)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--repair", default="")
    ap.add_argument("--sls", default="")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        sys.exit(f"missing collection: {src}")
    if not (args.repair or args.sls):
        sys.exit("give --repair and/or --sls")

    cases = sorted(p for p in src.iterdir() if p.is_dir() and (p / "truth.json").is_file())
    if not cases:
        sys.exit(f"no cases under {src}")

    for out in filter(None, [args.repair, args.sls]):
        if Path(out).exists():
            shutil.rmtree(out)

    done = {"sls": 0, "repair": 0}
    for case in cases:
        if args.sls and build_sls(case, Path(args.sls)):
            done["sls"] += 1
        if args.repair and build_repair(case, Path(args.repair)):
            done["repair"] += 1

    print(f"source : {src}  ({len(cases)} cases)")
    if args.sls:
        print(f"sls    : {args.sls}  ({done['sls']} deploy.yaml)")
    if args.repair:
        print(f"repair : {args.repair}  ({done['repair']} cases, with case.json + collection.json)")

    if args.sls:
        import yaml

        bad = []
        for case in cases:
            p = Path(args.sls) / case.name / "deploy.yaml"
            if not p.is_file():
                continue
            n_src = len(manifest_paths(case))
            try:
                n_doc = sum(1 for d in yaml.safe_load_all(p.read_text(encoding="utf-8")) if d is not None)
            except Exception as exc:  # noqa: BLE001
                bad.append((case.name, f"parse: {exc}"))
                continue
            if n_doc < n_src:
                bad.append((case.name, f"docs={n_doc} < files={n_src}"))
        if bad:
            sys.exit(f"[FAIL] {len(bad)} deploy.yaml malformed, e.g. {bad[:3]}")
        print("verify : all deploy.yaml parse and document counts match")


if __name__ == "__main__":
    main()
