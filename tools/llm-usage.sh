#!/usr/bin/env bash
# Collect the time and token usage of every run -> <STAGE>/LLM-USAGE.md + LLM-USAGE.json
# Read-only: no run directory is modified and KWOK is not rerun.
#
#   STAGE=reports bash llm-usage.sh
#
# Three sources:
#   1. method (ConfDR)               efficiency section of outputs/<run>/paper-metrics.json
#   2. detection baseline (SlsDetector) Tokens_in/out + Latency in runs/<label>/slsdetector/raw.csv
#   3. repair baseline (LLMSecConfig)   llm_* fields in runs/<label>/llmsec/out/index.json
set -euo pipefail
cd "${CONFDR_ROOT:-$(pwd)}"
export CONFDR_KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# shellcheck disable=SC1091
. ./env.sh >/dev/null 2>&1

"$PY" - <<'PY'
import csv, json, os, pathlib

S = pathlib.Path(os.environ.get("STAGE", "reports"))
S.mkdir(parents=True, exist_ok=True)
R = pathlib.Path(".")

# ---------- method: efficiency section of paper-metrics.json ----------
def method_eff(run):
    p = R / run / "paper-metrics.json"
    if not p.is_file():
        return None
    ds = json.loads(p.read_text())["datasets"]
    key = [k for k in ds if run.split("/")[-1] in k]
    e = ds[key[0] if key else list(ds)[0]]
    eff = e.get("efficiency") or {}
    g = lambda f: (eff.get(f) or {}).get("observed_total")
    tok = g("repair_total_tokens")
    rec = (eff.get("repair_total_tokens") or {}).get("recorded_cases")
    den = (eff.get("repair_total_tokens") or {}).get("denominator")
    return {
        "seconds_total": g("repair_seconds"), "method_seconds": g("method_seconds"),
        "deployment_seconds": g("deployment_seconds"),
        "prompt_tokens": g("repair_prompt_tokens"), "completion_tokens": g("repair_completion_tokens"),
        "total_tokens": tok, "token_recorded": f"{rec}/{den}" if rec is not None else "—",
    }

# ---------- detection baseline: raw.csv ----------
def detect_usage(csv_path):
    p = R / csv_path
    if not p.is_file():
        return None
    rows = list(csv.DictReader(p.open()))
    ti = sum(int(r.get("Tokens_in") or 0) for r in rows)
    to = sum(int(r.get("Tokens_out") or 0) for r in rows)
    lat = sum(float(r.get("Latency(ms)") or 0) for r in rows) / 1000
    return {"n": len(rows), "prompt_tokens": ti, "completion_tokens": to,
            "total_tokens": ti + to, "seconds_total": lat, "model": rows[0].get("Model") if rows else "—"}

# ---------- repair baseline: llmsec index.json ----------
def repair_usage(label):
    p = R / f"tmp/baseline-adapt/runs/{label}/llmsec/out/index.json"
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    if not d:
        return None
    s = lambda k: sum((r.get(k) or 0) for r in d)
    rounds = sum(len(r.get("loop_history") or []) for r in d)
    return {"n": len(d), "success": sum(1 for r in d if r.get("status") == "success"),
            "seconds_total": s("llm_repair_time_sec"),
            "prompt_tokens": s("llm_prompt_tokens"), "completion_tokens": s("llm_completion_tokens"),
            "total_tokens": s("llm_total_tokens"), "final_seconds": s("final_repair_time_sec"),
            "rounds": rounds, "rounds_per_case": rounds / len(d),
            "_raw": d}

def fmt_tok(x):
    return "—" if x is None else f"{int(x):,}"

def fmt_min(x):
    return "—" if x is None else f"{x/60:.1f} min"

L = [
 "# Time and token usage per run (gpt-5.6-terra and deepseek-v4.1-flash)",
 "",
 "Sources: the method uses the efficiency section (`observed_total`) of `paper-metrics.json`;",
 "the detection baseline uses `runs/<label>/slsdetector/raw.csv`; the repair baseline uses `runs/<label>/llmsec/out/index.json`.",
 "Per-case records are in `LLM-USAGE.json` in the same folder.",
 "",
 "Tokens are prompt + completion. Time covers only the stage itself (the method excludes KWOK deployment,",
 "the detection baseline sums request latencies, the repair baseline sums LLM call time).",
 "",
]
JSON_OUT = {"method": {}, "detection": {}, "repair": {}}

COLS = [
 ("D1 method", "gpt56terra-method", "method", "outputs/D1-method-gpt56terra"),
 ("D2 method", "ds41flash-d2-method", "method", "outputs/D2-method-ds41flash-d2"),
 ("D2 method", "gpt56terra-d2-method", "method", "outputs/D2-method-gpt56terra"),
]

# ===== method table =====
L += ["## 1. Method: ConfDR", "",
      "| Run | Model | N | method s | with deployment s | repair s | prompt | completion | total tokens | tokens recorded |",
      "|---|---|---|---|---|---|---|---|---|---|"]
for name, key, _, run in COLS:
    model = "deepseek-v4.1-flash" if "ds41" in key else "gpt-5.6-terra"
    v = method_eff(run)
    if not v:
        L.append(f"| {name} | {model} | — | *(not run)* | | | | | | |")
        continue
    JSON_OUT["method"][key] = v
    L.append(f"| {name} | {model} | — | {v['method_seconds']:,.1f} | {v['deployment_seconds']:,.1f} | "
             f"{v['seconds_total']:,.1f} | {fmt_tok(v['prompt_tokens'])} | {fmt_tok(v['completion_tokens'])} | "
             f"**{fmt_tok(v['total_tokens'])}** | {v['token_recorded']} |")

# ===== detection baseline table =====
L += ["", "## 2. Detection baseline: SlsDetector", "",
      "| Run | Model | N | total request latency | prompt | completion | total tokens |",
      "|---|---|---|---|---|---|---|"]
DET = [
 ("D1 detection baseline", "D1 detection baseline", "gpt56terra", "tmp/baseline-adapt/runs/gpt56terra/slsdetector/raw.csv"),
 ("D2 detection baseline", "D2 detection baseline, GPT", "gpt56terra-d2", "tmp/baseline-adapt/runs/gpt56terra-d2/slsdetector/raw.csv"),
 ("D2 detection baseline", "D2 detection baseline, DeepSeek", "ds41flash-d2", "tmp/baseline-adapt/runs/ds41flash-d2/slsdetector/raw.csv"),
]
for name, key, label, csv_path in DET:
    v = detect_usage(csv_path)
    if not v:
        L.append(f"| {name} | {label} | — | *(not run)* | | | |")
        continue
    JSON_OUT["detection"][key] = v
    L.append(f"| {name} | {v['model']} | {v['n']} | {v['seconds_total']:,.1f} s | "
             f"{fmt_tok(v['prompt_tokens'])} | {fmt_tok(v['completion_tokens'])} | **{fmt_tok(v['total_tokens'])}** |")

# ===== repair baseline table =====
L += ["", "## 3. Repair baseline: LLMSecConfig (detection gate removed)", "",
      "| Run | Model | N | success | LLM, all rounds | final round | prompt | completion | total tokens | rounds per case |",
      "|---|---|---|---|---|---|---|---|---|---|"]
REP = [
 ("D1 repair baseline", "gpt56terra", "gpt-5.6-terra"),
 ("D2 repair baseline", "gpt56terra-d2", "gpt-5.6-terra"),
 ("D2 repair baseline", "ds41flash-d2", "deepseek-v4.1-flash"),
]
for name, label, model in REP:
    v = repair_usage(label)
    if not v:
        L.append(f"| {name} | {model} | — | *(not run)* | | | | | | |")
        continue
    JSON_OUT["repair"][label] = {k: x for k, x in v.items() if k != "_raw"}
    JSON_OUT["repair"][label]["per_case"] = [
        {"case": r["case"], "status": r.get("status"), "seconds": r.get("llm_repair_time_sec"),
         "prompt_tokens": r.get("llm_prompt_tokens"), "completion_tokens": r.get("llm_completion_tokens"),
         "total_tokens": r.get("llm_total_tokens"), "rounds": len(r.get("loop_history") or [])}
        for r in v["_raw"]]
    L.append(f"| {name} | {model} | {v['n']} | {v['success']} | {fmt_min(v['seconds_total'])} | "
             f"{fmt_min(v['final_seconds'])} | {fmt_tok(v['prompt_tokens'])} | {fmt_tok(v['completion_tokens'])} | "
             f"**{fmt_tok(v['total_tokens'])}** | {v['rounds_per_case']:.2f} |")

L += ["", "**Note**: the Efficiency section of the repair baseline's `paper-metrics` shows N/A because",
      "`emit-repair-run.py` does not map the per-row `model_calls` into `result.json`; this file holds the usage.", ""]

(S / "LLM-USAGE.md").write_text("\n".join(L), encoding="utf-8")
(S / "LLM-USAGE.json").write_text(json.dumps(JSON_OUT, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"written: {S/'LLM-USAGE.md'}")
PY
