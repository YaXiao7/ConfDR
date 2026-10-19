#!/usr/bin/env python3
"""DeployFix repair experiment: run --recommend on the cases detected as conflicting, save all outputs and recheck.

Saved for each case:
  <out>/<case>/detect/      all output of the first k8s go run (conflicts/definitions/dump, no solution)
  <out>/<case>/repair/      output with --recommend (recommendations.yaml + solution/*.yaml)
  <out>/<case>/recheck/     output of the rerun after overwriting the sources with the solution
  <out>/<case>/meta.json    exit codes, number of recommendations, solution files, whether the recheck is clean

Verdict:
  fixed      = recheck exit code 0
  still      = recheck exit code 1
  crash      = repair exit code other than 1/0 (e.g. 101 panic)
  no_solution= repair exits with 1 but writes no solution/

Usage:
  python3 -I deployfix-repair-run.py --bin <cli> --dataset <dir> --detect <json> --out <dir> [--env-mode fed|free]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time


def prep(case_dir: str, work: str, overlay: str | None = None):
    src, dummy, out = (os.path.join(work, n) for n in ("src", "dummy", "out"))
    for p in (src, dummy, out):
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p)
    apps = os.path.join(case_dir, "apps")
    for name in sorted(os.listdir(apps)):
        if name.endswith(".yaml"):
            shutil.copy2(os.path.join(apps, name), os.path.join(src, name))
    if overlay and os.path.isdir(overlay):
        for name in sorted(os.listdir(overlay)):
            if name.endswith(".yaml"):
                shutil.copy2(os.path.join(overlay, name), os.path.join(src, name))
    return src, dummy, out


def run(binary, src, dummy, out, env_file, recommend):
    cmd = [binary, "k8s", "go", src, dummy, out, "--cycle-check"]
    if env_file:
        cmd += ["--env-file", env_file]
    if recommend:
        cmd += ["--recommend"]
    started = time.time()
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    return proc.returncode, round(time.time() - started, 4)


def copy_tree(src, dst):
    if os.path.isdir(src):
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--detect", required=True, help="detection result json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-mode", choices=["fed", "free"], default="fed")
    args = ap.parse_args()

    detect = json.load(open(args.detect, encoding="utf-8"))
    faults = sorted(c for c, v in detect.items() if v["status"] == "fault")
    os.makedirs(args.out, exist_ok=True)

    summary = {}
    for index, case in enumerate(faults, 1):
        case_dir = os.path.join(args.dataset, case)
        dest = os.path.join(args.out, case)
        os.makedirs(dest, exist_ok=True)
        env_file = None
        if args.env_mode == "fed":
            candidate = os.path.join(case_dir, "deployfix", "node-env.txt")
            env_file = candidate if os.path.isfile(candidate) else None

        work = os.path.join(dest, "_work")
        try:
            src, dummy, out = prep(case_dir, work)
            rc_detect, t_detect = run(args.bin, src, dummy, out, env_file, False)
            copy_tree(out, os.path.join(dest, "detect"))

            src, dummy, out = prep(case_dir, work)
            rc_repair, t_repair = run(args.bin, src, dummy, out, env_file, True)
            copy_tree(out, os.path.join(dest, "repair"))

            sol = os.path.join(dest, "repair", "solution")
            sol_files = sorted(f for f in os.listdir(sol) if f.endswith(".yaml")) if os.path.isdir(sol) else []
            rec_path = os.path.join(dest, "repair", "recommendations.yaml")
            rec_lines = 0
            if os.path.isfile(rec_path):
                rec_lines = len([l for l in open(rec_path, encoding="utf-8")
                                 if l.strip().startswith("- ")])

            rc_recheck = None
            if rc_repair == 1 and sol_files:
                src, dummy, out = prep(case_dir, work + "-recheck", overlay=sol)
                rc_recheck, _ = run(args.bin, src, dummy, out, env_file, False)
                copy_tree(out, os.path.join(dest, "recheck"))

            if rc_repair not in (0, 1):
                outcome = "crash"
            elif not sol_files:
                outcome = "no_solution"
            elif rc_recheck == 0:
                outcome = "fixed"
            else:
                outcome = "still"

            meta = {
                "case": case,
                "env_mode": args.env_mode,
                "env_file": bool(env_file),
                "detect_rc": rc_detect,
                "repair_rc": rc_repair,
                "recheck_rc": rc_recheck,
                "recommendations": rec_lines,
                "solution_files": sol_files,
                "outcome": outcome,
                "seconds_detect": t_detect,
                "seconds_repair": t_repair,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(work + "-recheck", ignore_errors=True)

        summary[case] = meta
        with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
        print(f"[{index}/{len(faults)}] {case:55s} {outcome:11s} "
              f"rec={rec_lines} sol={len(sol_files)} recheck={rc_recheck}", flush=True)

        with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1, sort_keys=True)

    with open(os.path.join(args.out, "summary.tsv"), "w", encoding="utf-8") as fh:
        fh.write("case\tenv_file\toutcome\trecommendations\tsolution_files\trepair_rc\trecheck_rc\tseconds\n")
        for case in sorted(summary):
            m = summary[case]
            fh.write(f"{case}\t{m['env_file']}\t{m['outcome']}\t{m['recommendations']}\t"
                     f"{len(m['solution_files'])}\t{m['repair_rc']}\t{m['recheck_rc']}\t{m['seconds_repair']}\n")

    from collections import Counter
    print("\n", dict(Counter(m["outcome"] for m in summary.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
