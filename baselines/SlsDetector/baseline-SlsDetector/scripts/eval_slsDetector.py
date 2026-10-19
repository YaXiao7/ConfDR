import json
from pathlib import Path
from typing import List, Dict, Set, Tuple


# ================= CONFIG =================
PARENT_DIR = Path("../dataset")
SUBSETS = [
    "baseline-deploy-fix/k8s",
    "extended_cases",
    "stackoverflow_cases",
]

SAVE_PATH = Path("../output/slsDetector_eval_all.json")
SAVE_TXT = Path("../output/slsDetector_eval_all.txt")
# =========================================


# ----------- Load truth.json ----------------

def load_truth(truth_path: Path) -> Tuple[bool, Set[str]]:
    with truth_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return bool(data.get("is_pending", False)), set(data.get("pending_labels", []))


# ----------- Load slsDetector_pred.json ----------------

def load_sls_pred(pred_path: Path) -> Tuple[bool, Set[str]]:
    with pred_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    pred_is_pending = bool(data.get("pred_is_pending", False))
    pred_labels = set(data.get("pred_labels", []))
    solve_time_sec = float(data.get("solve_time_sec", 0.0))

    return pred_is_pending, pred_labels, solve_time_sec


# ----------- Struct for one case ----------------

class CaseResult:
    def __init__(
        self,
        case_id,
        gt_is_pending,
        gt_labels,
        pred_is_pending,
        pred_labels,
        solve_time_sec,
    ):
        self.case_id = case_id
        self.gt_is_pending = gt_is_pending
        self.gt_labels = gt_labels
        self.pred_is_pending = pred_is_pending
        self.pred_labels = pred_labels
        self.solve_time_sec = solve_time_sec

# ----------- Load all cases of a dataset ----------------

def load_all_cases(base_dir: Path) -> List[CaseResult]:
    results = []

    if not base_dir.exists():
        print(f"[WARN] Dataset not found: {base_dir}")
        return results

    for case_dir in sorted([p for p in base_dir.iterdir() if p.is_dir()]):

        truth_path = case_dir / "truth.json"
        pred_path = case_dir / "slsDetector_pred.json"

        if (not truth_path.exists()) or (not pred_path.exists()):
            print(f"[WARN] Skipping {case_dir.name} (missing truth or slsDetector_pred)")
            continue

        gt_is_pending, gt_labels = load_truth(truth_path)
        pred_is_pending, pred_labels, solve_time_sec = load_sls_pred(pred_path)


        results.append(
            CaseResult(
                case_id=case_dir.name,
                gt_is_pending=gt_is_pending,
                gt_labels=gt_labels,
                pred_is_pending=pred_is_pending,
                pred_labels=pred_labels,
                solve_time_sec=solve_time_sec,
            )
        )

    return results


# ----------- Pending Detection Metrics ----------------

def evaluate_avg_solve_time(cases: List[CaseResult]) -> float:
    times = [c.solve_time_sec for c in cases if c.solve_time_sec > 0]
    if not times:
        return 0.0
    return round(sum(times) / len(times), 3)


def evaluate_pending_detection(cases: List[CaseResult]) -> Dict[str, float]:
    TP = FP = FN = TN = 0

    for c in cases:
        gt = c.gt_is_pending
        pred = c.pred_is_pending

        if gt and pred:
            TP += 1
        elif (not gt) and pred:
            FP += 1
        elif gt and (not pred):
            FN += 1
        else:
            TN += 1

    precision = TP / float(TP + FP) if (TP + FP) > 0 else 0.0
    recall = TP / float(TP + FN) if (TP + FN) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    acc = (TP + TN) / float(TP + TN + FP + FN) if (TP + TN + FP + FN) > 0 else 0.0

    return {
        "TP": TP,
        "FP": FP,
        "FN": FN,
        "TN": TN,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": acc,
    }


# ----------- Explanation Jaccard ----------------

def evaluate_explanation_jaccard(cases: List[CaseResult]) -> Dict[str, float]:
    total_j = 0.0
    count = 0

    for c in cases:
        if not c.gt_is_pending:
            continue

        G = set(c.gt_labels)
        P = set(c.pred_labels)

        union = G | P
        inter = G & P

        j = (len(inter) / float(len(union))) if union else 0.0
        total_j += j
        count += 1

    avg_j = total_j / float(count) if count > 0 else 0.0

    return {
        "pending_case_count": count,
        "avg_jaccard": avg_j,
    }


# ----------- Total Evaluation (Global + Per Dataset) ----------------

def main():

    per_dataset = {}
    all_cases: List[CaseResult] = []

    print("=== Evaluating SlsDetector on all datasets ===")

    for subset in SUBSETS:
        subset_dir = PARENT_DIR / subset
        print(f"\n--- Loading subset: {subset} ---")

        cases = load_all_cases(subset_dir)
        all_cases.extend(cases)

        det = evaluate_pending_detection(cases)
        expl = evaluate_explanation_jaccard(cases)

        avg_time = evaluate_avg_solve_time(cases)

        per_dataset[subset] = {
            "dataset_root": str(subset_dir),
            "num_cases": len(cases),
            "pending_detection": det,
            "explanation": expl,
            "efficiency": {
                "avg_solve_time_sec": avg_time
            },
        }

        print(f"Cases: {len(cases)}")
        print("  Pending Detection:", det)
        print("  Explanation:", expl)

    # -------- Global evaluation --------
    global_det = evaluate_pending_detection(all_cases)
    global_avg_time = evaluate_avg_solve_time(all_cases)
    global_expl = evaluate_explanation_jaccard(all_cases)

    results = {
        "parent_root": str(PARENT_DIR),
        "num_datasets": len(SUBSETS),
        "total_cases": len(all_cases),
        "global": {
            "pending_detection": global_det,
            "explanation": global_expl,
            "efficiency": {
                "avg_solve_time_sec": global_avg_time
            },
        },
        "per_dataset": per_dataset,
    }

    # -------- Save JSON --------
    SAVE_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")

    # -------- Save TXT --------
    with SAVE_TXT.open("w", encoding="utf-8") as f:
        f.write(f"Parent dataset root: {PARENT_DIR}\n")
        f.write(f"Sub-datasets: {', '.join(SUBSETS)}\n")
        f.write(f"Total cases: {len(all_cases)}\n\n")

        f.write("=== Global Pending Detection ===\n")
        f.write(json.dumps(global_det, indent=2))
        f.write("\n\n")

        f.write("=== Global Explanation (Jaccard) ===\n")
        f.write(json.dumps(global_expl, indent=2))
        f.write("\n\n")

        f.write("=== Global Efficiency ===\n")
        f.write(json.dumps(
            {"avg_solve_time_sec": global_avg_time}, indent=2
        ))
        f.write("\n\n")


        f.write("=== Per-dataset metrics ===\n\n")
        for subset in SUBSETS:
            f.write(f"--- {subset} ---\n")
            f.write(json.dumps(per_dataset[subset], indent=2))
            f.write("\n\n")
            f.write("Efficiency:\n")
            f.write(json.dumps(
                per_dataset[subset]["efficiency"], indent=2
            ))
            f.write("\n\n")


    print("\nSaved results:")
    print("  JSON:", SAVE_PATH)
    print("  TXT :", SAVE_TXT)


if __name__ == "__main__":
    main()
