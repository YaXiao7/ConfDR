#!/usr/bin/env bash
# Repair baseline LLMSecConfig without its detection gate, with GPT-5.6-Terra
#
# Usage:
#   bash repair-v2.sh --col D2           # D2: 24 Pending cases
#   bash repair-v2.sh --col D1           # D1: 159 Pending cases
#   LIMIT=1 bash repair-v2.sh --col D2  # smoke test
#
# Semantics (detection gate removed):
#   - no res.txt gate: every truth-Pending case enters repair
#   - the error context is the ConfDR input evidence (FailedScheduling reason in collection.json)
#   - success is decided by KWOK observation (Pods Running), not by the baseline's internal Z3 recheck
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh

export CONFDR_LLM_MODEL=gpt-5.6-terra
export CONFDR_LLM_STRUCTURED_OUTPUTS=1

COL=""
LIMIT=${LIMIT:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --col) COL="$2"; shift 2 ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac
done
[ -n "$COL" ] || { echo "need --col D1|D2" >&2; exit 2; }

if [ "$COL" = "D2" ]; then
  REPAIR_TREE=tmp/baseline-adapt/dataset-repair-d2
  REF_RUN=outputs/D2-method-gpt56terra
  LABEL=${LABEL:-gpt56terra-d2}
else
  REPAIR_TREE=tmp/baseline-adapt/dataset-repair
  REF_RUN=outputs/D1-26-10-method-v1
  LABEL=${LABEL:-gpt56terra}
fi

LOGS=logs; mkdir -p "$LOGS"

# --- 1. stage the patched baseline ---
"$PY" tmp/baseline-adapt/setup-llmsec.py --dataset "$REPAIR_TREE" 2>&1 | tee "$LOGS/$COL-repair-$LABEL-setup.log"

# --- 2. run the baseline ---
run_args=(--model "$CONFDR_LLM_MODEL" --label "$LABEL")
[ -n "$LIMIT" ] && run_args+=(--limit "$LIMIT")
"$PY" tmp/baseline-adapt/run-llmsec.py "${run_args[@]}" 2>&1 | tee "$LOGS/$COL-repair-$LABEL-run.log"
status=${PIPESTATUS[0]}
[ "$status" = 0 ] || { echo "repair run failed ($status)" >&2; exit "$status"; }

# --- 3. synthesize the run directory + independent KWOK check ---
"$PY" tmp/baseline-adapt/emit-repair-run.py \
  --label "$LABEL" \
  --model "$CONFDR_LLM_MODEL" \
  --ref-run "$REF_RUN" 2>&1 | tee -a "$LOGS/$COL-repair-$LABEL-run.log"

RUN="tmp/baseline-adapt/runs/$LABEL/repair-eval"
"$PY" tmp/baseline-adapt/evaluate-repair.py --run "$RUN" 2>&1 | tee "$LOGS/$COL-repair-$LABEL-kwok.log"
"$PY" -m confdr evaluate-metrics --run "$RUN" 2>&1 | tee -a "$LOGS/$COL-repair-$LABEL-kwok.log"

echo
echo "run     : $RUN"
echo "metrics : $RUN/paper-metrics.txt"
