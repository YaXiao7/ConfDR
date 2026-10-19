"""Run the patched LLMSecConfig baseline over the staged D1-26-10 repair cohort.

One invocation = one model. Everything the run produces lands under
runs/<label>/llmsec/ (out/<group>/<case>/run.json + fixed.yaml, index.json), so the
two models never overwrite each other and a crashed run can be resumed.

Cost control: this is the expensive half of the comparison. The runner makes up to
max_semantic_rounds x max_syntax_retries LLM calls per case, and each one follows a
~30-60 s kwok cluster create/verify cycle. --limit N caps the case count so the
plumbing can be proved on a couple of cases before committing to the full cohort.

Resume: the runner has no notion of "already done" and reprocesses every case under
data_dir on each start. So the label's own copy of the data dir is pruned of cases
that already have an index.json entry, and the run restarted -- that is the resume.

Usage:
  python tmp/baseline-adapt/run-llmsec.py --model gpt-5.6-terra --label gpt56terra --limit 2
  python tmp/baseline-adapt/run-llmsec.py --model deepseek-v4.1-flash --label ds41flash
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SMOKE = HERE / "llmsec"
PKG_SRC = SMOKE / "data"
RUNS = HERE / "runs"

sys.path.insert(0, str(HERE))
from hostenv import KEYS, apply_proxy  # noqa: E402


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip("'\"")
        if v:
            os.environ.setdefault(k.strip(), v)


def watch_progress(proc: subprocess.Popen, out_root: Path, cases: list[str], t0: float,
                   every: float = 30.0) -> None:
    """Print a progress line every `every` seconds while the runner works.

    The runner is a subprocess whose stdout is chatty but says nothing about how far
    along it is, so progress is counted from the filesystem instead: one run.json per
    finished case.  Counting index.json is not equivalent -- the baseline rewrites that
    whole file after every call, so it is not a reliable "how many are done" signal.

    Only run.json files for cases in THIS run are counted.  A resumed run prunes the
    finished cases out of its data dir but leaves their run.json behind, so a bare count
    would start at the previous run's total and read as more than 100%.

    The per-case cost is measured, not assumed: the reference run averaged 92 s/case but
    with a p90 of 232 s, so a constant would be misleading on a different model.
    """
    total = len(cases)
    wanted = set(cases)
    while proc.poll() is None:
        time.sleep(every)
        if proc.poll() is not None:
            break
        done = sum(1 for p in out_root.glob("out/*/*/run.json") if p.parent.name in wanted)
        elapsed = time.time() - t0
        if done <= 0:
            print(f"  [{elapsed / 60:5.1f}m] no case finished yet "
                  f"(the first includes staging + a cluster create)", flush=True)
            continue
        per = elapsed / done
        left = per * max(total - done, 0)
        eta = (f"{left:.0f}s" if left < 90 else
               f"{left / 60:.0f}m" if left < 5400 else f"{left / 3600:.1f}h")
        pct = 100.0 * done / total if total else 0.0
        print(f"  [{elapsed / 60:5.1f}m] {done}/{total} cases ({pct:4.1f}%)  "
              f"{per:5.1f}s/case  ETA {eta}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.6-terra")
    ap.add_argument("--label", required=True)
    ap.add_argument("--limit", type=int, default=0, help="stop after N cases (0 = all)")
    args = ap.parse_args()

    load_env(KEYS)
    key = os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("OPENAI_API_BASE")
    if not (key and base):
        sys.exit("OPENAI_API_KEY / OPENAI_API_BASE missing from tmp/baseline-keys.env")
    if not (SMOKE / "constraint_fix_pipeline" / "runner.py").is_file():
        sys.exit(f"missing patched package; run setup-llmsec.py first ({SMOKE})")

    out_root = RUNS / args.label / "llmsec"
    out = out_root / "out"
    data = out_root / "data"
    out.mkdir(parents=True, exist_ok=True)

    # --- stage this label's private copy of the cohort ---
    if not data.is_dir():
        shutil.copytree(PKG_SRC, data)
    index_path = out / "index.json"
    if index_path.is_file():
        # run.json records the case under "case"; older entries used "case_name".
        done = {
            e.get("case") or e.get("case_name")
            for e in json.loads(index_path.read_text(encoding="utf-8"))
        }
        done.discard(None)
        pruned = 0
        for gdir in data.iterdir():
            for cdir in list(gdir.iterdir()):
                if cdir.name in done:
                    shutil.rmtree(cdir)
                    pruned += 1
        if pruned:
            print(f"[resume] index.json lists {len(done)} done; pruned {pruned} case dirs")

    cases = sorted(c.name for g in sorted(data.iterdir()) for c in sorted(g.iterdir()))
    if args.limit:
        cases = cases[: args.limit]
        # restrict the data dir to the limited set so the runner does not run all
        keep = set(cases)
        for gdir in data.iterdir():
            for cdir in list(gdir.iterdir()):
                if cdir.name not in keep:
                    shutil.rmtree(cdir)
    print(f"model   : {args.model}")
    print(f"base    : {base}")
    print(f"cases   : {len(cases)}")
    print(f"out     : {out_root.relative_to(ROOT)}")
    if not cases:
        print("nothing to do")
        return

    env = os.environ.copy()
    # The baseline reaches the relay, which may only resolve through a proxy;
    # kubectl/kwok talk to 127.0.0.1 and must not be proxied or cluster setup fails
    # before any LLM call.
    env["OPENAI_API_KEY"] = key
    apply_proxy(env)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd = [
        sys.executable, "-m", "constraint_fix_pipeline.runner",
        "--data_dir", str(data),
        "--result_dir", str(out_root / "result"),
        "--output_dir", str(out),
        "--model_endpoint", args.model,
        "--api_base", base,
    ]
    print("command :", " ".join(cmd))
    print(f"api key : <from env, {len(key)} chars>")
    print()
    t0 = time.time()
    # Not subprocess.run: the progress watcher needs to poll the child while it works.
    proc = subprocess.Popen(cmd, cwd=str(SMOKE), env=env, text=True)
    try:
        watch_progress(proc, out_root, cases, t0)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    proc.wait()
    dt = time.time() - t0
    print(f"\n=== exit {proc.returncode} after {dt/60:.1f} min ===")

    # Summarize from the run's own index; the runner's stdout is chatty and the
    # index is what the evaluator consumes.
    if index_path.is_file():
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        from collections import Counter
        tally = Counter(e.get("status") for e in idx)
        tin = sum(e.get("llm_prompt_tokens") or 0 for e in idx)
        tout = sum(e.get("llm_completion_tokens") or 0 for e in idx)
        print(f"cases in index : {len(idx)}")
        for k, v in tally.most_common():
            print(f"  {k:12s} {v}")
        print(f"tokens         : {tin} in / {tout} out")
        print(f"index          : {index_path.relative_to(ROOT)}")
    else:
        print("[WARN] no index.json produced")


if __name__ == "__main__":
    main()
