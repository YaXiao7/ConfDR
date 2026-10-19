#!/usr/bin/env bash
# Stage: the ConfDR method itself, over the frozen D1 collection.
#
#   . ./env.sh
#   bash run-d1-method.sh --label ds41flash [--limit N] [--yes]
#
# `confdr experiment` (not `run`): it finishes every case's method output first and only
# then runs the independent KWOK deployment evaluation, in the same command.
#
# The flags are the ones the reference run used, so a row produced here is comparable
# with the reference results.  In particular `--search-timeout-s 0`
# is required: config/settings.yaml ships `search_timeout_s: 60`, and leaving that in
# place would give this row a different Z3 search budget from every other row.
#
# One process, sequential, on purpose.  run_method verifies the dataset against
# `outputs/D1-collect-26-10-v3/dataset-manifest.json`, and it looks for that file beside
# the dataset root -- so splitting the dataset into shards to run them in parallel would
# make the check silently find nothing.  It also bounds KWOK concurrency, which matters:
# kwok's per-command timeout is a fixed 30 s (config/settings.yaml:command_timeout_seconds)
# and too many clusters competing makes kwokctl/kubectl exceed it, turning cases into
# `error` for a reason that has nothing to do with the candidate.
set -euo pipefail

LABEL=""
LIMIT=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    # Accepted and ignored: run-all.sh passes it through from its own CLI.  The method
    # stage is sequential on purpose (see the note above about the manifest check and
    # KWOK's 30 s command timeout), so there is no job count to honour here.
    --jobs)  shift 2 ;;
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
DATASET="$ROOT/outputs/D1-collect-26-10-v3/dataset"
OUT="$ROOT/outputs/D1-method-$LABEL"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"

[ -d "$DATASET" ] || { echo "missing frozen dataset: $DATASET" >&2; exit 2; }
if [ -e "$OUT" ]; then
  echo "$OUT already exists -- remove it or pass a different --label" >&2
  exit 2
fi

echo "label   : $LABEL"
echo "out     : $OUT"
# Seconds -> "3m" / "4.1h".  Without this a 3-case smoke run prints "0.0h", which reads as
# "instant" when it is really three minutes.
dur() { awk -v s="$1" 'BEGIN{ if (s < 5400) printf "%dm", (s + 30) / 60; else printf "%.1fh", s / 3600 }'; }
if [ -n "$LIMIT" ]; then
  # A smoke run must not touch the frozen dataset, so it runs on a copied subset in a
  # scratch root of its own.  Its metric files are not comparable with a full run.
  #
  # `cp -a`, not `ln -s`: confdr's case discovery uses Path.rglob, which does NOT follow
  # directory symlinks, so a symlinked subset scans as zero cases and the run dies with
  # "Dataset contains no cases with case.json or apps/".  Windows' Git-Bash made `ln -s`
  # behave like a copy, which is why this only shows up on Linux.  Cases are ~90 KB each,
  # so copying is free.
  SMOKE="$ROOT/tmp/_smoke-$LABEL"
  rm -rf "$SMOKE"; mkdir -p "$SMOKE/dataset"
  n=0
  for case in "$DATASET"/*/; do
    cp -a "$case" "$SMOKE/dataset/$(basename "$case")"
    n=$((n + 1)); [ "$n" -ge "$LIMIT" ] && break
  done
  echo "limit   : $LIMIT cases via $SMOKE/dataset (copied subset; no dataset-manifest.json: the frozen check is off)"
  DATASET="$SMOKE/dataset"
fi
# Counted after the limit rewrite, so the estimate below describes what will actually run.
CASES="$(ls -1 "$DATASET" | wc -l)"
echo "dataset : $DATASET  ($CASES cases)"
echo "approx  : $CASES cases x (1 detection + up to 5 repair calls) -- roughly \$1.10 at DeepSeek rates for 191"
# Two phases, measured on outputs/D1-method-ds-001 (DeepSeek, 191 cases): the method
# phase took 10813.7 s and the KWOK deployment phase 7146.3 s.  Both are per-case rates,
# so they scale with the case count here; a 3-case smoke run is minutes, not hours.
echo "time    : ~$(dur "$(awk -v n="$CASES" 'BEGIN{print n*56.6}')") method + ~$(dur "$(awk -v n="$CASES" 'BEGIN{print n*37.4}')") KWOK  (sequential, one process)"
echo "          progress below: the run's own per-case lines, plus a 60 s ETA line"
if [ "$ASSUME_YES" != 1 ]; then
  printf 'continue? [y/N] '; read -r a
  case "$a" in [yY]*) ;; *) echo aborted; exit 1 ;; esac
fi

LOG="$LOGS/D1-method-$LABEL.log"
# Two progress sources, because they answer different questions.
#
# The run itself already prints one line per case -- `cli.py:336` hands `run_method` a
# `progress` hook, so the log carries `[12/191] <case>: ConfDR detection and static repair`
# plus one `Deployment evaluation <case>` line per case in the KWOK phase.  What it does
# NOT print is any sense of how long the rest will take, which is the whole problem with a
# 3-hour wait: you cannot tell a normal run from a hung one without doing arithmetic on
# line numbers.
#
# So watch-method.py counts finished cases on disk and prints an ETA beside the count.  It
# is a separate process because the tool has no hook that knows the total.  It shares no
# state with the run -- it only globs dataset/*/result.json and dataset/*/deployment.json
# under $OUT -- so if it dies the run is unaffected.
#
# It is started BEFORE the run because `experiment` refuses a pre-existing output directory
# (runtime/experiment.py:32), so the watcher waits for the directory to appear rather than
# the script creating it.
PROGRESS="$LOGS/D1-method-$LABEL-progress.log"
WATCHER_PID=""
TAIL_PID=""
cleanup() {
  # Silent on purpose: this also runs on the normal exit path, where "killed the
  # watcher" would be noise.  The -n guards are for the paths where nothing started.
  [ -n "$WATCHER_PID" ] && kill "$WATCHER_PID" 2>/dev/null
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT

# `tail -f` exits on a file it cannot open, so both must exist before it starts.
: >"$LOG"
: >"$PROGRESS"
"$PY" -I tmp/baseline-adapt/watch-method.py --run "$OUT" --total "$CASES" \
  >>"$PROGRESS" 2>&1 &
WATCHER_PID=$!
echo "progress: per-case lines from the run, plus an ETA line per 60 s"
echo "          logs: $LOG and $PROGRESS"

set +e
# Redirect rather than pipe: `cmd | tee log &` makes $! the PID of `tee`, so `wait`
# would return tee's status and every failure would look like a success.
"$PY" -m confdr experiment \
  --dataset "$DATASET" \
  --rules rules/rules-v1.2/rules.yaml \
  --output "$OUT" \
  --max-rounds 5 \
  --conflict-method label_skipping \
  --search-timeout-s 0 \
  --solver-timeout-ms 5000 \
  >"$LOG" 2>&1 &
RUN_PID=$!
# Follow both files to this terminal.  `tail -n +1 -f` with two arguments prints a
# `==> <file> <==` header whenever it switches, which is enough to tell the run's own
# per-case lines from the watcher's ETA line.  Both files exist before this point (created
# just above), because `tail -f` exits immediately on a file it cannot open at startup.
tail -n +1 -f "$LOG" "$PROGRESS" &
TAIL_PID=$!
wait "$RUN_PID"
status=$?
cleanup; WATCHER_PID=""; TAIL_PID=""
set -e

echo
echo "exit    : $status"
echo "log     : $LOG"
echo "progress: $PROGRESS"
if [ -f "$OUT/paper-metrics.txt" ]; then
  echo "metrics : $OUT/paper-metrics.txt   (this run only; nothing aggregated)"
fi
exit "$status"
