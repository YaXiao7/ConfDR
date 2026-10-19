#!/usr/bin/env bash
# ConfDR ablation w/o LG on {D1, D2} with GPT-5.6-Terra. ablation-woLG-all.sh also covers DeepSeek.
#
# w/o LG = the repair model receives none of: the formal rules (taxonomy/error_patterns/repair_examples),
#          the conflicting constraint instances (detection.formal / category_ids), or the configuration facts
#          (ClusterState / WaitingDeployPods in the case). Implemented by --no-lg in src/confdr/runtime/repair.py.
#
# Each group has two steps: method (confdr run --no-lg, KWOK skipped) -> KWOK evaluation (evaluate-deployment).
# **Everything runs serially**: the single KWOK cluster cannot be shared by concurrent evaluations.
#
# Finished groups are skipped, so the script can be rerun to resume.
#
# Usage:  bash ablation-woLG-GPT.sh             both datasets
#         ONLY=d1 bash ablation-woLG-GPT.sh     D1 only
#         ONLY=d2 bash ablation-woLG-GPT.sh     D2 only
set -uo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh

ONLY=${ONLY:-all}
RULES=rules/rules-v1.2/rules.yaml
D1=outputs/D1-collect-26-10-v3/dataset
D2=outputs/D2-collect-server-01/dataset
LOGS=logs; mkdir -p "$LOGS"
LOG=$LOGS/nolg-gpt.log

say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# ---------- generic per-group run, independent of the model ----------
run_group() {
  local tag="$1" dataset="$2" model="$3" structured="$4"
  local out="outputs/$tag" log="$LOGS/$tag.log" n

  say ""
  say "######## $tag ########"
  say "dataset=$dataset  model=$model  structured_outputs=$structured"

  n=$(ls -1 "$dataset" | wc -l)
  say "cases: $n"

  # ---- [1/2] method (KWOK skipped) ----
  # Completion must be read from the 'finished' field of experiment.json: the file is written after every case,
  # so its mere existence would mark a run that is still in progress as complete.
  if "$PY" -c "
import json,sys,pathlib
p=pathlib.Path('$out/experiment.json')
sys.exit(0 if p.is_file() and json.loads(p.read_text()).get('finished') else 1)
" 2>/dev/null; then
    say "  [1/2] $out method already finished, skipping"
  elif pgrep -f -- "--output $out" >/dev/null 2>&1; then
    say "  [1/2] $out method is still running, waiting for it to finish..."
    while pgrep -f -- "--output $out" >/dev/null 2>&1; do sleep 60; done
    say "  [1/2] $out method finished"
  else
    say "  [1/2] method confdr run --no-lg …"
    local t0=$(date +%s)
    CONFDR_LLM_MODEL="$model" CONFDR_LLM_STRUCTURED_OUTPUTS="$structured" \
      "$PY" -m confdr run \
        --dataset "$dataset" --rules "$RULES" --output "$out" \
        --max-rounds 5 --conflict-method label_skipping \
        --search-timeout-s 0 --solver-timeout-ms 5000 \
        --no-lg >"$log" 2>&1
    local rc=$?
    say "  method exit=$rc elapsed $(( $(date +%s) - t0 ))s"
    [ "$rc" -ne 0 ] && { say "  [!] method failed, skipping KWOK evaluation"; return 1; }
  fi

  # ---- [2/2] KWOK evaluation (serial) ----
  if "$PY" -c "
import json,sys,pathlib
p=pathlib.Path('$out/deployment-summary.json')
sys.exit(0 if p.is_file() and json.loads(p.read_text()).get('finished') else 1)
" 2>/dev/null; then
    say "  [2/2] $out KWOK evaluation already finished, skipping"
  else
    say "  [2/2] KWOK evaluation: evaluate-deployment (serial)..."
    local t1=$(date +%s)
    CONFDR_LLM_MODEL="$model" CONFDR_LLM_STRUCTURED_OUTPUTS="$structured" \
      "$PY" -m confdr evaluate-deployment --run "$out" >>"$log" 2>&1
    local rc2=$?
    say "  KWOK evaluation exit=$rc2 elapsed $(( $(date +%s) - t1 ))s"
    CONFDR_LLM_MODEL="$model" CONFDR_LLM_STRUCTURED_OUTPUTS="$structured" \
      "$PY" -m confdr evaluate-metrics --run "$out" >>"$log" 2>&1
  fi

  # ---- check ----
  local dep err
  dep=$(find "$out/dataset" -name deployment.json 2>/dev/null | wc -l)
  err=$(grep -rl '"status": *"error"' "$out"/dataset/*/deployment.json 2>/dev/null | wc -l)
  say "  check: deployment.json=$dep  error=$err"
  [ -f "$out/paper-metrics.txt" ] && say "  metrics: $out/paper-metrics.txt"
}

say "=== ConfDR ablation w/o LG (GPT, D1 and D2, serial) ===  ONLY=$ONLY"

case "$ONLY" in
  all|d1)
    run_group nolg-gpt56terra-d1   "$D1" gpt-5.6-terra          1
    ;;
esac
case "$ONLY" in
  all|d2)
    run_group nolg-gpt56terra-d2   "$D2" gpt-5.6-terra          1
    ;;
esac

say ""
say "=== GPT groups summary ==="
for t in nolg-gpt56terra-d1 nolg-gpt56terra-d2; do
  f="outputs/$t/paper-metrics.txt"
  [ -f "$f" ] && say "  $t  OK" || say "  $t  missing"
done
