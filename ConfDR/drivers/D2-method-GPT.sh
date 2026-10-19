#!/usr/bin/env bash
# ConfDR on D2 (DeployFix dataset, 40 cases) with GPT-5.6-Terra
#
# Same as D1-method-GPT.sh except for the dataset: the frozen D2 collection is
# outputs/D2-collect-server-01/dataset (40 cases: 24 Pending + 16 healthy).
# It has no dataset-manifest.json, so run_method skips the frozen-input check.
#
# Usage:  bash D2-method-GPT.sh            all 40 cases
#         LIMIT=1 bash D2-method-GPT.sh    smoke test
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh

export CONFDR_LLM_MODEL=gpt-5.6-terra
export CONFDR_LLM_STRUCTURED_OUTPUTS=1

LABEL=${LABEL:-gpt56terra}
LIMIT=${LIMIT:-}
DATASET=outputs/D2-collect-server-01/dataset

if [ -n "$LIMIT" ]; then
  LABEL="$LABEL-smoke"
  SMOKE="tmp/_smoke-d2-$LABEL/dataset"
  rm -rf "tmp/_smoke-d2-$LABEL"; mkdir -p "$SMOKE"
  n=0
  for c in "$DATASET"/*/; do
    cp -a "$c" "$SMOKE/$(basename "$c")"; n=$((n+1))
    [ "$n" -ge "$LIMIT" ] && break
  done
  DATASET="$SMOKE"
fi

OUT="outputs/D2-method-$LABEL"
LOG="logs/D2-method-$LABEL-run.log"
PROG="logs/D2-method-$LABEL-run-progress.log"
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
