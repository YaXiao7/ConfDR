#!/usr/bin/env bash
# KWOK deployment evaluation for D1, run serially on a single cluster (concurrent runs pollute the results with status=error).
#
#   method 191 cases -> confdr evaluate-deployment --run outputs/D1-method-gpt56terra
#   repair 159 cases -> tmp/baseline-adapt/evaluate-repair.py --run .../runs/gpt56terra/repair-eval
#
# The two evaluations run one after the other, never concurrently.
# evaluate-deployment cannot skip finished cases, so each evaluation reruns in full.
set -u
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh >/dev/null 2>&1

LOG=logs/D1-verify-serial.log
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

E1=outputs/D1-method-gpt56terra
E5=tmp/baseline-adapt/runs/gpt56terra/repair-eval

say "=== D1 KWOK evaluation, serial (method -> repair) ==="
say "existing kwok clusters: $(ls -1 ~/.kwok/clusters/ 2>/dev/null | tr '\n' ' ')"

say "--- [1/2] method, 191 cases ---"
start=$(date +%s)
"$PY" -m confdr evaluate-deployment --run "$E1" >> "$LOG" 2>&1
s1=$?
say "method done, exit=$s1, elapsed $(( $(date +%s) - start ))s"
"$PY" -m confdr evaluate-metrics --run "$E1" >> "$LOG" 2>&1

say "--- [2/2] repair baseline, 159 cases ---"
start=$(date +%s)
"$PY" tmp/baseline-adapt/evaluate-repair.py --run "$E5" >> "$LOG" 2>&1
s2=$?
say "repair done, exit=$s2, elapsed $(( $(date +%s) - start ))s"
"$PY" -m confdr evaluate-metrics --run "$E5" >> "$LOG" 2>&1

say "=== check ==="
for r in "$E1" "$E5"; do
  n=$(find "$r/dataset" -name deployment.json 2>/dev/null | wc -l)
  e=$(grep -rl '"status": *"error"' "$r/dataset"/*/deployment.json 2>/dev/null | wc -l)
  say "  $r  deployment.json=$n  error=$e"
done
say "done. method exit=$s1  repair exit=$s2"
