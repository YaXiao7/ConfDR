"""Run the SlsDetector detection baseline over the new D1-26-10 dataset.

Reuses the baseline's REAL prompt and REAL <START>/<END> parser; only the OpenAI
client and the model id are swapped, exactly as tmp/smoke-slsdetector.py does.
Nothing under sldstst-ConfDR-18d2307/ is modified -- its hardcoded
base_url='XXX' / chose_model='gpt-4o' / output-gpt4o.csv are never touched.

Difference from tmp/run-slsdetector-batch.py (which targets the old 118 cases):
pred_labels is now FILLED. The old run left it null because no field-path ->
code table existed and the legacy mapping was non-deterministic. We now construct
one against the new taxonomy (labels.py); it is a new artifact and must be
declared as such wherever these numbers are reported.

Usage:
  python tmp/baseline-adapt/run-slsdetector.py --model gpt-5.6-terra --label gpt56terra
  python tmp/baseline-adapt/run-slsdetector.py --model deepseek-v4.1-flash --label ds41flash
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# The baseline module ships inside `sldstst-ConfDR-18d2307/`, which now sits beside this
# tree; --base overrides it for a checkout kept somewhere else.
BASE = ROOT / "sldstst-ConfDR-18d2307" / "baseline-SlsDetector"
DATASET = HERE / "dataset-sls"  # --dataset overrides
KEYS = ROOT / "tmp" / "baseline-keys.env"

sys.path.insert(0, str(HERE))
from hostenv import KEYS as ENV_KEYS, apply_proxy  # noqa: E402
from labels import classify_reasons  # noqa: E402

# USD per 1M tokens, list price of the OpenAI-compatible relay used in the experiments.
PRICING = {
    "gpt-5.6-terra": (0.80, 4.80),
    "deepseek-v4.1-flash": (0.1185184, 0.4740736),
}


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip("'\"")
        if v:
            os.environ[k.strip()] = v


def extract_json(text: str) -> dict:
    m = re.search(r"<START>(.*?)<END>", text, re.DOTALL)
    body = (m.group(1) if m else text).strip()
    body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.MULTILINE).strip()
    return json.loads(body)


def _eta(i: int, total: int, t0: float) -> str:
    """Remaining time from the mean of the cases finished so far.

    Deliberately the running mean, not a fixed per-case constant: latencies here
    range from 2 s to 183 s (the long ones are the multi-reason storage cases), so a
    constant would be wrong by an order of magnitude at both ends.  The mean is the
    honest summary of a heavy-tailed distribution, and it self-corrects as that tail
    comes in.
    """
    elapsed = time.time() - t0
    if i <= 0 or elapsed <= 0:
        return "ETA --"
    per = elapsed / i
    left = per * (total - i)
    if left < 90:
        shown = f"{left:.0f}s"
    elif left < 5400:
        shown = f"{left / 60:.0f}m"
    else:
        shown = f"{left / 3600:.1f}h"
    return f"ETA {shown:>5s} @{per:4.1f}s/case"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.6-terra")
    ap.add_argument("--label", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--dataset", type=Path, default=None,
        help="case tree with one <case>/deploy.yaml each; defaults to dataset-sls (D1). "
             "Pass tmp/baseline-adapt/dataset-sls-d2 for the D2 column.",
    )
    ap.add_argument(
        "--keys", type=Path, default=None,
        help="dotenv file holding OPENAI_API_KEY / OPENAI_API_BASE. The credentials live "
             "outside this tree; pass that file's absolute path. Nothing is written back.",
    )
    ap.add_argument(
        "--base", type=Path, default=None,
        help="directory holding the original SlsDetector.py (its real prompt and parser). "
             "Defaults to <template tree>/sldstst-ConfDR-18d2307/baseline-SlsDetector, which "
             "only exists once that folder is copied in; point it at the sibling tree otherwise.",
    )
    args = ap.parse_args()

    dataset = (args.dataset or DATASET).resolve()
    if not dataset.is_dir():
        sys.exit(f"dataset not found: {dataset}")

    label = args.label or args.model
    out = ROOT / "tmp" / f"baseline-adapt" / "runs" / label / "slsdetector"
    pred_dir = out / "pred"
    pred_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = out / "raw.csv"

    keys = (args.keys or ENV_KEYS).resolve()
    if not keys.is_file():
        sys.exit(f"keys file not found: {keys}  (pass --keys <path>)")
    load_env(keys)
    key = os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("OPENAI_API_BASE")
    if not (key and base):
        sys.exit("OPENAI_API_KEY / OPENAI_API_BASE missing from the keys file")

    # The relay may only resolve through a proxy (a local HTTP proxy, say).  CONFDR_HTTP_PROXY
    # carries it; unset means direct, which is what a server with plain internet access
    # wants.  NO_PROXY always excludes loopback so kwok/kubectl are never pushed at it.
    apply_proxy(os.environ)

    base_dir = (args.base or BASE).resolve()
    if not (base_dir / "SlsDetector.py").is_file():
        sys.exit(f"SlsDetector.py not found in {base_dir}  (pass --base <dir>)")

    sys.path.insert(0, str(base_dir))
    import SlsDetector  # noqa: E402
    from openai import OpenAI  # noqa: E402

    SlsDetector.client = OpenAI(base_url=base, api_key=key, max_retries=2)
    SlsDetector.chose_model = args.model

    usage = {"in": 0, "out": 0}
    _real_create = SlsDetector.client.chat.completions.create

    def _recording_create(*a, **kw):
        resp = _real_create(*a, **kw)
        u = getattr(resp, "usage", None)
        if u is not None:
            usage["in"] += getattr(u, "prompt_tokens", 0) or 0
            usage["out"] += getattr(u, "completion_tokens", 0) or 0
        return resp

    SlsDetector.client.chat.completions.create = _recording_create

    cases = sorted(p for p in dataset.iterdir() if p.is_dir() and (p / "deploy.yaml").is_file())
    if args.limit:
        cases = cases[: args.limit]
    done = {f.stem for f in pred_dir.glob("*.json")}
    todo = [c for c in cases if c.name not in done]

    print(f"model : {args.model}")
    print(f"base  : {base}")
    print(f"data  : {dataset.relative_to(ROOT)}")
    print(f"cases : {len(cases)}  todo: {len(todo)}  (done: {len(done)})")
    print(f"out   : {out.relative_to(ROOT)}")
    if not todo:
        print("nothing to do")
        return

    if not raw_csv.exists():
        raw_csv.write_text(
            "Case,Model,Final_responses,Latency(ms),Tokens_in,Tokens_out\n", encoding="utf-8"
        )

    pin, pout = PRICING.get(args.model, (0.0, 0.0))
    cost = 0.0
    ok = fail = 0
    t0 = time.time()
    print(f"start : {time.strftime('%H:%M:%S')}  {len(todo)} cases to run")
    print()

    for i, cdir in enumerate(todo, 1):
        data = (cdir / "deploy.yaml").read_text(encoding="utf-8", errors="ignore")
        tin, tout = usage["in"], usage["out"]
        try:
            result, latency_ms = SlsDetector.constraint_method(data)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(todo)}] FAIL {cdir.name}: {type(exc).__name__}: {str(exc)[:150]}")
            fail += 1
            continue

        raw = "".join(result)
        try:
            obj = extract_json(raw)
            is_pending = bool(obj.get("is_pending"))
            reasons = obj.get("reasons") if is_pending else []
            pred_labels = classify_reasons(reasons)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(todo)}] PARSE-FAIL {cdir.name}: {type(exc).__name__}: {raw[:160]!r}")
            fail += 1
            continue

        (pred_dir / f"{cdir.name}.json").write_text(
            json.dumps(
                {
                    "pred_is_pending": is_pending,
                    "pred_labels": pred_labels,
                    "reasons": reasons,
                    "solve_time_sec": round(latency_ms / 1000.0, 3),
                    "model": args.model,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with raw_csv.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(
                [cdir.name, args.model, json.dumps(raw), latency_ms,
                 usage["in"] - tin, usage["out"] - tout]
            )

        ok += 1
        cost = usage["in"] / 1e6 * pin + usage["out"] / 1e6 * pout
        print(
            f"[{i}/{len(todo)}] {cdir.name[:36]:36s} pending={str(is_pending):5s} "
            f"labels={','.join(pred_labels) or '-':8s} {latency_ms/1000:5.2f}s "
            f"${cost:.4f}  {_eta(i, len(todo), t0)}  [ok={ok} fail={fail}]"
        )

    dt = time.time() - t0
    print(f"\n=== done: ok={ok} fail={fail} in {dt/60:.1f} min ===")
    print(f"tokens: {usage['in']} in / {usage['out']} out")
    print(f"spend : ${cost:.4f} (list price; relay billing may differ)")
    print(f"preds : {pred_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
