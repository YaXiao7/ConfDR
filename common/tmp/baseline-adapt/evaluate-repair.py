"""Independently evaluate an LLMSecConfig repair run in KWOK, then score it.

Calls confdr's own `evaluate_deployment` + `evaluate_metrics` rather than the CLI,
for one reason: the CLI builds an LLM advisor and generates `deployment-advice.md`
for every failed case (cli.py:272-282). That is extra API spend with no effect on
any reported number. Passing llm=None keeps the run free and deterministic.

What this reproduces, per case: KWOK deploys the ORIGINAL manifests and the
method's submitted manifests, and confdr's frozen formal rules re-detect both
snapshots. From that come deployment status, observed Pending, and the
before/after category sets behind CVRR / Average Introduced Errors. It is the
same independent verification ConfDR's own row was scored under, which is what
makes the two rows comparable.

Usage (confdr2 env, cwd = the ConfDR2.0 project so config/settings.yaml resolves):
  cd <template tree>            # the project root, i.e. this script's parents[2]
  conda run -n confdr2 python tmp/baseline-adapt/evaluate-repair.py --run <path> --limit N
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The project root must be importable and be the cwd: confdr.shared.io.settings
# resolves config/settings.yaml relative to the installed package root.
# Was `parents[3] / "2.0" / "ConfDR2.0"` -- one level up from the sibling checkout this script
# used to live in.  Inside the template tree the project root *is* parents[2].  See the changelog,
# section 4.3.
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "src"))

from confdr.shared.io import settings  # noqa: E402
from confdr.validation.deployment import evaluate_deployment  # noqa: E402
from confdr.validation.kwok import KwokValidator  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = settings(Path(args.config) if args.config else None)
    summary = evaluate_deployment(
        Path(args.run),
        KwokValidator(cfg.get("kwok")),
        llm=None,
        progress=lambda text: print(text, flush=True),
    )
    print("\n=== deployment summary ===")
    for key in ("finished", "total", "completed", "statuses", "candidate_counts",
                "candidate_deployment_rate", "seconds"):
        print(f"  {key}: {summary.get(key)}")


if __name__ == "__main__":
    main()
