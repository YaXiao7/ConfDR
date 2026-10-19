#!/usr/bin/env bash
# Detection baseline SlsDetector on D2 (40 cases) with GPT-5.6-Terra
#
# Same logic as run-d1-detect.sh with another dataset and label: the label gpt56terra-d2
# writes to runs/gpt56terra-d2/ so the D1 raw.csv is not overwritten.
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
# shellcheck disable=SC1091
. ./env.sh

export CONFDR_LLM_MODEL=gpt-5.6-terra

LABEL=gpt56terra-d2
SLS=tmp/baseline-adapt/dataset-sls-d2
LOGS=logs
mkdir -p "$LOGS"

[ -d "$SLS" ] || { echo "missing $SLS -- run adapt-d2.py first" >&2; exit 2; }
N=$(ls -1 "$SLS" | wc -l)
echo "label  : $LABEL"
echo "dataset: $SLS  ($N cases)"
echo "model  : $CONFDR_LLM_MODEL"

"$PY" tmp/baseline-adapt/run-slsdetector.py \
  --model "$CONFDR_LLM_MODEL" \
  --label "$LABEL" \
  --keys tmp/baseline-keys.env \
  --dataset "$SLS" 2>&1 | tee "$LOGS/D2-detect-$LABEL.log"
status=${PIPESTATUS[0]}
[ "$status" = 0 ] || { echo "detector failed ($status)" >&2; exit "$status"; }

# Synthesize a run directory; --ref-run points to the ConfDR D2 run so both use the same rule bundle.
"$PY" tmp/baseline-adapt/emit-run.py \
  --pred "tmp/baseline-adapt/runs/$LABEL/slsdetector/pred" \
  --truth "$SLS" \
  --label "$LABEL" \
  --kind detection \
  --ref-run outputs/D2-method-gpt56terra \
  --model "$CONFDR_LLM_MODEL" 2>&1 | tee -a "$LOGS/D2-detect-$LABEL.log"

RUN="tmp/baseline-adapt/runs/$LABEL/detection-eval"
"$PY" -m confdr evaluate-metrics --run "$RUN" 2>&1 | tee -a "$LOGS/D2-detect-$LABEL.log"
echo
echo "run     : $RUN"
echo "metrics : $RUN/paper-metrics.txt"
