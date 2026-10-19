#!/usr/bin/env bash
# Stage: the LLMSecConfig repair baseline, over the 159-case D1 repair cohort.
#
#   . ./env.sh
#   bash run-d1-repair.sh --label ds41flash [--limit N] [--rounds 5] [--syntax 10] [--yes]
#
# Three steps, in order:
#   1. setup-llmsec.py   stage a patched copy of the baseline into tmp/baseline-adapt/llmsec/
#   2. run-llmsec.py     run it over the staged cohort, one model call per round
#   3. emit-repair-run.py + evaluate-repair.py
#                        write the baseline's output into a ConfDR-shaped run directory,
#                        then re-verify it in KWOK with confdr's own evaluator
#
# This is the expensive stage: the runner makes up to rounds x syntax LLM calls per case
# and each one follows a ~30-60 s cluster create/verify cycle.  Run it on its own.
#
# Never run two of these at once.  The baseline's own harness does a bare
# `kwokctl delete cluster` on a cluster named `kwok` (its runner.py), so a second
# concurrent run deletes the first one's cluster out from under it.  (confdr's own
# evaluate_deployment is safe to run concurrently -- it uses a unique cluster name.)
set -euo pipefail

LABEL=""
LIMIT=""
ROUNDS=""
SYNTAX=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --label)  LABEL="$2"; shift 2 ;;
    --limit)  LIMIT="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --syntax) SYNTAX="$2"; shift 2 ;;
    --yes)    ASSUME_YES=1; shift ;;
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
REPAIR="$ROOT/tmp/baseline-adapt/dataset-repair"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"

[ -d "$REPAIR" ] || { echo "missing $REPAIR -- run 'bash run-all.sh --stage adapt' first" >&2; exit 2; }
CASES="$(ls -1 "$REPAIR" | wc -l)"
# Seconds -> "3m" / "4.1h".  Without this a 3-case smoke run prints "0.0h", which reads as
# "instant" when it is really three minutes.
dur() { awk -v s="$1" 'BEGIN{ if (s < 5400) printf "%dm", (s + 30) / 60; else printf "%.1fh", s / 3600 }'; }
echo "label   : $LABEL"
echo "model   : $CONFDR_LLM_MODEL"
echo "cohort  : $REPAIR  ($CASES staged cases)"
echo "approx  : the expensive stage; ~\$0.55 at DeepSeek rates for the full cohort"
# 92.3 s/case mean wall over the reference cohort -- median 62.6 s, p90 231.6 s, max 422.2 s.
# Most of that is the baseline's own ~30-60 s cluster create/verify cycle per LLM call, not
# the call itself (llm_repair_time_sec averaged 30.7 s).  The running mean printed every 30 s
# below replaces this figure as soon as the first case lands.
echo "time    : ~$(dur "$(awk -v n="$CASES" 'BEGIN{print n*92.3}')") at ~92 s/case (wall, includes kwok cluster churn)"
# Step 3's own cost, measured from the per-case deployment_seconds in the reference run
# (runs/ds41flash/repair-eval, 130 evaluated cases: mean 45.2 s, median 26.7 s, max 221.1 s).
# It is larger than the method stage's per-case rate because a case with a candidate gets
# three to five probes (original, final, and the 1/3/5-round prefixes), not one.
echo "          step 3 re-verifies every case in KWOK afterwards; that is a further ~45 s/case, ~$(dur "$(awk -v n="$CASES" 'BEGIN{print n*45.2}')")"
[ -n "$LIMIT" ] && echo "limit   : $LIMIT"
if [ "$ASSUME_YES" != 1 ]; then
  printf 'continue? [y/N] '; read -r a
  case "$a" in [yY]*) ;; *) echo aborted; exit 1 ;; esac
fi

# --- 1. stage the patched baseline ------------------------------------------
# Every patch is applied to the COPY under tmp/baseline-adapt/llmsec/; the checkout in
# sldstst-ConfDR-18d2307/ is never modified.  The kwok flags come from CONFDR_KWOK_BIN,
# so this is where the binary runtime paths for this host get baked in.
stage_args=()
[ -n "$ROUNDS" ] && stage_args+=(--rounds "$ROUNDS")
[ -n "$SYNTAX" ] && stage_args+=(--syntax "$SYNTAX")
"$PY" tmp/baseline-adapt/setup-llmsec.py "${stage_args[@]}" 2>&1 | tee "$LOGS/D1-repair-$LABEL-setup.log"

# --- 2. run it ---------------------------------------------------------------
run_args=(--model "$CONFDR_LLM_MODEL" --label "$LABEL")
[ -n "$LIMIT" ] && run_args+=(--limit "$LIMIT")
"$PY" tmp/baseline-adapt/run-llmsec.py "${run_args[@]}" 2>&1 | tee "$LOGS/D1-repair-$LABEL-run.log"
status=${PIPESTATUS[0]}
[ "$status" = 0 ] || { echo "repair run failed ($status)" >&2; exit "$status"; }

# --- 3. synthesize the run, then re-verify it in KWOK ------------------------
# --cohort-from puts the 29 cases the baseline's own detector called SAT back into the
# denominator, where they score as failures.  Leaving them out would score the baseline
# only on the subset it selected for itself.
"$PY" tmp/baseline-adapt/emit-repair-run.py \
  --label "$LABEL" \
  --model "$CONFDR_LLM_MODEL" \
  --cohort-from "$ROOT/Dataset/D1-26-10" 2>&1 | tee -a "$LOGS/D1-repair-$LABEL-run.log"

RUN="$ROOT/tmp/baseline-adapt/runs/$LABEL/repair-eval"
"$PY" tmp/baseline-adapt/evaluate-repair.py --run "$RUN" 2>&1 | tee "$LOGS/D1-repair-$LABEL-kwok.log"
"$PY" -m confdr evaluate-metrics --run "$RUN" 2>&1 | tee -a "$LOGS/D1-repair-$LABEL-kwok.log"

echo
echo "run     : $RUN"
echo "metrics : $RUN/paper-metrics.txt   (repair only; nothing aggregated)"
echo "kwok    : $RUN/dataset/<case>/deployment.json  (the independent recheck)"
