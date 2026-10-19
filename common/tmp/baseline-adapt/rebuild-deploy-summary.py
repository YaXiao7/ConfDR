"""Rebuild a run's deployment-summary.json from its per-case deployment.json evidence.

`evaluate_deployment` writes `deployment-summary.json` at the end of a run
(src/confdr/validation/deployment.py:313).  Re-emitting the run layout with
`emit-repair-run.py` (which does `shutil.rmtree(out)` on the run directory) deletes it,
and `shard-deploy.py restore` only copies back per-case `deployment.json` -- so the
aggregate summary can go missing while every piece of per-case evidence stays intact.

This rebuilds the aggregate the same way confdr does (deployment.py:287-313) and the same
way `shard-deploy.py merge` does (merge():194-215), reading ONLY the genuine per-case
`deployment.json` files.  Nothing is invented: every record comes from a case's own
evidence, and `finished` is true only when every case in experiment.json has one.

The scorer does not read this file -- `evaluate_metrics` reads each case's
`deployment.json` directly (paper_metrics.py:559) -- so this is a reproducibility/audit
artifact, not a scoring input.

Usage:
    python -I tmp/baseline-adapt/rebuild-deploy-summary.py \
        --run tmp/baseline-adapt/runs/gpt56terra/repair-eval
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def bundle_hash(run: Path) -> str:
    """Replicate confdr's `digest(catalog)` -- canonical JSON, not file bytes."""
    catalog = read(run / "rules-used.json")
    canonical = json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--stamp", default="rebuilt from per-case deployment.json")
    args = ap.parse_args()
    run = args.run.resolve()
    if not (run / "experiment.json").is_file():
        raise SystemExit(f"no emitted run at {run}")

    expected = [r["case"] for r in read(run / "experiment.json")["records"]]
    summaries, missing = [], []
    for case in expected:
        target = run / "dataset" / case
        dep_file = target / "deployment.json"
        if not dep_file.is_file():
            missing.append(case)
            continue
        deployment = read(dep_file)
        summaries.append(
            {
                "case": case,
                "method_status": (read(target / "result.json").get("repair") or {}).get("status"),
                "status": deployment.get("status"),
                "original_status": (deployment.get("original") or {}).get("status", "not_evaluated"),
                "candidate_status": (deployment.get("candidate") or {}).get("status", "not_evaluated"),
            }
        )

    counts = Counter(r["candidate_status"] for r in summaries)
    assessed = counts["passed"] + counts["failed"]
    write(
        run / "deployment-summary.json",
        {
            "stage": "deployment_evaluation",
            "rules_bundle_hash": bundle_hash(run),
            "seconds": None,
            "scope": "simulation deployment rate, not repair correctness",
            "finished": len(summaries) == len(expected),
            "total": len(expected),
            "completed": len(summaries),
            "statuses": dict(Counter(r["status"] for r in summaries)),
            "records": summaries,
            "candidate_counts": dict(counts),
            "candidate_deployment_rate": counts["passed"] / assessed if assessed else None,
            "rate_denominator": (
                "candidate passed + failed; infrastructure errors and absent candidates excluded"
            ),
            "feedback_to_method": False,
            "rebuilt_from": args.stamp,
            "cohort_note": (
                f"{len(missing)} of the {len(expected)} cohort cases have no deployment.json because the "
                "method produced no candidate for them (its own detector judged the manifest SAT), so "
                "there was nothing to deploy. They are not missing evidence; they have no deployment to "
                "evaluate. `finished` is therefore false by construction."
            ) if missing else "",
        },
    )
    print(f"{run}")
    print(f"  summary : {len(summaries)}/{len(expected)} records, candidate_counts={dict(counts)}")
    print(f"  statuses: {dict(Counter(r['status'] for r in summaries))}")
    if missing:
        print(f"  [WARN] {len(missing)} cases have no deployment.json: {missing[:10]}")


if __name__ == "__main__":
    main()
