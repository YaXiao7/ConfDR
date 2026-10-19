"""Read current and legacy ground truth and summarize three-state detection; never count UNKNOWN or missing results as healthy."""

from __future__ import annotations

from pathlib import Path

from confdr.shared.io import read


def truth(case_dir: str | Path, label_mapping: dict | None = None, *, known_categories=None) -> dict:
    """Original labels are used only for evaluation; no legacy category mapping is built in."""
    directory = Path(case_dir)
    path = next((directory / n for n in ("truth_v2.json", "truth.json") if (directory / n).is_file()), None)
    if path is None:
        return {"status": "unlabelled", "category_ids": [], "category_labelled": False}
    raw = read(path)
    if not isinstance(raw, dict):
        raise ValueError("truth must be an object")
    if raw.get("annotation_status") == "needs_review":
        return {"status": "unlabelled", "category_ids": [], "category_labelled": False}
    status = raw.get("status", raw.get("deployment_health"))
    explicit_pending = (
        raw.get("is_pending")
        if "is_pending" in raw
        else (True if status == "pending" else False if status == "healthy" else None)
    )
    legacy = "is_pending" in raw
    if legacy:
        if type(raw["is_pending"]) is not bool:
            raise ValueError("truth.is_pending must be a boolean")
        status = "fault" if raw["is_pending"] else "healthy"
    if status in {"pending", "runtime_fault", "unhealthy", "failed"}:
        status = "fault"
    if status not in {"fault", "healthy"}:
        status = "unlabelled"
    labels = raw.get("category_ids", [])
    unmapped = []
    category_labelled = "category_ids" in raw
    if not category_labelled and "pending_labels" in raw:
        old = raw.get("pending_labels", [])
        if not isinstance(old, list) or any(not isinstance(x, str) for x in old):
            raise ValueError("pending_labels must be a list of strings")
        known = set(known_categories or [])
        mapping = label_mapping or {}
        unmapped = [x for x in old if x not in known and x not in mapping]
        labels = sorted({v for x in old for v in (mapping[x] if x in mapping else [x] if x in known else [])})
        category_labelled = not unmapped and (
            bool(old) or status == "healthy" or raw.get("annotation_status") == "complete"
        )
    if not isinstance(labels, list) or any(not isinstance(x, str) or not x.strip() for x in labels):
        raise ValueError("truth.category_ids must be a list of nonempty strings")
    if unmapped:
        labels = []
    return {
        "status": status,
        "is_pending": explicit_pending,
        "category_ids": sorted(set(labels)),
        "category_labelled": category_labelled,
        "unmapped_labels": unmapped,
        "pending_labels": raw.get("pending_labels", []),
        "scope": "original_pending" if legacy else "original_fault",
        "source": str(path),
    }


def evaluate(records: list[dict]) -> dict:
    labelled = [r for r in records if r.get("truth", {}).get("status") in {"fault", "healthy"}]
    matrix = {
        actual: {pred: 0 for pred in ("fault", "healthy", "unknown")} for actual in ("fault", "healthy")
    }
    category_exact = category_total = 0
    for record in labelled:
        expected = record["truth"]
        detection = record.get("result", {}).get("detection", {})
        predicted = detection.get("status", "unknown")
        predicted = predicted if predicted in {"fault", "healthy"} else "unknown"
        matrix[expected["status"]][predicted] += 1
        if expected["status"] == "fault" and expected.get("category_ids"):
            category_total += 1
            category_exact += predicted == "fault" and set(expected["category_ids"]) == set(
                detection.get("category_ids", [])
            )
    tp, fp = matrix["fault"]["fault"], matrix["healthy"]["fault"]
    positive = sum(matrix["fault"].values())
    total = len(labelled)
    unknown = sum(row["unknown"] for row in matrix.values())
    formal_records = [
        record
        for record in records
        if isinstance(record.get("result", {}).get("detection", {}).get("formal"), dict)
    ]
    # Record absent completeness fields in legacy results as unreported; do not infer that the search was interrupted.
    reported_formal = [
        record for record in formal_records if "formal_complete" in record["result"]["detection"]["formal"]
    ]
    formal_complete = [
        record
        for record in reported_formal
        if record["result"]["detection"]["formal"].get("formal_complete") is True
    ]
    formal_incomplete_cases = [
        record.get("case", "")
        for record in reported_formal
        if record["result"]["detection"]["formal"].get("formal_complete") is not True
    ]
    core_search_incomplete_cases = [
        record.get("case", "")
        for record in formal_records
        if record["result"]["detection"]["formal"].get("core_search_complete") is False
    ]
    return {
        "total_records": len(records),
        "labelled": total,
        "unlabelled": len(records) - total,
        "confusion": matrix,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall_including_abstentions": tp / positive if positive else None,
        "accuracy_including_abstentions": (tp + matrix["healthy"]["healthy"]) / total if total else None,
        "coverage": (total - unknown) / total if total else None,
        "decision_coverage": (total - unknown) / total if total else None,
        "formal_coverage": len(formal_complete) / len(records) if records else None,
        "formal_coverage_unreported_cases": [
            record.get("case", "")
            for record in records
            if "formal_complete" not in (record.get("result", {}).get("detection", {}).get("formal") or {})
        ],
        "formal_incomplete_cases": formal_incomplete_cases,
        "core_search_incomplete_cases": core_search_incomplete_cases,
        "category_exact_match": category_exact / category_total if category_total else None,
        "multilabel": multilabel_metrics(records),
    }


def category_ids(record: dict) -> set[str]:
    label = record.get("truth", {})
    values = label.get("category_ids", [])
    if label.get("status") != "fault" or not isinstance(values, list):
        return set()
    return {x for x in values if isinstance(x, str) and x.strip()}


def novelty(record: dict, seen: set[str]) -> str:
    status = record.get("truth", {}).get("status")
    if status == "healthy":
        return "healthy"
    if status != "fault":
        return "unlabelled"
    categories = category_ids(record)
    if not categories:
        return "fault_without_category"
    if categories <= seen:
        return "seen_only"
    return "mixed_seen_unseen" if categories & seen else "unseen_only"


def evaluate_partition(
    records: list[dict], train: list[dict], *, known_categories: list[str] | None = None
) -> dict:
    """Measure unknown categories using held-out ground truth; the detector need not know their names in advance."""
    seen = (
        set(known_categories)
        if known_categories is not None
        else set().union(*(category_ids(r) for r in train))
    )
    unseen = set().union(*(category_ids(r) for r in records)) - seen
    groups = {
        g: [r for r in records if novelty(r, seen) == g]
        for g in (
            "seen_only",
            "unseen_only",
            "mixed_seen_unseen",
            "healthy",
            "fault_without_category",
            "unlabelled",
        )
    }
    result = {
        "reference_scope": "catalog_taxonomy" if known_categories is not None else "training_labels",
        "known_category_ids": sorted(seen),
        "unseen_category_ids": sorted(unseen),
        "unseen_category_count": len(unseen),
        "unseen_case_count": sum(bool(category_ids(r) - seen) for r in records),
        "overall": evaluate(records),
        "by_novelty": {g: evaluate(rs) for g, rs in groups.items()},
        "unseen_cases": evaluate([r for r in records if category_ids(r) - seen]),
        "per_category": {},
    }
    for cat in sorted(set().union(*(category_ids(r) for r in records))):
        subset = [r for r in records if cat in category_ids(r)]
        detected = [r for r in subset if r.get("result", {}).get("detection", {}).get("status") == "fault"]
        named = [r for r in detected if cat in r["result"]["detection"].get("category_ids", [])]
        result["per_category"][cat] = {
            "unseen_in_catalog" if known_categories is not None else "unseen_in_training": cat not in seen,
            "cases": len(subset),
            "fault_detected": len(detected),
            "fault_detection_recall": len(detected) / len(subset),
            "category_identified": len(named),
            "category_recall": len(named) / len(subset),
        }
    return result


def compare_labels(expected: dict, detection: dict) -> dict:
    """Compare only original-input predictions; missing truth and abstentions are explicit."""
    labelled = expected.get("category_labelled", bool(expected.get("category_ids")))
    if expected.get("status") not in {"fault", "healthy"} or not labelled:
        return {
            "scored": False,
            "reason": "missing_or_unmapped_category_labels",
            "unmapped_labels": expected.get("unmapped_labels", []),
        }
    actual = set(expected.get("category_ids", []))
    predicted = set(detection.get("category_ids", [])) if detection.get("status") == "fault" else set()
    tp, fp, fn = actual & predicted, predicted - actual, actual - predicted
    return {
        "scored": True,
        "truth": sorted(actual),
        "predicted": sorted(predicted),
        "tp": sorted(tp),
        "fp": sorted(fp),
        "fn": sorted(fn),
        "abstained": detection.get("status") not in {"fault", "healthy"},
        "exact_match": detection.get("status") == expected["status"] and actual == predicted,
    }


def multilabel_metrics(records: list[dict]) -> dict:
    comparisons = [
        compare_labels(r.get("truth", {}), r.get("result", {}).get("detection", {})) for r in records
    ]
    scored = [c for c in comparisons if c["scored"]]
    labels = sorted({label for c in scored for label in c["truth"] + c["predicted"]})

    def rates(tp, fp, fn):
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        }

    per_label = {
        label: rates(*(sum(label in c[key] for c in scored) for key in ("tp", "fp", "fn")))
        for label in labels
    }
    micro = rates(*(sum(len(c[key]) for c in scored) for key in ("tp", "fp", "fn")))
    f1s = [m["f1"] for m in per_label.values() if m["f1"] is not None]
    return {
        "scored_cases": len(scored),
        "unscored_cases": len(comparisons) - len(scored),
        "abstentions": sum(c["abstained"] for c in scored),
        "exact_match": sum(c["exact_match"] for c in scored) / len(scored) if scored else None,
        "micro": micro,
        "macro_f1": sum(f1s) / len(f1s) if f1s else None,
        "per_category": per_label,
    }
