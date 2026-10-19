"""Print progress and an ETA for a `confdr experiment` method run.

The run already reports progress per case: `cli.py:336` hands `run_method` a `progress`
hook, so its log carries `[12/191] <case>: ConfDR detection and static repair` and then one
`Deployment evaluation <case>` line per case in the KWOK phase.  What it does not report is
the one thing a 3-hour wait needs -- how much longer the rest will take.  Six cases into a
191-case run looks exactly like six cases into a run that has hung, unless you count lines
and divide.

So this counts finished cases on disk and prints the count with an ETA beside it.  It is a
separate process because the tool has no hook that knows the total; it shares no state with
the run, and the run is unaffected if it dies.

Two phases, both observable on disk:

  method      one `dataset/<case>/result.json`  lands per case once the method finishes
              that case (repair included).  It runs first, for all cases.
  deployment  `dataset/<case>/deployment.json`  lands per case afterwards, in the KWOK
              re-check that `experiment` runs once the method phase is complete.

So the two phases are counted separately and joined at the point where result.json
stops growing.  Run it in the background next to the method:

    python -I tmp/baseline-adapt/watch-method.py --run outputs/D1-method-ds41flash --total 191 &

Format is chosen for `tail -f` on a log file: one line per tick, no carriage returns.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# The reference run's measured wall time per phase.  Used only to seed the very first
# ETA, before enough cases have finished to measure one; after that the ETA is the
# running mean, which self-corrects.  Both figures are real numbers from
# outputs/D1-method-ds-001 (DeepSeek, 191 cases): 10813.7 s for the method phase and
# 7146.3 s for KWOK, which is why the deployment phase is worth its own ETA -- a third of
# the total time hides behind the point where `result.json` stops growing.
REF_METHOD_S = 10813.7 / 191  # 56.6 s/case
REF_DEPLOY_S = 7146.3 / 191  # 37.4 s/case


def fmt(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True, help="the method run directory")
    ap.add_argument("--total", type=int, default=191)
    ap.add_argument("--every", type=float, default=60.0)
    ap.add_argument(
        "--wait", type=float, default=600.0,
        help="seconds to wait for the run directory to be created before giving up.  The "
             "run itself creates it, so this only bounds a run that never starts.",
    )
    args = ap.parse_args()

    run = (ROOT / args.run).resolve() if not args.run.is_absolute() else args.run

    total = args.total
    t0 = time.time()
    print(f"# watching {run}")
    print(f"# {total} cases; method phase then deployment phase", flush=True)

    # `confdr experiment` refuses an output directory that already exists
    # (runtime/experiment.py:32), so the caller cannot create it first -- this waits for
    # the run to create it instead of exiting.  A watcher that quit on startup because it
    # lost a race would be worse than no watcher: the run would look unmonitored.
    while not run.is_dir():
        if time.time() - t0 > args.wait:
            sys.exit(f"gave up after {args.wait:.0f}s: {run} never appeared")
        time.sleep(2.0)

    def count(name: str) -> int:
        return sum(1 for _ in run.glob(f"dataset/*/{name}"))

    # The method phase cannot be told apart from the deployment phase by file type alone
    # until the first deployment.json appears, so the phase is a latched flag: once
    # deployment.json starts growing, the method phase is over.
    phase = "method"
    method_done_at = None
    while True:
        time.sleep(args.every)
        results = count("result.json")
        deploys = count("deployment.json")
        elapsed = time.time() - t0

        if results >= total and deploys >= total:
            print(f"[{elapsed / 60:6.1f}m] all {total} cases have both outputs -- "
                  f"run should be finishing its final metrics write", flush=True)
            return

        if deploys > 1 and phase == "method":
            phase = "deployment"
            method_done_at = elapsed
            print(f"# method phase finished ({results}/{total}); deployment phase begins",
                  flush=True)

        if phase == "method":
            if results:
                per = elapsed / results
                eta = fmt(per * (total - results))
            else:
                per = REF_METHOD_S
                eta = fmt(REF_METHOD_S * total)
            print(f"[{elapsed / 60:6.1f}m] method {results:3d}/{total} "
                  f"({100.0 * results / total:5.1f}%)  {per:5.1f}s/case  ETA {eta}",
                  flush=True)
        else:
            # Deployments start from a partial count, so they are measured against their
            # own window rather than from t0.  A window too short to divide by -- the
            # first tick after the phase flip, or a run already finished when the watcher
            # started -- falls back to the reference rate instead of printing a rate of
            # 0.0 s/case, which would read as an ETA of zero.
            base = method_done_at if method_done_at is not None else elapsed
            window = elapsed - base
            if deploys and window >= deploys:
                per = window / deploys
                eta = fmt(per * (total - deploys))
            else:
                per = REF_DEPLOY_S
                eta = fmt(REF_DEPLOY_S * (total - deploys))
            print(f"[{elapsed / 60:6.1f}m] deploy {deploys:3d}/{total} "
                  f"({100.0 * deploys / total:5.1f}%)  {per:5.1f}s/case  ETA {eta}",
                  flush=True)


if __name__ == "__main__":
    main()
