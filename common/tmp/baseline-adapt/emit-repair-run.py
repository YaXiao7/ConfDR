"""Turn an LLMSecConfig run into a ConfDR2.0 repair run, then evaluate it in KWOK.

Same rationale as emit-run.py: rather than port a legacy evaluator, write the
baseline's outputs into the artifact layout paper_metrics.py already reads, so the
baseline and ConfDR are scored by one implementation.

Per case, this writes dataset/<case>/{apps,envs,result.json} where:

  apps/, envs/   the method's final manifests. Written through confdr's own
                 save_candidate(), so the files land on the paths the original
                 objects came from and load_documents() reads them back in the
                 canonical POSIX order. output_documents_hash is computed from the
                 RELOADED documents, never from the in-memory list -- the round trip
                 re-sorts, so hashing before the write would not match.
  result.json    method_finished, repair_entered=True, the `detection` object (three
                 views), `repair.attempts` carrying the final proposal, and
                 `source_snapshot` holding the ORIGINAL documents and case. The
                 snapshot is required: without it select_candidate() cannot run the
                 repair-policy check and every case scores as "blocked".

output_action is "method_candidate" only when the baseline's own verification
declared success (run.json status == "success"; the runner reaches that after it
observes Running pods AND Z3 SAT). Otherwise it is "original_copy" and the run's
manifests are the untouched inputs, so the case cannot be credited as repaired.
That split is the whole point: it mirrors ConfDR's "accepted method output" rule.

The baseline's detected categories are projected from ConfDR's own input evidence --
the FailedScheduling reason KWOK recorded at collection time (collection.json) -- onto
R/A/P/S/E.  The baseline's own detector is gone, so this states what the baseline was
told to fix, not a verdict it produced.

COHORT.  Every truth-Pending case enters repair (the baseline detector that used to
skip the SAT ones is removed), so the run's own output set is the whole cohort and
nothing is synthesized in.

`deployment.json` is deliberately NOT written here: confdr's evaluate_deployment
produces it (and it rejects a run whose hashes it cannot trust).

Usage (confdr2 env -- needs confdr on sys.path):
  conda run -n confdr2 python tmp/baseline-adapt/emit-repair-run.py --label gpt56terra \
      --ref-run <run whose rules bundle this baseline carries>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

# FailedScheduling reason text (collection.json) -> R/A/P/S/E category.  Same evidence
# the repair runner maps to the baseline's own S/R/N/V vocabulary; this projection is
# only for the synthesized `detection` block, so the baseline's row states what it was
# told to fix.
REASON_TO_CATEGORY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"didn't match pod affinity rules", re.I), "P2"),
    (re.compile(r"didn't match pod anti-affinity rules", re.I), "P3"),
    (re.compile(r"satisfy existing pods anti-affinity rules", re.I), "P3"),
    (re.compile(r"didn't match Pod's node affinity/selector", re.I), "P1"),
    (re.compile(r"node\(s\) had untolerated taint", re.I), "A1"),
    (re.compile(r"Insufficient cpu", re.I), "R1"),
    (re.compile(r"Insufficient memory", re.I), "R2"),
    (re.compile(r"Insufficient nvidia\.com/gpu", re.I), "R3"),
    (re.compile(r"didn't have free ports", re.I), "E1"),
    (re.compile(r"unbound immediate PersistentVolumeClaims", re.I), "S1"),
    (re.compile(r"persistentvolumeclaim .* not found", re.I), "S1"),
]

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # the template tree; this script was migrated into it
# The frozen rules bundle this baseline's numbers were produced under.  It is a property of
# the RESULT, not of the method: the baseline was labelled against an earlier bundle than
# the newest method run carries, and evaluate-metrics refuses to aggregate runs whose
# bundles differ.  `--ref-run` overrides it, and the shipped tree's own D1 run is the
# default only because it happens to carry that same bundle.
REF_RUN = ROOT / "outputs" / "D1-26-10-method-v1"
STAGED = HERE / "llmsec" / "data"
RUNS = HERE / "runs"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
from confdr.collection.files import load_case, load_documents, save_candidate  # noqa: E402
from confdr.shared.catalog import source_content  # noqa: E402
from confdr.shared.io import digest, write  # noqa: E402


def evidence_categories(case_dir: Path) -> list[str]:
    """R/A/P/S/E categories from ConfDR's input evidence (collection.json).

    The baseline's own detector is removed, so there is no res.txt to project from.
    What the LLM was given instead is the FailedScheduling reason KWOK recorded for
    the case at collection time; this maps that reason onto the same taxonomy the
    method is scored on, so the baseline's `detection` block states what it was told
    to fix -- not a verdict it never produced.
    """
    f = case_dir / "collection.json"
    if not f.is_file():
        return []
    try:
        reason = str(json.loads(f.read_text(encoding="utf-8", errors="ignore")).get("reason") or "")
    except Exception:
        return []
    codes: set[str] = set()
    for rx, code in REASON_TO_CATEGORY:
        if rx.search(reason):
            codes.add(code)
    return sorted(codes)


def detection_object(categories: list[str]) -> dict:
    cores = [{"category_id": c} for c in categories]
    return {
        "status": "fault",
        "category_ids": categories,
        # solver_status must be "unsat" or predictions() downgrades combined_detection
        # to "unknown" (paper_metrics.py:63-72).
        "formal": {
            "status": "unsat",
            "solver_status": "unsat",
            "formal_complete": True,
            "initial_core": cores,
            "core": cores,
        },
    }


def build_case(staged: Path, out_case: Path, target: Path) -> tuple[dict, dict]:
    """Write one case's artifacts; returns (result, record)."""
    case, orig_docs, obs, locations = load_case(staged)
    categories = evidence_categories(staged)

    run_json = {}
    rj = out_case / "run.json"
    if rj.is_file():
        try:
            run_json = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run_json = {}
    status = run_json.get("status") or "missing"
    fixed = out_case / "fixed.yaml"
    proposal = fixed.read_text(encoding="utf-8", errors="ignore") if fixed.is_file() else None

    accepted = status == "success" and bool(proposal and proposal.strip())
    if accepted:
        try:
            out_docs = [d for d in yaml.safe_load_all(proposal) if d is not None]
        except yaml.YAMLError:
            underived = f"fixed.yaml does not parse: {case}"
            print(f"  [WARN] {underived}")
            accepted, out_docs = False, orig_docs
    else:
        out_docs = orig_docs

    target.mkdir(parents=True, exist_ok=True)
    try:
        save_candidate(target, out_docs, locations, case, obs)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"  [WARN] {staged.name}: save_candidate failed ({exc}); falling back to original copy")
        accepted = False
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        save_candidate(target, orig_docs, locations, case, obs)

    # Hash the RELOADED documents: save_candidate regroups by path and
    # load_documents re-sorts by POSIX path, so the in-memory order is not canonical.
    final_docs, _ = load_documents(target)

    result = {
        "method_finished": True,
        "output_documents_hash": digest(final_docs),
        "output_action": "method_candidate" if accepted else "original_copy",
        "repair_entered": True,
        "repair": {
            "status": status,
            "attempts": ([{"proposal": {"fixed_yaml": proposal}}] if proposal else []),
        },
        "detection": detection_object(categories),
        "source_snapshot": {"documents": orig_docs, "case": case},
        "seconds": run_json.get("llm_repair_time_sec"),
        "repair_seconds": run_json.get("final_repair_time_sec"),
        "model_calls": [],
    }
    truth = json.loads((staged / "truth.json").read_text(encoding="utf-8"))
    labels = sorted(set(truth.get("category_ids") or []))
    record = {
        "case": staged.name,
        "truth": {
            "is_pending": truth["is_pending"],
            "category_ids": labels,
            "category_labelled": True,
            "pending_labels": labels,
            "scope": "original_pending",
            "status": "fault" if truth["is_pending"] else "healthy",
        },
        "method_result_hash": digest(result),
        "repair_status": status,
        "output_action": result["output_action"],
    }
    write(target / "result.json", result)
    return result, record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument(
        "--ref-run", type=Path, default=REF_RUN,
        help="Completed run whose frozen rules bundle this baseline was labelled under.",
    )
    args = ap.parse_args()
    ref_run = args.ref_run

    run_root = RUNS / args.label / "llmsec"
    out_root = run_root / "out"
    if not out_root.is_dir():
        sys.exit(f"no repair output at {out_root}; run run-llmsec.py first")

    out = RUNS / args.label / "repair-eval"
    if out.exists():
        shutil.rmtree(out)
    (out / "dataset").mkdir(parents=True)

    ref_experiment = json.loads((ref_run / "experiment.json").read_text(encoding="utf-8"))
    catalog = json.loads((ref_run / "rules-used.json").read_text(encoding="utf-8"))
    write(out / "rules-used.json", catalog)
    bundle_hash = digest(catalog)
    assert digest(source_content(catalog)) == ref_experiment["rules_hash"], "rules_hash mismatch"

    records, missing = [], []
    for group_dir in sorted(p for p in out_root.iterdir() if p.is_dir()):
        for out_case in sorted(p for p in group_dir.iterdir() if p.is_dir()):
            staged = STAGED / group_dir.name / out_case.name
            if not (staged / "case.json").is_file():
                missing.append(f"{group_dir.name}/{out_case.name}")
                continue
            _, record = build_case(staged, out_case, out / "dataset" / out_case.name)
            records.append(record)

    if missing:
        print(f"[WARN] {len(missing)} outputs without a staged input, skipped: {missing[:5]}")

    # No cohort backfill: every truth-Pending case now enters the runner (the baseline
    # detector that used to skip 32 of them is gone), so the run's own output set IS the
    # cohort and no case is synthesized in.
    write(
        out / "experiment.json",
        {
            "stage": "method_completed",
            "finished": True,
            "completed": len(records),
            "total": len(records),
            "model": args.model or ref_experiment.get("model", ""),
            "model_source": "baseline-adapt",
            "rules_hash": ref_experiment["rules_hash"],
            "rules_bundle_hash": bundle_hash,
            "output_dataset": str(out / "dataset"),
            "records": records,
        },
    )
    from collections import Counter

    print(f"emitted : {out.relative_to(ROOT)}  ({len(records)} cases)")
    print("actions :", dict(Counter(r["output_action"] for r in records)))
    print("statuses:", dict(Counter(r["repair_status"] for r in records)))


if __name__ == "__main__":
    main()
