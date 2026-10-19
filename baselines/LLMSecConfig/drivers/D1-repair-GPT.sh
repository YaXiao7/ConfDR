#!/usr/bin/env bash
# Repair baseline LLMSecConfig on D1 with GPT-5.6-Terra
#
# The only difference from run-d1-repair.sh: **step 3, the independent KWOK check (evaluate-repair.py), is skipped**.
# emit-repair-run.py (needed for a scorable run directory) and evaluate-metrics (free) are kept.
#
# Never run two repairs concurrently: the baseline harness runs `kwokctl delete cluster` on a
# cluster named `kwok`, so the second run deletes the first one's cluster. Step 2 also creates a
# kwok cluster, so the run needs the machine to itself even without step 3.
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh
export CONFDR_LLM_MODEL=gpt-5.6-terra

LABEL=gpt56terra
LOGS=logs
mkdir -p "$LOGS"

echo "label  : $LABEL"
echo "model  : $CONFDR_LLM_MODEL"
echo "mode   : repair steps 1-2 + emit, KWOK check skipped"

echo "=== 1. stage the baseline ==="
"$PY" tmp/baseline-adapt/setup-llmsec.py 2>&1 | tee "$LOGS/D1-repair-$LABEL-setup.log"

echo "=== 2. run LLMSecConfig ==="
"$PY" tmp/baseline-adapt/run-llmsec.py --model "$CONFDR_LLM_MODEL" --label "$LABEL" 2>&1 | tee "$LOGS/D1-repair-$LABEL-run.log"
status=${PIPESTATUS[0]}
[ "$status" = 0 ] || { echo "repair run failed ($status)" >&2; exit "$status"; }

echo "=== 3a. synthesize the repair-eval run ==="
"$PY" tmp/baseline-adapt/emit-repair-run.py \
  --label "$LABEL" \
  --model "$CONFDR_LLM_MODEL" \
  --cohort-from "$PWD/Dataset/D1-26-10" 2>&1 | tee -a "$LOGS/D1-repair-$LABEL-run.log"

echo "=== 3b. KWOK check (evaluate-repair.py) skipped ==="

RUN="$PWD/tmp/baseline-adapt/runs/$LABEL/repair-eval"
echo "=== 3c. scoring (free) ==="
"$PY" -m confdr evaluate-metrics --run "$RUN" 2>&1 | tee "$LOGS/D1-repair-$LABEL-metrics.log"

echo
echo "run     : $RUN"
echo "metrics : $RUN/paper-metrics.txt"
echo "(no KWOK check: Resolved/Introduced and deployment metrics are missing)"
