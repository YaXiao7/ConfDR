"""Total token spend of the baseline rerun, from the run artifacts.

Detection: SlsDetector's own raw.csv records per-case Tokens_in/Tokens_out.
Repair: each llmsec run.json records `llm_total_tokens` (CUMULATIVE across semantic
rounds) plus the FINAL round's prompt/completion split. The cumulative total is the
right numerator for cost; the final round's ratio is the best available estimate of
how that total splits, so the two are combined. Any case with llm_total_tokens == 0
is a call that raised before returning -- count it as zero spend and flag it, because
those are the relay-outage cases (see rerun-outage.py).

Prices are the list prices of the OpenAI-compatible relay used in the experiments; the relay
bills its own way, so treat the total as an estimate.

ConfDR's own runs are counted separately, from `outputs/<run>/dataset/<case>/result.json`:
each `model_calls[]` entry carries `usage.prompt_tokens` / `completion_tokens` and the
model name it recorded, so the spend is attributed per model rather than per run --
which matters because a run's `experiment.json` model field is a single name while a
run can in principle mix.  DeployFix contributes nothing here (no model calls at all)
and neither do the KWOK/Z3 stages; those are compute, not quota.

Usage: python tmp/baseline-adapt/cost.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
OUTPUTS = HERE.parents[1] / "outputs"  # the template tree's own run directory
PRICING = {
    "gpt-5.6-terra": (0.80, 4.80),
    "deepseek-v4.1-flash": (0.1185184, 0.4740736),
}
LABEL_MODEL = {"gpt56terra": "gpt-5.6-terra", "ds41flash": "deepseek-v4.1-flash"}
# Every ConfDR run in this tree that actually calls a model.  Listed explicitly rather
# than globbed: `outputs/` also holds collection runs and smoke runs, and a smoke run
# counted twice would read as full-corpus spend.
CONFDR_RUNS = (
    "D1-26-10-method-v1",
    "D1-26-10-method-v2",
    "D1-26-10-method-v3",
    "D2-method-label-skipping-001",
    "D1-method-ds-001",
    "D2-method-ds-001",
    # The 3-case fault re-run.  Listed because it does spend quota, but note it is NOT a
    # publishable run -- it is a 3-case probe used to attribute failures (see the changelog,
    # section 10), and its spend must never be added to the D1/D2 figures.
    "D1-faults-rerun-001",
)


def detection(label: str) -> tuple[int, int, int, float, float]:
    pin, pout = PRICING[LABEL_MODEL[label]]
    raw = RUNS / label / "slsdetector" / "raw.csv"
    tin = tout = rows = 0
    with raw.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 6 or row[0] == "Case":
                continue
            rows += 1
            tin += int(row[4] or 0)
            tout += int(row[5] or 0)
    return rows, tin, tout, tin / 1e6 * pin + tout / 1e6 * pout, pin


def repair(label: str) -> tuple[int, int, int, float, int, int]:
    pin, pout = PRICING[LABEL_MODEL[label]]
    cases = zero = total = 0
    cost = 0.0
    prompt = completion = 0
    for run_json in (RUNS / label / "llmsec" / "out").rglob("run.json"):
        d = json.loads(run_json.read_text(encoding="utf-8"))
        cases += 1
        tot = d.get("llm_total_tokens") or 0
        if not tot:
            zero += 1
            continue
        total += tot
        fp = d.get("final_repair_prompt_tokens") or d.get("llm_prompt_tokens") or 0
        fc = d.get("final_repair_completion_tokens") or d.get("llm_completion_tokens") or 0
        share = fp / (fp + fc) if (fp + fc) else 1.0
        prompt += tot * share
        completion += tot * (1 - share)
        cost += tot * share / 1e6 * pin + tot * (1 - share) / 1e6 * pout
    return cases, zero, total, cost, int(prompt), int(completion)


def confdr(run: str) -> tuple[int, dict[str, list[int]]]:
    """Token totals of one ConfDR run, keyed by the model each call recorded.

    Returns (cases-with-a-result, {model: [prompt, completion, calls]}).  A run that is
    still in flight simply contributes the cases finished so far, which is what makes
    this usable as a running cost meter rather than only a post-mortem.
    """
    totals: dict[str, list[int]] = {}
    cases = 0
    for result in sorted((OUTPUTS / run / "dataset").glob("*/result.json")):
        cases += 1
        doc = json.loads(result.read_text(encoding="utf-8"))
        for call in doc.get("model_calls") or []:
            usage = call.get("usage") or {}
            slot = totals.setdefault(call.get("model") or "unknown", [0, 0, 0])
            slot[0] += int(usage.get("prompt_tokens") or 0)
            slot[1] += int(usage.get("completion_tokens") or 0)
            slot[2] += 1
    return cases, totals


def main() -> None:
    grand = 0.0
    for label in sorted(LABEL_MODEL):
        rows, tin, tout, cost, _ = detection(label)
        print(f"[detection] {label:11s} {rows:4d} cases  {tin:8d} in / {tout:7d} out  ${cost:.4f}")
        grand += cost
    print()
    for label in sorted(LABEL_MODEL):
        cases, zero, total, cost, prompt, completion = repair(label)
        note = f"  ({zero} zero-token case(s) excluded)" if zero else ""
        print(
            f"[repair]    {label:11s} {cases:4d} cases  {total:9d} total tokens  "
            f"~{prompt} in / ~{completion} out  ${cost:.4f}{note}"
        )
        grand += cost

    print("\n[ConfDR] own runs (counted from each case's own model_calls usage):")
    confdr_by_model: dict[str, float] = {}
    for run in CONFDR_RUNS:
        if not (OUTPUTS / run / "dataset").is_dir():
            print(f"  {run:32s} (not present)")
            continue
        cases, totals = confdr(run)
        for model, (tin, tout, calls) in sorted(totals.items()):
            if model in PRICING:
                pin, pout = PRICING[model]
                cost = tin / 1e6 * pin + tout / 1e6 * pout
            else:
                cost = None
            grand += cost or 0.0
            confdr_by_model[model] = confdr_by_model.get(model, 0.0) + (cost or 0.0)
            shown = "n/a (no list price)" if cost is None else f"${cost:.4f}"
            print(
                f"  {run:32s} {cases:4d} cases  {model:20s} {calls:4d} calls  "
                f"{tin:9d} in / {tout:8d} out  {shown}"
            )
    print("\nsubtotals by model:")
    for model, cost in sorted(confdr_by_model.items()):
        print(f"  ConfDR runs, {model:20s} ${cost:.4f}")
    print(f"  baselines (SlsDetector + LLMSecConfig, both models)  ${grand - sum(confdr_by_model.values()):.4f}")
    print(f"\ntotal (list price, relay billing may differ): ${grand:.4f}")
    print("DeployFix and the KWOK/Z3 stages spend no quota; their cost is wall clock only.")


if __name__ == "__main__":
    main()
