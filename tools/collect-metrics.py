#!/usr/bin/env python3
"""Collect the detection and repair metrics of every method on D1/D2 into Markdown + CSV.

Sources (relative to CONFDR_ROOT, default: the current directory):
  SlsDetector  tmp/baseline-adapt/runs/<label>/{detection-eval,repair-eval}/paper-metrics.json
  ConfDR       outputs/<run>/paper-metrics.json
  DeployFix    $DEPLOYFIX_RESULTS/{deployfix-d1.json,deployfix-d2.json,repair-d1/,repair-d2/}
               (default: <CONFDR_ROOT>/deployfix-results; written by baselines/DeployFix/drivers)

The summary is written to $DEPLOYFIX_RESULTS/metrics-summary.{md,csv}.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.environ.get("CONFDR_ROOT", os.getcwd())
DFX = os.environ.get("DEPLOYFIX_RESULTS", os.path.join(ROOT, "deployfix-results"))


def load(p):
    d = json.load(open(p, encoding="utf-8"))
    ds = d["datasets"]
    return ds[sorted(ds)[0]]


def pct(x, nd=2):
    return "—" if x is None else f"{x * 100:.{nd}f}"


def num(x, nd=3):
    return "—" if x is None else f"{x:.{nd}f}"


def detection(path, view="combined_detection"):
    m = load(path)
    det = m["detection"][view]
    eff = (m.get("efficiency") or {}).get("detection_end_to_end_seconds") or {}
    return {
        "tp": det["tp"], "fp": det["fp"], "fn": det["fn"], "tn": det["tn"],
        "p": det["precision"], "r": det["recall"], "f1": det["f1"],
        "jac": det["avg_jaccard"], "exact": det.get("exact_match"),
        "seconds": eff.get("average"), "cases": m.get("case_count"),
        "groups": m.get("groups", {}).get(view, {}),
    }


def repair(path, d2=False):
    m = load(path)
    rep = m.get("repair") or {}
    fc = m.get("fixed_cohort_repair") or {}
    return {
        "n": rep.get("n"), "parse": rep.get("parse_successes"), "psr": rep.get("psr"),
        "deploy": rep.get("deployment_passes"), "pr": rep.get("pr"),
        "pr_lb": rep.get("pr_lower_bound"),
        "missing": rep.get("deployment_missing_cases"),
        "resolved": rep.get("resolved"), "introduced": rep.get("introduced"),
        "cohort_n": fc.get("n"), "cohort_succ": fc.get("successes"),
        "cohort_rate": fc.get("success_rate"), "cohort_lb": fc.get("success_rate_lower_bound"),
    }


def deployfix_detect(dataset):
    name = "deployfix-d1.json" if dataset == "D1" else "deployfix-d2.json"
    d = json.load(open(os.path.join(DFX, name), encoding="utf-8"))
    truth_root = (f"{ROOT}/outputs/D1-collect-26-10-v3/dataset" if dataset == "D1"
                  else f"{ROOT}/outputs/D2-collect-server-01/dataset")
    tp = fp = fn = tn = 0
    jacs = []
    for case, rec in d.items():
        tpath = os.path.join(truth_root, case, "truth.json")
        if dataset == "D1":
            # D1 truth is read from the cases in paper-metrics, not from truth.json
            truth = D1_TRUTH[case]
        else:
            truth = json.load(open(tpath, encoding="utf-8"))
        isp = truth["is_pending"]
        pred_fault = rec["status"] == "fault"
        if isp and pred_fault: tp += 1
        elif isp: fn += 1
        elif pred_fault: fp += 1
        else: tn += 1
        if isp:
            T = set(truth.get("category_ids") or truth.get("pending_labels") or [])
            P = set(rec["category_ids"]) if pred_fault else set()
            jacs.append(len(T & P) / len(T | P) if (T | P) else 1.0)
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else None
    secs = [v["seconds"] for v in d.values()]
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "p": p, "r": r, "f1": f1,
            "jac": sum(jacs) / len(jacs) if jacs else None, "exact": None,
            "seconds": sum(secs) / len(secs) if secs else None, "cases": len(d)}


def deployfix_groups(dataset):
    path = os.path.join(DFX, "deployfix-d1.json" if dataset == "D1" else "deployfix-d2.json")
    d = json.load(open(path, encoding="utf-8"))
    truth_root = (f"{ROOT}/outputs/D1-collect-26-10-v3/dataset" if dataset == "D1"
                  else f"{ROOT}/outputs/D2-collect-server-01/dataset")
    out = {g: {"tp": 0, "fp": 0, "fn": 0, "j": []} for g in "RAPSE"}
    for case, rec in d.items():
        if dataset == "D1":
            truth = D1_TRUTH[case]
            isp = truth["is_pending"]
            labels = truth.get("category_ids") or []
        else:
            t = json.load(open(os.path.join(truth_root, case, "truth.json"), encoding="utf-8"))
            isp = t["is_pending"]
            labels = t.get("pending_labels") or []
        pred = set(rec["category_ids"]) if rec["status"] == "fault" else set()
        for g in "RAPSE":
            T = {x for x in labels if x.startswith(g)}
            P = {x for x in pred if x.startswith(g)}
            if isp and T:
                out[g]["tp" if P else "fn"] += 1
                out[g]["j"].append(len(T & P) / len(T | P))
            elif isp and not T and P:
                out[g]["fp"] += 1
    res = {}
    for g, v in out.items():
        tp, fn = v["tp"], v["fn"]
        res[g] = {"recall": tp / (tp + fn) if tp + fn else None,
                  "jaccard": sum(v["j"]) / len(v["j"]) if v["j"] else None}
    return res


def deployfix_repair(dataset):
    s = json.load(open(os.path.join(DFX, f"repair-{dataset.lower()}", "summary.json"), encoding="utf-8"))
    fixed = sum(1 for v in s.values() if v["outcome"] == "fixed")
    still = sum(1 for v in s.values() if v["outcome"] == "still")
    crash = sum(1 for v in s.values() if v["outcome"] == "crash")
    nosol = sum(1 for v in s.values() if v["outcome"] == "no_solution")
    return {"n": len(s), "fixed": fixed, "still": still, "crash": crash, "nosol": nosol}


pm = json.load(open(f"{ROOT}/tmp/baseline-adapt/runs/gpt56terra/detection-eval/paper-metrics.json"))
D1_TRUTH = {c["case"]: c["truth"] for c in pm["cases"]}

RUNS = {
    "D1": {
        "SlsDetector-GPT": (f"{ROOT}/tmp/baseline-adapt/runs/gpt56terra/detection-eval/paper-metrics.json",
                            f"{ROOT}/tmp/baseline-adapt/runs/gpt56terra/repair-eval/paper-metrics.json"),
        "SlsDetector-DeepSeek": (f"{ROOT}/tmp/baseline-adapt/runs/ds41flash/detection-eval/paper-metrics.json",
                                 f"{ROOT}/tmp/baseline-adapt/runs/ds41flash/repair-eval/paper-metrics.json"),
        "ConfDR-GPT": (f"{ROOT}/outputs/D1-method-gpt56terra/paper-metrics.json",) * 2,
        "ConfDR-DeepSeek": (f"{ROOT}/outputs/D1-method-ds41flash/paper-metrics.json",) * 2,
        "ConfDR-GPT w/o LG": (f"{ROOT}/outputs/nolg-gpt56terra-d1/paper-metrics.json",) * 2,
        "ConfDR-DeepSeek w/o LG": (f"{ROOT}/outputs/nolg-ds41flash-d1/paper-metrics.json",) * 2,
    },
    "D2": {
        "SlsDetector-GPT": (f"{ROOT}/tmp/baseline-adapt/runs/gpt56terra-d2/detection-eval/paper-metrics.json",
                            f"{ROOT}/tmp/baseline-adapt/runs/gpt56terra-d2/repair-eval/paper-metrics.json"),
        "SlsDetector-DeepSeek": (f"{ROOT}/tmp/baseline-adapt/runs/ds41flash-d2/detection-eval/paper-metrics.json",
                                 f"{ROOT}/tmp/baseline-adapt/runs/ds41flash-d2/repair-eval/paper-metrics.json"),
        "ConfDR-GPT": (f"{ROOT}/outputs/D2-method-gpt56terra/paper-metrics.json",) * 2,
        "ConfDR-DeepSeek": (f"{ROOT}/outputs/D2-method-ds41flash-d2/paper-metrics.json",) * 2,
        "ConfDR-GPT w/o LG": (f"{ROOT}/outputs/nolg-gpt56terra-d2/paper-metrics.json",) * 2,
        "ConfDR-DeepSeek w/o LG": (f"{ROOT}/outputs/nolg-ds41flash-d2/paper-metrics.json",) * 2,
    },
}


def main():
    out_md = ["# DeployFix / SlsDetector / ConfDR metrics", "",
              "Detection uses the `combined_detection` view. "
              "GPT is GPT-5.6-Terra and DeepSeek is DeepSeek-V4.1-Flash for ConfDR and SlsDetector.", ""]
    rows = []
    for dataset in ("D1", "D2"):
        out_md += [f"## {dataset}", ""]
        det_rows = []
        rep_rows = []
        for method, (dpath, rpath) in RUNS[dataset].items():
            d = detection(dpath)
            r = repair(rpath)
            d['src'] = dpath.replace(ROOT + "/", "")
            det_rows.append((method, d))
            r['src'] = rpath.replace(ROOT + "/", "")
            rep_rows.append((method, r))
            rows.append([dataset, method, "detect", d["tp"], d["fp"], d["fn"], d["tn"],
                         pct(d["p"]), pct(d["r"]), pct(d["f1"]), num(d["jac"]),
                         pct(d["exact"]), num(d["seconds"]), d["cases"]])
            rows.append([dataset, method, "repair", r["n"], r["parse"], r["deploy"],
                         "—", pct(r["psr"]), pct(r["pr"]), pct(r["pr_lb"]),
                         pct(r["resolved"]), pct(r["introduced"]), "—", r["cohort_n"]])

        ddf = deployfix_detect(dataset)
        ddf["groups"] = deployfix_groups(dataset)
        ddf["src"] = os.path.relpath(os.path.join(DFX, f"deployfix-{dataset.lower()}.json"), ROOT)
        det_rows.append(("DeployFix", ddf))
        rows.append([dataset, "DeployFix", "detect", ddf["tp"], ddf["fp"], ddf["fn"], ddf["tn"],
                     pct(ddf["p"]), pct(ddf["r"]), pct(ddf["f1"]), num(ddf["jac"]),
                     "—", num(ddf["seconds"]), ddf["cases"]])

        rdf = deployfix_repair(dataset)
        rows.append([dataset, "DeployFix", "repair", rdf["n"], "—", rdf["fixed"], rdf["still"],
                     "—", pct(rdf["fixed"] / rdf["n"] if rdf["n"] else None), "—",
                     "—", "—", "—", rdf["n"]])

        out_md += ["### Detection", "",
                   "| Method | Source | TP | FP | FN | TN | Precision | Recall | F1 | Jaccard | Exact | s/case | Cases |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, d in det_rows:
            out_md.append(f"| {name} | `{d.get('src','—')}` | {d['tp']} | {d['fp']} | {d['fn']} | {d['tn']} | "
                          f"{pct(d['p'])} | {pct(d['r'])} | {pct(d['f1'])} | {num(d['jac'])} | "
                          f"{pct(d['exact'])} | {num(d['seconds'])} | {d['cases']} |")

        out_md += ["", "### Repair", "",
                   "| Method | Source | Repaired | Parsed | PSR | Deployed | PR | PR lower bound | Resolved | Introduced | Fixed-cohort success |",
                   "|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, r in rep_rows:
            out_md.append(f"| {name} | `{r.get('src','—')}` | {r['n']} | {r['parse']} | {pct(r['psr'])} | {r['deploy']} | "
                          f"{pct(r['pr'])} | {pct(r['pr_lb'])} | {pct(r['resolved'])} | "
                          f"{pct(r['introduced'])} | {pct(r['cohort_rate'])} |")
        out_md.append(f"| DeployFix | `{os.path.relpath(os.path.join(DFX, f'repair-{dataset.lower()}', 'summary.json'), ROOT)}` | {rdf['n']} | — | — | — | — | — | — | — | "
                      f"{pct(rdf['fixed'] / rdf['n'] if rdf['n'] else None)} † |")
        out_md.append("")

        # group table
        out_md += ["### Per-group detection (one-vs-rest, Recall / Jaccard)", "",
                   "| Method | R | A | P | S | E |", "|---|---|---|---|---|---|"]
        for name, d in det_rows:
            g = d.get("groups") or {}
            cells = []
            for grp in "RAPSE":
                v = g.get(grp) or {}
                cells.append(f"{pct(v.get('recall'))} / {num(v.get('jaccard'))}")
            out_md.append(f"| {name} | " + " | ".join(cells) + " |")
        out_md.append("")

    out_md += ["> † A DeployFix repair counts as successful when its own recheck reports no conflict, not when "
               "the KWOK deployment passes, so it is not directly comparable with the PR/Resolved of the other methods.", ""]

    md = "\n".join(out_md)
    open(os.path.join(DFX, "metrics-summary.md"), "w", encoding="utf-8").write(md)

    import csv
    with open(os.path.join(DFX, "metrics-summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["dataset", "method", "stage", "tp_or_n", "fp_or_parse", "fn_or_deploy",
                    "tn_or_still", "precision_or_psr", "recall_or_pr", "f1_or_pr_lb",
                    "jaccard_or_resolved", "exact_or_introduced", "seconds", "cases"])
        w.writerows(rows)
    print(md)


if __name__ == "__main__":
    sys.exit(main())
