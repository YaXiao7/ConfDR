#!/usr/bin/env python3
"""Run DeployFix detection on D1 (191 cases) and write the results as scorable predictions.

Differences from the upstream container script (which assumes sample/ and target/release inside /app):
  * the binary path is passed with --bin instead of being assumed inside the repository;
  * each case's apps/*.yaml are flattened into a separate scratch src/ (DeployFix's
    SOURCE_DIR is not recursive and only reads .yaml);
  * the case's deployfix/node-env.txt is passed with --env-file; this environment input
    supplies the domain of node labels;
  * --recommend is not used (only detection is evaluated here, and upstream --recommend panics on this corpus).

Verdict = process exit code: 1 = conflict, 0 = clean, anything else = error.
Conflict line -> category: the nearest enclosing nodeAffinity/podAffinity/podAntiAffinity key above that line.

Usage:
  python3 -I deployfix-d1-run.py --bin <deployfix-cli> --dataset <dir> --out <json>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

AFFINITY_KEYS = {
    "nodeAffinity": "P1",
    "podAffinity": "P2",
    "podAntiAffinity": "P3",
    "topologySpreadConstraints": "P4",
}


def classify_line(path: str, lineno: int) -> str | None:
    """Find the nearest affinity key enclosing lineno; DeployFix models only these three kinds."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    if not 1 <= lineno <= len(lines):
        return None
    target_indent = len(lines[lineno - 1]) - len(lines[lineno - 1].lstrip())
    best: tuple[int, str] | None = None
    for index in range(lineno - 1, -1, -1):
        text = lines[index]
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(text) - len(text.lstrip())
        if indent >= target_indent and index != lineno - 1:
            continue
        for key, category in AFFINITY_KEYS.items():
            if stripped.startswith(key + ":"):
                if best is None or indent > best[0]:
                    best = (indent, category)
    return best[1] if best else None


def parse_conflicts(out_dir: str) -> list[tuple[str, int]]:
    refs: list[tuple[str, int]] = []
    for name in os.listdir(out_dir):
        if not (name.startswith("conflicts-") and name.endswith(".yaml")):
            continue
        path = os.path.join(out_dir, name)
        entity = None
        for line in open(path, encoding="utf-8", errors="replace"):
            text = line.rstrip("\n")
            if text.startswith("- name:"):
                entity = text.split(":", 1)[1].strip()
            elif text.strip().startswith("- ") and ":" in text:
                ref = text.strip()[2:]
                file_part, _, line_part = ref.rpartition(":")
                try:
                    refs.append((file_part, int(line_part)))
                except ValueError:
                    continue
    return refs


def run_case(binary: str, case_dir: str, work: str, timeout: int) -> dict:
    apps = os.path.join(case_dir, "apps")
    src = os.path.join(work, "src")
    dummy = os.path.join(work, "dummy")
    out = os.path.join(work, "out")
    for path in (src, dummy, out):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(src)
    os.makedirs(dummy)
    os.makedirs(out)
    for name in sorted(os.listdir(apps)):
        if name.endswith(".yaml"):
            shutil.copy2(os.path.join(apps, name), os.path.join(src, name))

    env_file = os.path.join(case_dir, "deployfix", "node-env.txt")
    command = [binary, "k8s", "go", src, dummy, out, "--cycle-check"]
    if os.path.isfile(env_file):
        command += ["--env-file", env_file]

    started = time.time()
    try:
        proc = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout
        )
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    elapsed = time.time() - started

    if code == 1:
        status = "fault"
        labels = sorted({c for ref in parse_conflicts(out) for c in [classify_line(*ref)] if c})
    elif code == 0:
        status, labels = "healthy", []
    else:
        status, labels = "error", []

    return {
        "status": status,
        "category_ids": labels,
        "returncode": code,
        "seconds": round(elapsed, 4),
        "env_file": os.path.isfile(env_file),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    cases = sorted(
        name for name in os.listdir(args.dataset)
        if os.path.isdir(os.path.join(args.dataset, name))
    )
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c in wanted]

    results: dict[str, dict] = {}
    if os.path.isfile(args.out):
        results = json.load(open(args.out, encoding="utf-8"))

    work_root = tempfile.mkdtemp(prefix="deployfix-d1-")
    try:
        for index, case in enumerate(cases, 1):
            work = os.path.join(work_root, case)
            os.makedirs(work, exist_ok=True)
            record = run_case(args.bin, os.path.join(args.dataset, case), work, args.timeout)
            results[case] = record
            print(
                f"[{index}/{len(cases)}] {case}  {record['status']:7s} "
                f"{','.join(record['category_ids']) or '-':12s} "
                f"rc={record['returncode']} {record['seconds']:.2f}s",
                flush=True,
            )
            if index % 10 == 0 or index == len(cases):
                with open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(results, handle, indent=1, sort_keys=True)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=1, sort_keys=True)
    print(f"wrote {args.out} ({len(results)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
