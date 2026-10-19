#!/usr/bin/env bash
# The whole D1 experiment: method + the two baselines, in the order they depend on
# each other.  Every stage is skippable and re-runnable on its own.
#
#   . ./env.sh                       # first, in every shell
#   bash run-all.sh --stage smoke
#   bash run-all.sh --stage all
#
# Stages
#   smoke    a handful of cases through both baselines -- proves the plumbing
#   method   ConfDR over the frozen 191-case D1 collection, with KWOK evaluation
#   adapt    adapt the frozen dataset into each baseline's input layout
#   detect   SlsDetector over 191 cases          (paid: one call per case)
#   repair   LLMSecConfig over the 159-case cohort (paid: the expensive stage)
#   metrics  per-run metric files (free)
#
# Paid stages print an estimate and ask before spending, unless --yes.
#
# Cost, at the DeepSeek relay rates used for the reference run
# ($0.1185184/M in, $0.4740736/M out):
#   method  ~$1.10     detect  ~$0.35     repair  ~$0.55
# A smoke run is a few cents.
#
# Time, from the same reference runs (191 cases for D1, 159 for the repair cohort).
# Every figure is measured, not assumed; the per-stage scripts carry the same numbers
# in their own pre-flight output:
#
#   adapt    ~2 min    free, local, no model calls.  Its Z3 gate step wrote all 127
#                      res.txt in 1.4 s; the rest is copying manifests
#   method   ~5.0 h    = 10813.7 s (3.0 h) of method + 7146.3 s (2.0 h) of KWOK, both
#                      from outputs/D1-method-ds-001.  The KWOK half is 40% of the
#                      stage and comes after every result.json is already on disk
#   detect   ~51 min   = 16.0 s/case mean over 191 (median 9.3 s, max 182.7 s)
#   repair   ~6.1 h    = 92.3 s/case wall over 130 measured cases (median 62.6 s, p90
#                      231.6 s, max 422.2 s), then a further 45.2 s/case of KWOK
#                      re-verification in step 3 -- more than the method stage's
#                      37.4 s/case there, because a case with a candidate gets three to
#                      five probes (original, final, and the 1/3/5-round prefixes), not one
#   metrics  ~1 min    free, local
#
# So a full D1 run is about 12 h wall, nearly all of it waiting on KWOK clusters.  The
# method stage is one sequential process by design; the two baselines are independent of
# it and of each other, so on a big server `--stage method` and `--stage detect` can run
# in two shells at once.  Do NOT run two --stage repair concurrently, whatever you do:
# each one's harness deletes a cluster literally named `kwok`.
#
# Progress: the two paid long stages print their own per-case lines, so no stage is ever
# silent for hours.  The method stage additionally gets an ETA line every 60 s from
# tmp/baseline-adapt/watch-method.py, since `confdr experiment` reports per-case progress
# but never says how much is left.
set -euo pipefail

STAGE=all
MODEL_LABEL=""
LIMIT=0
JOBS=3
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="$2"; shift 2 ;;
    --label) MODEL_LABEL="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --jobs)  JOBS="$2"; shift 2 ;;
    --yes)   ASSUME_YES=1; shift ;;
    # 2..50 is the whole header comment; 50 is the last line before `set -euo pipefail`.
    -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Git Bash reports /d/... which native Windows Python resolves against the current
# drive; pwd -W gives D:/... which both bash and Python accept.  On Linux they agree.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && (pwd -W 2>/dev/null || pwd))"
cd "$ROOT"

# `confdr` and the adapters both need the model; env.sh refuses to continue without it.
# shellcheck disable=SC1091
. "$ROOT/env.sh"

LABEL="${MODEL_LABEL:-${CONFDR_LABEL:-ds41flash}}"
D1_FROZEN="$ROOT/outputs/D1-collect-26-10-v3/dataset"
D1_PENDING="$ROOT/Dataset/D1-26-10"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"

# Rebuilt here, not read from "$@": the parse loop above consumes this script's argv, so by
# the time a stage function runs its "$@" is empty and `--limit` would be parsed, echoed in
# the header line, and then silently dropped.  The `${a[@]+...}` guard is for `set -u` on
# bash < 4.4 (CentOS 7 ships 4.2), where expanding an empty array aborts the script.
LIMIT_ARGS=()
[ "$LIMIT" != 0 ] && LIMIT_ARGS=(--limit "$LIMIT")

# Same reason, same fix, for --yes: the stage scripts each ask `continue? [y/N]` and would
# block forever on a non-interactive shell unless the flag is forwarded.  Only stage_smoke
# carried it, so a documented `--stage method --yes` still stopped at the prompt.
YES_ARGS=()
[ "$ASSUME_YES" = 1 ] && YES_ARGS=(--yes)

say() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
stamp() { date +%H:%M:%S; }

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  printf '%s  [y/N] ' "$1"
  read -r answer
  case "$answer" in [yY]*) return 0 ;; *) echo "skipped"; return 1 ;; esac
}

# --- stages -----------------------------------------------------------------

stage_smoke() {
  say "smoke: 3 cases through each baseline"
  bash "$ROOT/run-d1-method.sh" --limit 3 --label smoke-"$LABEL" --yes
  bash "$ROOT/run-d1-detect.sh" --limit 3 --label smoke-"$LABEL" --yes
  bash "$ROOT/run-d1-repair.sh" --limit 3 --label smoke-"$LABEL" --yes
  say "smoke done -- check the three logs in $LOGS before spending on the full run"
}

stage_method() {
  say "method: ConfDR over $D1_FROZEN (191 cases, KWOK evaluation included)"
  bash "$ROOT/run-d1-method.sh" --label "$LABEL" --jobs "$JOBS" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} ${YES_ARGS[@]+"${YES_ARGS[@]}"}
}

stage_adapt() {
  say "adapt: frozen dataset -> each baseline's input layout"
  # adapt_dataset.py builds dataset-sls (pending only) and dataset-repair;
  # adapt-healthy.py adds the 32 healthy controls so detection is scored on 191;
  # run-legacy-detect.py writes each case's res.txt, which is the repair cohort gate.
  "$PY" tmp/baseline-adapt/adapt_dataset.py
  "$PY" tmp/baseline-adapt/adapt-healthy.py
  "$PY" tmp/baseline-adapt/run-legacy-detect.py --jobs 4
}

stage_detect() {
  say "detect: SlsDetector over dataset-sls"
  bash "$ROOT/run-d1-detect.sh" --label "$LABEL" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} ${YES_ARGS[@]+"${YES_ARGS[@]}"}
}

stage_repair() {
  say "repair: LLMSecConfig over the staged cohort"
  bash "$ROOT/run-d1-repair.sh" --label "$LABEL" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} ${YES_ARGS[@]+"${YES_ARGS[@]}"}
}

stage_metrics() {
  say "metrics: per-run files"
  bash "$ROOT/run-d1-metrics.sh" --label "$LABEL"
}

run_stage() {
  case "$1" in
    smoke)  stage_smoke ;;
    method) stage_method ;;
    adapt)  stage_adapt ;;
    detect) stage_detect ;;
    repair) stage_repair ;;
    metrics) stage_metrics ;;
    all)
      stage_adapt
      stage_method
      stage_detect
      stage_repair
      stage_metrics
      ;;
    *) echo "unknown stage: $1" >&2; exit 2 ;;
  esac
}

say "$(stamp)  stage=$STAGE label=$LABEL limit=$LIMIT jobs=$JOBS"
run_stage "$STAGE"

say "$(stamp)  done"
cat <<EOF

Outputs
  method   outputs/D1-method-$LABEL/         experiment.json, dataset/<case>/{result,deployment}.json
           outputs/D1-method-$LABEL/paper-metrics.{json,txt}     its own metrics
  detect   tmp/baseline-adapt/runs/$LABEL/slsdetector/          pred/<case>.json, raw.csv
           tmp/baseline-adapt/runs/$LABEL/detection-eval/        synthesized run + its own metrics
  repair   tmp/baseline-adapt/runs/$LABEL/llmsec/               out/, result/, index.json
           tmp/baseline-adapt/runs/$LABEL/repair-eval/           synthesized run + its own metrics
  logs     logs/

Each run keeps its own paper-metrics.json/txt; nothing is aggregated into one file
unless you ask for it with 'confdr evaluate-metrics --run ... --output <dir>'.
EOF
