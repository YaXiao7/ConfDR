"""Synthesize a ConfDR2.0-style run directory from a baseline's predictions, so
`confdr evaluate-metrics` scores the baseline with the SAME metric code, the same
taxonomy and the same case set as ConfDR itself.

Why synthesize instead of porting the legacy evaluators: the legacy scripts read
C1..C15 / S-R-N-V codes and would have to be rewritten; and any rewrite would sit
on a different metric implementation, which is exactly what makes baseline and
method numbers incomparable. Reusing paper_metrics.py removes that whole class of
drift.

What evaluate-metrics requires (src/confdr/runtime/paper_metrics.py):
  experiment.json : stage == "method_completed", finished, rules_hash, records[]
  rules-used.json : copied verbatim from the real run; its digest must equal both
                    rules_hash (via source_content) and rules_bundle_hash
  per case result.json : method_finished true, digest(result) == record's
                    method_result_hash, and a `detection` object
`_case_record` (paper_metrics.py:419) additionally calls load_documents() on the
case dir; we set output_documents_hash to null so a missing apps/envs is tolerated
(paper_metrics.py:429-432) and no manifests need to be copied.

Detection-only methods (SlsDetector) must set `repair_entered: false` explicitly.
Otherwise repair_metrics.repair_entered() (repair_metrics.py:15-18) falls back to
`detection.status == "fault"` and the detector would be credited with -- and scored
on -- a repair cohort it never entered.

Usage:
  conda run --no-capture-exec -n confdr2 python tmp/baseline-adapt/emit-run.py \
      --pred runs/gpt56terra/slsdetector/pred --label gpt56terra --kind detection

The frozen bundle comes from a reference run and must be the one the baseline's numbers were
produced under, not the newest one available: the LLM baselines were labelled against the older
rules bundle, so their rows carry `129ffdec7a…` / `7844ca5962…` (declared in the changelog,
section 7). `--ref-run` exists because that is not universal -- DeployFix is scored on the D2
case set and belongs in the same aggregate as ConfDR's D2 run, so its row has to carry that
run's bundle (`64e142f8…` / `ce2c2aa9…`). `evaluate-metrics` refuses to aggregate runs whose
bundles differ (paper_metrics.py:757), so this is a real constraint, not a bookkeeping detail.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # the template tree; this script was migrated into it
# Both the source and the default reference run are read from the template tree.  Before the
# migration this pointed at the sibling `ConfDR-Code/2.0/ConfDR2.0`, which no longer sits next to
# this tree at all, so the default silently resolved to a nonexistent path.
DEFAULT_REF_RUN = ROOT / "outputs" / "D1-26-10-method-v1"  # the LLM baselines' frozen bundle

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
from confdr.shared.catalog import source_content  # noqa: E402
from confdr.shared.io import digest, write  # noqa: E402
from labels import classify_reasons  # noqa: E402


def detection_from_prediction(pred: dict) -> dict:
    """Shape a baseline verdict into the `detection` object predictions() reads.

    The category codes are re-derived here from the stored `reasons` rather than
    read from the saved `pred_labels`. The mapping table is a research artifact
    that gets revised (it already has been, once), and the model's raw output --
    the field paths and its own description -- is what the table is a function of.
    Re-deriving keeps every scored number consistent with the table as it stands,
    without paying to re-run the model.
    """
    reasons = pred.get("reasons")
    labels = sorted(set(classify_reasons(reasons))) if reasons else sorted(set(pred.get("pred_labels") or []))
    cores = [{"category_id": c} for c in labels]
    if pred.get("pred_is_pending") is True:
        formal = {
            "status": "unsat",
            "solver_status": "unsat",
            "formal_complete": True,
            "initial_core": cores,
            "core": cores,
        }
        status = "fault"
    elif pred.get("pred_is_pending") is False:
        formal = {
            "status": "sat",
            "solver_status": "sat",
            "formal_complete": True,
            "initial_core": [],
            "core": [],
        }
        status = "healthy"
    else:
        formal = {"status": "unknown", "solver_status": "not_run", "initial_core": [], "core": []}
        status = "unknown"
    return {"status": status, "category_ids": labels, "formal": formal}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True, help="dir of per-case prediction JSONs")
    ap.add_argument("--truth", default=str(HERE / "dataset-sls"), help="dir of per-case truth.json")
    ap.add_argument("--label", required=True)
    ap.add_argument("--kind", default="detection", choices=["detection", "repair"])
    ap.add_argument("--model", default="")
    ap.add_argument(
        "--ref-run",
        default=str(DEFAULT_REF_RUN),
        help="Completed run whose frozen rules bundle this baseline was labelled under.",
    )
    ap.add_argument("--out", default="", help="Override the emitted run directory.")
    args = ap.parse_args()
    ref_run = Path(args.ref_run)

    pred_dir = (ROOT / args.pred) if not Path(args.pred).is_absolute() else Path(args.pred)
    truth_dir = Path(args.truth)
    out = (
        Path(args.out)
        if args.out
        else ROOT / "tmp" / "baseline-adapt" / "runs" / args.label / f"{args.kind}-eval"
    )
    if out.exists():
        shutil.rmtree(out)
    (out / "dataset").mkdir(parents=True)

    ref_experiment = json.loads((ref_run / "experiment.json").read_text(encoding="utf-8"))
    catalog = json.loads((ref_run / "rules-used.json").read_text(encoding="utf-8"))
    # Freeze the exact rule bundle ConfDR was scored under; evaluate-metrics rejects
    # a run whose catalog does not hash to the same values (paper_metrics.py:595-599).
    write(out / "rules-used.json", catalog)
    bundle_hash = digest(catalog)
    assert digest(source_content(catalog)) == ref_experiment["rules_hash"], "rules_hash mismatch"

    records = []
    missing = []
    for pf in sorted(pred_dir.glob("*.json")):
        case = pf.stem
        tf = truth_dir / case / "truth.json"
        if not tf.is_file():
            missing.append(case)
            continue
        pred = json.loads(pf.read_text(encoding="utf-8"))
        truth = json.loads(tf.read_text(encoding="utf-8"))
        labels = sorted(set(truth.get("pending_labels") or []))
        expected = {
            "is_pending": truth["is_pending"],
            "category_ids": labels,
            "category_labelled": True,
            "pending_labels": labels,
            "scope": "original_pending",
            "status": "fault" if truth["is_pending"] else "healthy",
        }
        detection = detection_from_prediction(pred)
        # paper_metrics.py:462 reads result["detection"]["seconds"] for
        # detection_end_to_end_seconds -- putting it at the top level silently
        # yields "recorded_cases": 0 and an N/A Avg.Times column.
        detection["seconds"] = pred.get("solve_time_sec")
        result = {
            "method_finished": True,
            "output_documents_hash": None,  # no manifests copied; see module docstring
            "repair_entered": False,        # detection-only: stay out of the repair cohort
            "detection": detection,
        }
        write(out / "dataset" / case / "result.json", result)
        records.append(
            {
                "case": case,
                "truth": expected,
                "method_result_hash": digest(result),
                "repair_status": "not_in_repair_cohort",
                "output_action": "none",
            }
        )

    if missing:
        print(f"[WARN] {len(missing)} predictions without truth, skipped: {missing[:5]}")

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
    print(f"emitted : {out.relative_to(ROOT)}  ({len(records)} cases, kind={args.kind})")


if __name__ == "__main__":
    main()
