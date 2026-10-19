"""Adapt ConfDR2.0's D1-26-10 dataset into each baseline's expected input layout.

The new dataset stores one case as `apps/*.yaml` + `envs/*.yaml` + `truth.json`.
Neither baseline consumes that shape directly:

  * SlsDetector.py:153 reads exactly one file per case, `case_dir/deploy.yaml`,
    as a single multi-document YAML blob (Nodes + workloads concatenated).
  * The LLMSecConfig repair runner (utils/io.py:9-23) globs `**/*.y*ml` under the
    case dir and additionally gates on `case_dir/res.txt` (runner.py:423-429).

So we emit two sibling trees rather than one: the repair runner globs recursively,
so leaving a concatenated `deploy.yaml` next to `apps/`/`envs/` would feed it every
document twice.

Outputs (under tmp/baseline-adapt/):
  dataset-sls/<case>/deploy.yaml   single multi-doc YAML, same document order as
                                   ConfDR2.0's load_documents (see note below)
  dataset-sls/<case>/truth.json    ground truth, unmodified copy
  dataset-repair/<case>/apps,envs  verbatim copies for the repair runner
  dataset-repair/<case>/truth.json normalized: adds category_ids + category_labelled,
                                   which confdr's paper_metrics._case_record requires
                                   (paper_metrics.py:454) and the raw truth.json lacks

Document order: load_documents() (collection/files.py:59-72) sorts the manifest
paths by their POSIX form. Every `apps/` path sorts before every `envs/` path
('a' < 'e'), and the sort is global, not per-directory. We reproduce exactly that
so the LLM sees the same documents in the same order the method does.

Usage:
  D:\\Anaconda_envs\\envs\\ConfDR\\python.exe tmp/baseline-adapt/adapt_dataset.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the template tree
# Before the migration this script lived in the sibling checkout and read the dataset from
# `ROOT.parent / "2.0" / "ConfDR2.0"`.  It now lives in the template tree itself, which carries
# its own copy of D1-26-10 (159 cases, same as the sibling's), so the sibling lookup is gone.
# See the changelog, section 4.3.
SRC = ROOT / "Dataset" / "D1-26-10"
OUT = ROOT / "tmp" / "baseline-adapt"
SLS = OUT / "dataset-sls"
BASE = ROOT.parent  # display root: the directory holding the template tree
REPAIR = OUT / "dataset-repair"


def disp(p: Path) -> str:
    """Path relative to the checkout root when possible, else absolute."""
    try:
        return str(p.relative_to(BASE))
    except ValueError:
        return str(p)


def manifest_paths(case: Path) -> list[Path]:
    """The yaml files of one case in ConfDR2.0's document order."""
    paths = [
        p
        for part in ("envs", "apps")
        for p in (case / part).rglob("*")
        if p.suffix in {".yaml", ".yml"} and not any(x.startswith(".") for x in p.relative_to(case).parts)
    ]
    # Match collection/files.py: the key is the POSIX form of the path. On an
    # absolute path the shared prefix is identical across cases, so comparing the
    # relative form gives the same order -- and, unlike PurePath.__lt__ on Windows,
    # it does not case-fold.
    return sorted(paths, key=lambda p: p.as_posix())


def normalize_truth(raw: dict) -> dict:
    """Add the fields confdr's paper_metrics reads but the dataset does not store."""
    pending = raw.get("pending_labels", [])
    return {
        **raw,
        # _case_record requires category_labelled is True and category_ids to be a
        # list before it will score a category (paper_metrics.py:454-457).
        "category_ids": list(pending),
        "category_labelled": True,
        "scope": raw.get("scope", "original_pending"),
        "status": raw.get("status", "fault" if raw.get("is_pending") else "healthy"),
    }


def main() -> None:
    if not SRC.is_dir():
        sys.exit(f"missing dataset: {SRC}")

    cases = sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "truth.json").is_file())
    if not cases:
        sys.exit(f"no cases under {SRC}")

    for tree in (SLS, REPAIR):
        if tree.exists():
            shutil.rmtree(tree)

    written = {"sls": 0, "repair": 0, "truth_normalized": 0, "empty_manifest": []}
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

        # --- repair: verbatim apps/ + envs/, plus normalized truth ---
        r = REPAIR / case.name
        r.mkdir(parents=True)
        for part in ("apps", "envs"):
            if (case / part).is_dir():
                shutil.copytree(case / part, r / part)
        raw = json.loads((case / "truth.json").read_text(encoding="utf-8"))
        (r / "truth.json").write_text(
            json.dumps(normalize_truth(raw), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written["repair"] += 1
        written["truth_normalized"] += 1

    print(f"source : {disp(SRC)}  ({len(cases)} cases)")
    print(f"sls    : {disp(SLS)}  ({written['sls']} deploy.yaml)")
    print(f"repair : {disp(REPAIR)}  ({written['repair']} cases)")
    if written["empty_manifest"]:
        print(f"[WARN] no manifests for: {written['empty_manifest']}")

    # Sanity: every emitted deploy.yaml must parse as YAML with >0 documents, and
    # the document count must equal the number of source manifest files.
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
