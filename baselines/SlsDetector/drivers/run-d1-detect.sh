#!/usr/bin/env bash
# Stage: the SlsDetector detection baseline, over the 191-case D1 detection set.
#
#   . ./env.sh
#   bash run-d1-detect.sh --label ds41flash [--limit N] [--yes]
#
# Runs the ORIGINAL SlsDetector.py (its real prompt and parser) from the legacy checkout,
# one model call per case, then synthesizes a ConfDR-shaped run directory from the
# predictions so `confdr evaluate-metrics` scores the baseline with the same metric code,
# taxonomy and case set as the method.
#
# Prerequisite: `bash run-all.sh --stage adapt` (builds dataset-sls/ with all 191 cases).
#
# Cost: one call per case, roughly $0.35 for 191 cases at DeepSeek rates.
set -euo pipefail

LABEL=""
LIMIT=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --yes)   ASSUME_YES=1; shift ;;
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
SLS="$ROOT/tmp/baseline-adapt/dataset-sls"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"

[ -d "$SLS" ] || { echo "missing $SLS -- run 'bash run-all.sh --stage adapt' first" >&2; exit 2; }
CASES="$(ls -1 "$SLS" | wc -l)"
# Seconds -> "3m" / "51m".  The formatter the other two stage scripts use, capped at
# minutes because detection is the one stage that never reaches an hour on 191 cases.
dur() { awk -v s="$1" 'BEGIN{ if (s < 5400) printf "%dm", (s + 30) / 60; else printf "%.1fh", s / 3600 }'; }
echo "label   : $LABEL"
echo "dataset : $SLS  ($CASES cases)"
echo "model   : $CONFDR_LLM_MODEL"
echo "approx  : \$0.35 at DeepSeek rates for 191 cases (one call per case, no retries)"
# 16.0 s/case mean over the 191-case reference run.  The distribution is heavy-tailed --
# median 9.3 s, max 182.7 s (the multi-reason storage cases) -- so the running mean the
# adapter prints per case is the honest estimate, and it tightens as the run proceeds.
echo "time    : ~$(dur "$(awk -v n="$CASES" 'BEGIN{print n*16.0}')") at ~16 s/case"
[ -n "$LIMIT" ] && echo "limit   : $LIMIT"
echo "          per-case line below carries its own ETA"
if [ "$ASSUME_YES" != 1 ]; then
  printf 'continue? [y/N] '; read -r a
  case "$a" in [yY]*) ;; *) echo aborted; exit 1 ;; esac
fi

extra=()
[ -n "$LIMIT" ] && extra=(--limit "$LIMIT")

# One process per call would hammer the relay; the adapter is sequential by design.
"$PY" tmp/baseline-adapt/run-slsdetector.py \
  --model "$CONFDR_LLM_MODEL" \
  --label "$LABEL" \
  --keys "$KEYS" \
  --dataset "$SLS" \
  "${extra[@]}" 2>&1 | tee "$LOGS/D1-detect-$LABEL.log"
status=${PIPESTATUS[0]}
[ "$status" = 0 ] || { echo "detector failed ($status)" >&2; exit "$status"; }

# Synthesize the run directory, then let the method's own evaluator score it.  Both
# write a paper-metrics.{json,txt} into the run directory -- and only into it.
"$PY" tmp/baseline-adapt/emit-run.py \
  --pred "tmp/baseline-adapt/runs/$LABEL/slsdetector/pred" \
  --truth "$SLS" \
  --label "$LABEL" \
  --kind detection \
  --model "$CONFDR_LLM_MODEL" 2>&1 | tee -a "$LOGS/D1-detect-$LABEL.log"

RUN="$ROOT/tmp/baseline-adapt/runs/$LABEL/detection-eval"
"$PY" -m confdr evaluate-metrics --run "$RUN" 2>&1 | tee -a "$LOGS/D1-detect-$LABEL.log"

echo
echo "run     : $RUN"
echo "metrics : $RUN/paper-metrics.txt   (detection only; nothing aggregated)"
echo "raw     : tmp/baseline-adapt/runs/$LABEL/slsdetector/{pred,raw.csv}"
