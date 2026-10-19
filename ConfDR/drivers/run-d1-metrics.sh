#!/usr/bin/env bash
# Stage: metrics.  Free -- no model calls, no cluster.
#
#   . ./env.sh
#   bash run-d1-metrics.sh --label ds41flash
#
# The metric files are ALREADY written, each inside its own run directory, by the
# run that produced it (`confdr experiment` and `evaluate-metrics` both do this).
# This stage rescores them.  Nothing is aggregated into one file unless you ask for
# a combined report with COMBINED_OUT=<dir>.
set -euo pipefail

LABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Git Bash reports /d/... which native Windows Python resolves against the current
# drive; pwd -W gives D:/... which both bash and Python accept.  On Linux they agree.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && (pwd -W 2>/dev/null || pwd))"
cd "$ROOT"
# shellcheck disable=SC1091
. "$ROOT/env.sh"

LABEL="${LABEL:-${CONFDR_LABEL:-ds41flash}}"
BASE="$ROOT/tmp/baseline-adapt"
RUNS="$BASE/runs/$LABEL"

echo "=== 1. per-run metric files"
found=0
for run in "$ROOT/outputs/D1-method-$LABEL" "$RUNS/detection-eval" "$RUNS/repair-eval"; do
  if [ -f "$run/experiment.json" ]; then
    # Re-running is free and makes the files self-consistent after any adapter change.
    "$PY" -m confdr evaluate-metrics --run "$run" >/dev/null
    cases="$("$PY" -c "import json;print(json.load(open(r'$run/paper-metrics.json'))['overall']['case_count'])")"
    echo "  $run/paper-metrics.{json,txt}  ($cases cases)"
    found=$((found + 1))
  else
    echo "  [skip] no run at $run"
  fi
done
[ "$found" -gt 0 ] || { echo "nothing to score -- run the method or a baseline first" >&2; exit 2; }

echo
echo "=== 2. combined report (optional)"
# Only on request, and only ever as a separate file: the per-run files above stay the
# primary output, so a detection run is never read out of a file carrying repair cases.
if [ -n "${COMBINED_OUT:-}" ]; then
  runs=()
  for run in "$ROOT/outputs/D1-method-$LABEL" "$RUNS/detection-eval" "$RUNS/repair-eval"; do
    [ -f "$run/experiment.json" ] && runs+=("$run")
  done
  "$PY" -m confdr evaluate-metrics --run "${runs[@]}" --output "$COMBINED_OUT" >/dev/null
  echo "  $COMBINED_OUT/paper-metrics.{json,txt}"
else
  echo "  (skipped; set COMBINED_OUT=<dir> to also write one aggregated report)"
fi
