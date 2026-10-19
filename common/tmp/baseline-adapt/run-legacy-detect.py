"""Produce each case's `res.txt` with the legacy ConfDR-v1 Z3 detector.

Why this is needed at all: LLMSecConfig does not use its own detector's verdict to
decide *what* to fix -- it uses it to decide *whether* to fix. runner.py:423-429
skips a case outright unless `case_dir/res.txt` exists, and
load_prediction_from_res (runner.py:44-79) only lets it through when that file
mentions UNSAT. That is the paper's "repair runs only where the method's own
detector reports UNSAT" rule, so the cohort LLMSecConfig is scored on is defined
entirely by this file. Without it the baseline would be scored on zero cases.

Wiring, traced end to end:
  main.py -> run_all_cases.py -> run_case.py -> solver/z3_solver_while.py:591
  run_solver_with_output(CASE_PATH, CASE_JSON)  ->  writes
      <case>/res.txt          round-by-round report (the gate reads this)
      <case>/solve_time.json  {"solve_time_sec": ...}
  config.py (repo root) reads DATASET_ROOT + CASE_ID from the environment, so we
  do NOT need the dead hardcoded paths in repair/config.py -- that module belongs
  to the repair half, not the detection half. We invoke the solver directly and
  skip main.py/run_case.py entirely: run_case.py shells out with a relative path
  and `repair/config.py` hardcodes a Linux DATASET_ROOT, both of which would need
  patching for no benefit.

Inputs: 2.0/D1-collect-26-10/dataset/<case>/case.json  (solver's cluster file:
"WaitingDeployPods" + "ClusterState"{Nodes,PVCs,PVs,StorageClasses}); all 159
cases of D1-26-10 have one.
Output: tmp/baseline-adapt/dataset-repair/<case>/{case.json,res.txt,solve_time.json}
        -- the same directory tree the repair runner globs, so apps/envs/truth.json
        already sit alongside and nothing has to be moved afterwards.

No LLM, no API spend: this is pure Z3.

Usage:
  python tmp/baseline-adapt/run-legacy-detect.py [--limit N] [--jobs 4]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # the template tree; used only for display paths
# The legacy Z3 solver is not part of ConfDR2.0, so it comes from the sldstst checkout that
# ships beside this tree.  The cluster file, however, must come from the FROZEN v3
# collection: the older `D1-collect-26-10/dataset` predates the v3 re-collection, and its
# case.json differs semantically from v3's for dozens of cases.  The solver's verdict is a
# function of case.json alone, so sourcing the stale one would re-derive the cohort from
# inputs the method is not scored on -- and would silently overwrite the v3 case.json staged
# into dataset-repair.  Point CONFDR_SLDSTST_DIR elsewhere to use a different checkout.
sys.path.insert(0, str(HERE))
from hostenv import SLDSTST  # noqa: E402

COLLECT = ROOT / "outputs" / "D1-collect-26-10-v3" / "dataset"
REPAIR = HERE / "dataset-repair"
SOLVER = SLDSTST / "solver" / "z3_solver_while.py"
SRC_ROOT = SOLVER.parents[1]  # sys.path root the solver appends for `config`


def stage_case(case: str) -> str | None:
    """Ensure the case's v3 case.json sits next to apps/envs; returns an error string or None."""
    src = COLLECT / case / "case.json"
    if not src.is_file():
        return f"no case.json in frozen collection: {src}"
    dst = REPAIR / case / "case.json"
    # rebuild-v3-inputs.py already staged the frozen file; copying the same bytes again is
    # wasted I/O on 159 cases, and comparing first makes an accidental source mismatch loud.
    if dst.is_file() and dst.read_bytes() == src.read_bytes():
        return None
    shutil.copy2(src, dst)
    return None


def run_one(case: str, timeout: int) -> tuple[str, str, float]:
    """Run the solver for one case. Returns (case, status, seconds)."""
    case_dir = REPAIR / case
    env = dict(os.environ)
    env["DATASET_ROOT"] = str(REPAIR)
    env["CASE_ID"] = case
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # z3 prints a fair amount; keep it out of our stdout.
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, str(SOLVER)],
            cwd=str(SRC_ROOT), env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return case, f"timeout>{timeout}s", time.time() - t0
    dt = time.time() - t0
    res = case_dir / "res.txt"
    if not res.is_file():
        tail = (p.stdout or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
        return case, f"no res.txt (rc={p.returncode}) {' | '.join(tail)[:200]}", dt
    return case, "unsat" if "UNSAT" in res.read_text(encoding="utf-8", errors="ignore") else "sat", dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=4, help="parallel solver processes")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--force", action="store_true", help="redo cases that already have res.txt")
    args = ap.parse_args()

    if not SOLVER.is_file():
        sys.exit(f"missing solver: {SOLVER}")
    if not REPAIR.is_dir():
        sys.exit(f"missing {REPAIR}; run adapt_dataset.py first")

    cases = sorted(p.name for p in REPAIR.iterdir() if p.is_dir())
    if args.limit:
        cases = cases[: args.limit]

    staged = []
    for c in cases:
        err = stage_case(c)
        if err:
            print(f"[WARN] {c}: {err}")
        else:
            staged.append(c)

    if not args.force:
        staged = [c for c in staged if not (REPAIR / c / "res.txt").is_file()]

    print(f"cases : {len(cases)}   to solve: {len(staged)}   jobs: {args.jobs}")
    print(f"out   : {REPAIR.relative_to(ROOT)}")
    if not staged:
        print("nothing to do")
        return

    t0 = time.time()
    tally: dict[str, int] = {}
    fails: list[tuple[str, str]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(run_one, c, args.timeout): c for c in staged}
        for fut in as_completed(futs):
            case, status, dt = fut.result()
            done += 1
            key = status.split("(")[0].split(">")[0]
            tally[key] = tally.get(key, 0) + 1
            if not status.startswith(("unsat", "sat")):
                fails.append((case, status))
            print(f"[{done}/{len(staged)}] {case[:48]:48s} {status:10s} {dt:6.2f}s")

    dt = time.time() - t0
    print(f"\n=== done in {dt/60:.1f} min ===")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:24s} {v}")
    if fails:
        print(f"\n[FAIL] {len(fails)}:")
        for c, s in fails[:10]:
            print(f"  {c}: {s}")

    # res.txt is the repair cohort gate, so report the cohort size explicitly.
    n_unsat = sum(1 for c in cases if (REPAIR / c / "res.txt").is_file()
                  and "UNSAT" in (REPAIR / c / "res.txt").read_text(encoding="utf-8", errors="ignore"))
    print(f"\nrepair cohort (res.txt contains UNSAT): {n_unsat}/{len(cases)}")


if __name__ == "__main__":
    main()
