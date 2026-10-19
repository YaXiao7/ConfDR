#!/usr/bin/env bash
# KWOK deployment evaluation of the ConfDR output on D2 (serial, single cluster, no resource contention).
#   confdr evaluate-deployment --run outputs/D2-method-gpt56terra
#   confdr evaluate-metrics    --run <same run>
set -u
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh >/dev/null 2>&1

LOG=/tmp/d2-verify.log
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
RUN=outputs/D2-method-gpt56terra

say "=== D2 method KWOK evaluation (40 cases, serial) ==="
"$PY" -m confdr evaluate-deployment --run "$RUN" >> "$LOG" 2>&1
say "evaluate-deployment exit=$?"

say "=== refresh metrics ==="
"$PY" -m confdr evaluate-metrics --run "$RUN" >> "$LOG" 2>&1
say "evaluate-metrics exit=$?"

n=$("$PY" -c "import json;print(json.load(open('$RUN/paper-metrics.json'))['overall']['case_count'])" 2>/dev/null)
d=$(find "$RUN/dataset" -name deployment.json 2>/dev/null | wc -l)
say "  $RUN  cases=$n  deployment.json=$d"
say "done."
