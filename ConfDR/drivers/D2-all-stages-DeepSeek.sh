#!/usr/bin/env bash
# All D2 stages with DeepSeek-V4.1-Flash (method -> detection baseline -> repair baseline -> KWOK checks)
#
# The only differences from the GPT runs are the model and the structured-output switch:
#   DeepSeek behind the relay does not accept response_format=json_schema, so CONFDR_LLM_STRUCTURED_OUTPUTS=0.
#
# Runs serially (KWOK evaluations must never run concurrently).
# Usage:  bash D2-all-stages-DeepSeek.sh
set -uo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh

export CONFDR_LLM_MODEL=deepseek-v4.1-flash
export CONFDR_LLM_STRUCTURED_OUTPUTS=0

LABEL=ds41flash-d2
MREF=outputs/D2-method-gpt56terra        # reference run that used the same rule bundle
DSET=outputs/D2-collect-server-01/dataset
LOGS=logs; mkdir -p "$LOGS"
LOG=$LOGS/D2-ds-all.log
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

say "=== D2 x DeepSeek-V4.1-Flash, all stages ==="
say "model=$CONFDR_LLM_MODEL structured_outputs=$CONFDR_LLM_STRUCTURED_OUTPUTS"

# ---------- 1. method ----------
OUT=outputs/D2-method-$LABEL
say "--- [1/4] method: confdr run (40 cases, KWOK skipped) ---"
if [ -e "$OUT" ]; then
  say "  $OUT already exists, skipping"
else
  N=$(ls -1 "$DSET" | wc -l)
  "$PY" -m confdr run \
    --dataset "$DSET" --rules rules/rules-v1.2/rules.yaml --output "$OUT" \
    --max-rounds 5 --conflict-method label_skipping \
    --search-timeout-s 0 --solver-timeout-ms 5000 \
    >> "$LOG" 2>&1
  say "  exit=$?  ($OUT)"
fi

# ---------- 2. KWOK evaluation of the method output (serial) ----------
say "--- [2/4] method KWOK evaluation (40 cases, serial) ---"
"$PY" -m confdr evaluate-deployment --run "$OUT" >> "$LOG" 2>&1
say "  exit=$?"
"$PY" -m confdr evaluate-metrics --run "$OUT" >> "$LOG" 2>&1

# ---------- 3. detection baseline SlsDetector ----------
say "--- [3/4] detection baseline SlsDetector (40 cases) ---"
"$PY" tmp/baseline-adapt/run-slsdetector.py \
  --model "$CONFDR_LLM_MODEL" --label "$LABEL" \
  --keys tmp/baseline-keys.env --dataset tmp/baseline-adapt/dataset-sls-d2 \
  >> "$LOG" 2>&1
say "  run exit=$?"
"$PY" tmp/baseline-adapt/emit-run.py \
  --pred "tmp/baseline-adapt/runs/$LABEL/slsdetector/pred" \
  --truth tmp/baseline-adapt/dataset-sls-d2 \
  --label "$LABEL" --kind detection --ref-run "$MREF" --model "$CONFDR_LLM_MODEL" \
  >> "$LOG" 2>&1
say "  emit exit=$?"
"$PY" -m confdr evaluate-metrics --run "tmp/baseline-adapt/runs/$LABEL/detection-eval" >> "$LOG" 2>&1

# ---------- 4. repair baseline (without its detection gate) + KWOK check ----------
say "--- [4/4] repair baseline (detection gate removed), 24 Pending cases ---"
"$PY" tmp/baseline-adapt/setup-llmsec.py --dataset tmp/baseline-adapt/dataset-repair-d2 >> "$LOG" 2>&1
say "  setup exit=$?"
"$PY" tmp/baseline-adapt/run-llmsec.py --model "$CONFDR_LLM_MODEL" --label "$LABEL" >> "$LOG" 2>&1
say "  run-llmsec exit=$?"
"$PY" tmp/baseline-adapt/emit-repair-run.py --label "$LABEL" --model "$CONFDR_LLM_MODEL" --ref-run "$MREF" >> "$LOG" 2>&1
say "  emit-repair exit=$?"
"$PY" tmp/baseline-adapt/evaluate-repair.py --run "tmp/baseline-adapt/runs/$LABEL/repair-eval" >> "$LOG" 2>&1
say "  evaluate-repair exit=$?"
"$PY" -m confdr evaluate-metrics --run "tmp/baseline-adapt/runs/$LABEL/repair-eval" >> "$LOG" 2>&1

say "=== done ==="
for r in "$OUT" "tmp/baseline-adapt/runs/$LABEL/detection-eval" "tmp/baseline-adapt/runs/$LABEL/repair-eval"; do
  [ -f "$r/paper-metrics.txt" ] && say "  $r OK" || say "  $r missing"
done
