"""Count deployment_status across a repair-eval dir and list the 'error' reasons.

Usage: python tmp/baseline-adapt/diag-status.py <repair-eval-dir>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

run = Path(sys.argv[1]).resolve()
counts = Counter()
errors = []
for dep_json in sorted((run / "dataset").rglob("deployment.json")):
    dep = json.loads(dep_json.read_text(encoding="utf-8"))
    status = (dep.get("paper_evaluation") or {}).get("deployment_status")
    counts[status] += 1
    if status == "error":
        errors.append((dep_json.parent.name, dep.get("reason")))

print(f"{run.name}: {sum(counts.values())} cases")
for status, count in counts.most_common():
    print(f"  {count:4d}  {status}")
if errors:
    print(f"\n{len(errors)} error(s):")
    for case, reason in errors:
        print(f"  {case}  reason={reason}")
