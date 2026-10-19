#!/usr/bin/env bash
# ConfDR on D1 with GPT-5.6-Terra
#
# Differences from run-d1-method.sh:
#   1. Uses `confdr run` instead of `experiment`: it stops after the method output and
#      **skips the independent KWOK deployment evaluation**. Run that afterwards with
#      `confdr evaluate-deployment --run <dir>` (see D1-verify-KWOK-serial.sh).
#   2. GPT-5.6-Terra honours response_format=json_schema, so
#      CONFDR_LLM_STRUCTURED_OUTPUTS=1 keeps the designed path. The DeepSeek runs need =0
#      because the API relay silently ignores json_schema; this is a relay difference, not a method difference.
#
# Usage:  bash D1-method-GPT.sh            all 191 cases
#         LIMIT=3 bash D1-method-GPT.sh    smoke test on a few cases
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh

# Override the DeepSeek settings from the keys file (takes effect after env.sh)
export CONFDR_LLM_MODEL=gpt-5.6-terra
export CONFDR_LLM_STRUCTURED_OUTPUTS=1

LABEL=${LABEL:-gpt56terra}
LIMIT=${LIMIT:-}
DATASET=outputs/D1-collect-26-10-v3/dataset

if [ -n "$LIMIT" ]; then
  LABEL="$LABEL-smoke"
  SMOKE="tmp/_smoke-gpt56terra/dataset"
  rm -rf tmp/_smoke-gpt56terra; mkdir -p "$SMOKE"
  n=0
  for c in "$DATASET"/*/; do
    cp -a "$c" "$SMOKE/$(basename "$c")"; n=$((n+1))
    [ "$n" -ge "$LIMIT" ] && break
  done
  DATASET="$SMOKE"
fi

OUT="outputs/D1-method-$LABEL"
LOG="logs/D1-method-$LABEL-run.log"
PROG="logs/D1-method-$LABEL-run-progress.log"
mkdir -p logs

if [ -e "$OUT" ]; then echo "$OUT already exists, aborting (change LABEL or remove it)" >&2; exit 2; fi

N=$(ls -1 "$DATASET" | wc -l)
echo "label  : $LABEL"
echo "model  : $CONFDR_LLM_MODEL (structured_outputs=$CONFDR_LLM_STRUCTURED_OUTPUTS)"
echo "dataset: $DATASET  ($N cases)"
echo "out    : $OUT"
echo "mode   : confdr run -- method only, KWOK evaluation skipped"

: >"$LOG"; : >"$PROG"
"$PY" -I tmp/baseline-adapt/watch-method.py --run "$OUT" --total "$N" >>"$PROG" 2>&1 &
WATCHER=$!

set +e
"$PY" -m confdr run \
  --dataset "$DATASET" \
  --rules rules/rules-v1.2/rules.yaml \
  --output "$OUT" \
  --max-rounds 5 \
  --conflict-method label_skipping \
  --search-timeout-s 0 \
  --solver-timeout-ms 5000 \
  >"$LOG" 2>&1
status=$?
set -e

kill "$WATCHER" 2>/dev/null || true
echo
echo "exit   : $status"
echo "log    : $LOG"
[ -f "$OUT/paper-metrics.txt" ] && echo "metrics: $OUT/paper-metrics.txt"
exit "$status"
