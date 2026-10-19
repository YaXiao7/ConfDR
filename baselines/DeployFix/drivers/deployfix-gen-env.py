#!/usr/bin/env python3
"""Generate DeployFix's --env-file from a case's envs/*.yaml (plus the actual placement in pod_info.json).

Format of DeployFix's DefaultEnvParser (src/model/env.rs):
    <node_name> <label>;<label>;...
One environment per line; lines with the same label set are folded into one env that keeps the first name.
So the output is one line per node, with the k=v pairs of Node.metadata.labels,
plus the labels of the Pods actually running on that node (Scheduled events in pod_info).

Usage:
  python3 -I deployfix-gen-env.py --dataset <dir> [--write] [--check-d1 <d1dir>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml



def node_labels(path: str) -> list[tuple[str, list[str]]]:
    """An envs file may hold several documents; take the labels of each Node and attach Pods to nodes by nodeName."""
    out = []
    for doc in yaml.safe_load_all(open(path, encoding="utf-8")):
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "Pod":
            node = (doc.get("spec") or {}).get("nodeName")
            labels = (doc.get("metadata") or {}).get("labels") or {}
            if node and labels:
                _POD.setdefault(node, set()).update(f"{k}={v}" for k, v in labels.items() if v != "")
            continue
        if doc.get("kind") != "Node":
            continue
        name = (doc.get("metadata") or {}).get("name")
        if not name:
            continue
        labels = (doc.get("metadata") or {}).get("labels") or {}
        # Empty-valued labels (node-role.kubernetes.io/agent: '') are kept as key=, matching Kubernetes selector semantics
        pairs = sorted([f"{k}={v}" for k, v in labels.items() if v != ""]
                       + [f"{k}=" for k, v in labels.items() if v == ""])
        # Follow the convention of the existing D1 node-env.txt files: kubernetes.io/{arch,os} also had
        # beta.kubernetes.io/ aliases in older clusters; KWOK snapshots omit them, so add them here.
        for k in ("kubernetes.io/arch", "kubernetes.io/os"):
            if k in labels and labels[k] != "":
                alias = "beta." + k
                if alias not in labels:
                    pairs.append(f"{alias}={labels[k]}")
        merged = set(pairs) | _POD.get(name, set())
        pairs = sorted(merged)
        out.append((name, pairs))
    return out


def build_simple(case_dir: str) -> list[str]:
    """Use only Node.metadata.labels, without Pod labels (a DeployFix env is a set of node labels)."""
    envs = os.path.join(case_dir, "envs")
    out = []
    if not os.path.isdir(envs):
        return out
    for f in sorted(os.listdir(envs)):
        if not f.endswith(".yaml"):
            continue
        for name, labels in node_labels(os.path.join(envs, f)):
            out.append(f"{name} {';'.join(labels)};")
    return out


_POD: dict[str, set[str]] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check-d1", default="")
    args = ap.parse_args()

    cases = sorted(c for c in os.listdir(args.dataset)
                   if os.path.isdir(os.path.join(args.dataset, c)))
    wrote = 0
    for case in cases:
        cd = os.path.join(args.dataset, case)
        _POD.clear()
        lines = build_simple(cd)
        if not lines:
            print(f"[skip] {case}: no envs/")
            continue
        if args.write:
            d = os.path.join(cd, "deployfix")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "node-env.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            wrote += 1
    print(f"generated {wrote} node-env.txt files ({len(cases)} cases)")

    if args.check_d1:
        same = diff = 0
        for case in cases:
            _POD.clear()
            mine = build_simple(os.path.join(args.dataset, case))
            ref_path = os.path.join(args.check_d1, case, "deployfix", "node-env.txt")
            if not os.path.isfile(ref_path):
                continue
            ref = [l.strip() for l in open(ref_path, encoding="utf-8") if l.strip()]
            if sorted(mine) == sorted(ref):
                same += 1
            else:
                diff += 1
                if diff <= 2:
                    print(f"[diff] {case}\n  mine={mine[:2]}\n  ref ={ref[:2]}")
        print(f"compared with the existing D1 files: {same} identical, {diff} different")
    return 0


if __name__ == "__main__":
    sys.exit(main())
