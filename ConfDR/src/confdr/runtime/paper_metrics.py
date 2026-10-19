"""Paper-aligned metrics from frozen method outputs and independent evaluation evidence."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from confdr.collection.files import load_documents
from confdr.runtime.attribution import attributed_categories
from confdr.runtime.evaluation import multilabel_metrics
from confdr.shared.catalog import source_content
from confdr.shared.io import digest, read, write
from confdr.validation.repair_metrics import repair_entered, select_candidate, select_prefix_candidate

VIEWS = ("single_round", "all_conflicts", "combined_detection")
GROUPS = ("R", "A", "P", "S", "E")


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def measurement(values):
    observed = [v for v in values if _number(v)]
    return {
        "total": sum(observed) if len(observed) == len(values) and values else None,
        "average": ratio(sum(observed), len(values)) if len(observed) == len(values) else None,
        "observed_total": sum(observed),
        "observed_average": ratio(sum(observed), len(observed)),
        "recorded_cases": len(observed),
        "missing_cases": len(values) - len(observed),
        "denominator": len(values),
    }


def predictions(detection):
    formal = detection.get("formal") or {}
    result = {}
    for name, key in (("single_round", "initial_core"), ("all_conflicts", "core")):
        available = key in formal and formal.get("solver_status") not in {None, "not_run"}
        # Do not use filtered terminal SAT as a prediction on the original case.
        raw = formal.get("solver_status") if name == "single_round" else formal.get("status")
        state = (
            "fault"
            if available and raw == "unsat"
            else (
                "healthy"
                if available and raw == "sat" and formal.get("formal_complete") is True
                else "unknown"
            )
        )
        # Apply the category attribution of the final detection so that the views differ only in their cores.
        labels = attributed_categories(formal.get(key, []))
        result[name] = {"status": state, "category_ids": labels, "recorded": available}
    combined = {
        "status": detection.get("status", "unknown"),
        "category_ids": detection.get("category_ids", []),
        "recorded": bool(detection),
    }
    if combined["status"] == "fault" and formal.get("status") != "unsat":
        findings = (detection.get("llm") or {}).get("findings", [])
        # A runtime-only LLM fault is not evidence of a Pending scheduling defect.
        relevant = [
            f for f in findings if f.get("verification_scope") == "control_plane" and f.get("category_ids")
        ]
        if not relevant:
            combined.update(status="unknown", category_ids=[], reason="no_pending_scope_evidence")
        else:
            combined["category_ids"] = sorted({c for f in relevant for c in f["category_ids"]})
    result["combined_detection"] = combined
    # w/o LS removes only label skipping: LLM-assisted detection does not use the cores,
    # so a formal UNKNOWN keeps the LLM-assisted result in the first-core view.
    if formal.get("status") == "unknown":
        result["single_round"] = {**combined, "recorded": result["single_round"]["recorded"] or combined["recorded"]}
    return result


def pending_truth(expected):
    if type(expected.get("is_pending")) is bool:
        return expected["is_pending"]
    # Older normalized truth retained the scope but not the explicit Boolean.
    if expected.get("scope") == "original_pending" and expected.get("status") in {"fault", "healthy"}:
        return expected["status"] == "fault"
    return None


def detection_metrics(cases, view):
    counts = Counter()
    jaccards, exact, category_records = [], [], []
    eligible_pending = 0
    for row in cases:
        expected, predicted = row["truth"], row["predictions"][view]
        actual = pending_truth(expected)
        state = predicted["status"] if predicted["status"] in {"fault", "healthy"} else "unknown"
        if actual is None:
            counts["unlabelled"] += 1
        elif state == "unknown":
            counts["unknown_pending" if actual else "unknown_nonpending"] += 1
        else:
            counts[("tp" if actual else "fp") if state == "fault" else ("fn" if actual else "tn")] += 1
        if actual is True:
            eligible_pending += 1
            if expected.get("category_labelled") is True and predicted["recorded"]:
                g, p = set(expected.get("category_ids", [])), set(predicted["category_ids"])
                # Legacy paper convention: an explicitly annotated empty union scores 0.
                jaccards.append(len(g & p) / len(g | p) if g | p else 0.0)
                exact.append(g == p)
        category_records.append({"truth": expected, "result": {"detection": predicted}})
    tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
    decided = tp + fp + fn + tn
    unknown = counts["unknown_pending"] + counts["unknown_nonpending"]
    labelled = decided + unknown
    return {
        **{
            k: counts[k]
            for k in ("tp", "fp", "fn", "tn", "unknown_pending", "unknown_nonpending", "unlabelled")
        },
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "accuracy": ratio(tp + tn, decided),
        "binary_scope": "explicit Pending truth; standard metrics on decided cases",
        "decided_cases": decided,
        "labelled_cases": labelled,
        "coverage": ratio(decided, labelled),
        "recall_including_abstentions": ratio(tp, tp + fn + counts["unknown_pending"]),
        "accuracy_including_abstentions": ratio(tp + tn, labelled),
        "avg_jaccard": ratio(sum(jaccards), len(jaccards)),
        "exact_match": ratio(sum(exact), len(exact)),
        "jaccard_scored_cases": len(jaccards),
        "jaccard_missing_cases": eligible_pending - len(jaccards),
        "jaccard_scope": "truth Pending only; deduplicated categories; unknown predictions retain any found labels",
        "secondary_category_metrics": multilabel_metrics(category_records),
    }


def group_metrics(cases, view):
    """One-vs-rest group scores; a multi-group case contributes to each true group."""
    groups = {}
    for group in GROUPS:
        counts = Counter()
        jaccards = []
        for row in cases:
            truth = row["truth"]
            actual = pending_truth(truth)
            if actual is None or truth.get("category_labelled") is not True:
                counts["unlabelled"] += 1
                continue
            predicted = row["predictions"][view]
            true_labels = {c for c in truth.get("category_ids", []) if c.startswith(group)}
            predicted_labels = {c for c in predicted.get("category_ids", []) if c.startswith(group)}
            true_group = actual and bool(true_labels)
            if true_group:
                counts["truth_positive"] += 1
                if predicted.get("recorded"):
                    union = true_labels | predicted_labels
                    jaccards.append(len(true_labels & predicted_labels) / len(union))
            status = predicted.get("status")
            if status not in {"fault", "healthy"}:
                counts["unknown_positive" if true_group else "unknown_negative"] += 1
                continue
            found = status == "fault" and bool(predicted_labels)
            counts[("tp" if true_group else "fp") if found else ("fn" if true_group else "tn")] += 1
        tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
        groups[group] = {
            **{k: counts[k] for k in (
                "truth_positive", "tp", "fp", "fn", "tn", "unknown_positive", "unknown_negative", "unlabelled"
            )},
            "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn),
            "recall_including_abstentions": ratio(tp, counts["truth_positive"]),
            "jaccard": ratio(sum(jaccards), len(jaccards)),
            "jaccard_scored_cases": len(jaccards),
            "jaccard_missing_cases": counts["truth_positive"] - len(jaccards),
        }
    return groups


def fixed_cohort_repair_metrics(cases):
    """End-to-end outcome on every truth-Pending case, including missed detections."""
    cohort = [c for c in cases if pending_truth(c["truth"]) is True]
    successes = sum(
        c["repair_entered"]
        and c["candidate"].get("selection") == "accepted_output"
        and c["deployment_status"] == "passed"
        and c["pending"] is False
        for c in cohort
    )
    missing = sum(
        c["repair_entered"]
        and c["candidate"].get("selection") == "accepted_output"
        and (
            c["deployment_status"] not in {"passed", "failed", "no_candidate", "blocked"}
            or (c["deployment_status"] == "passed" and c["pending"] is None)
        )
        for c in cohort
    )
    n = len(cohort)
    paired = [
        c for c in cohort
        if c.get("original_quality_status") == "scored" and c["quality_status"] == "scored"
    ]
    before = sum(len(c["original_categories"]) for c in paired)
    after = sum(len(c["post_categories"]) for c in paired)
    introduced = sum(
        len(set(c["post_categories"]) - set(c["original_categories"])) for c in paired
    )
    complete_quality = len(paired) == n and n > 0
    return {
        "cohort": "all truth-Pending cases; success requires accepted method output, passed deployment, and non-Pending observation",
        "n": n,
        "method_repair_entered": sum(c["repair_entered"] for c in cohort),
        "successes": successes,
        "success_rate": ratio(successes, n) if missing == 0 else None,
        "success_rate_lower_bound": ratio(successes, n),
        "deployment_missing_cases": missing,
        "configuration_violation_reduction_rate": (
            ratio(before - after, before) if complete_quality else None
        ),
        "formal_average_introduced_errors": ratio(introduced, n) if complete_quality else None,
        "paired_quality_cases": len(paired),
        "paired_quality_missing_cases": n - len(paired),
        "observed_paired_quality": {
            "n": len(paired),
            "initial_error_categories": before,
            "final_error_categories": after,
            "introduced_error_categories": introduced,
            "configuration_violation_reduction_rate": ratio(before - after, before),
            "average_introduced_errors": ratio(introduced, len(paired)),
        },
    }


KNOWN_DEPLOYMENT = {"passed", "failed", "no_candidate", "blocked"}


def detected_categories(case):
    """Categories that detection reported for a case that entered repair."""
    prediction = (case.get("predictions") or {}).get("combined_detection") or {}
    return prediction.get("category_ids") if prediction.get("status") == "fault" else None


def detected_quality(rows):
    """Resolved and Introduced over every case that enters repair (paper Eq. 11).

    Each row pairs the detected categories with the evaluated output. An output that is
    not deployed or whose recheck is incomplete resolves none of its categories. It
    introduces an error if it violates the admissibility checks, is rejected by the API
    server, or has a recheck category that detection did not report.
    """
    before = resolved = introduced = missing = 0
    for detected, outcome in rows:
        status = outcome.get("deployment_status")
        if detected is None or status not in KNOWN_DEPLOYMENT:
            missing += 1
            continue
        detected = set(detected)
        rejected = status == "blocked" or outcome.get("api_accepted") is False
        post = outcome.get("post_categories")
        scored = outcome.get("quality_status") == "scored" and isinstance(post, list)
        after = set(post) if scored and not rejected else detected
        before += len(detected)
        resolved += len(detected - after)
        introduced += rejected or (scored and bool(set(post) - detected))
    complete = missing == 0 and bool(rows)
    return {
        "resolved": ratio(resolved, before) if complete else None,
        "introduced": ratio(introduced, len(rows)) if complete else None,
        "detected_categories": before,
        "resolved_categories": resolved,
        "introduced_cases": introduced,
        "quality_evidence_missing_cases": missing,
    }


def repair_phase_calls(calls, budget=None):
    """Repair calls and the LLM-assisted detection in the rechecks that follow them.

    Diagnose calls before the first repair call belong to the initial detection, and
    failure advice (suggest_repair) is not part of repair. With a budget, the calls stop
    before the repair call of the next attempt.
    """
    phase, repairs = [], 0
    for c in calls or []:
        task = c.get("task")
        if task == "repair":
            if budget is not None and repairs == budget:
                break
            repairs += 1
            phase.append(c)
        elif task == "diagnose" and repairs:
            phase.append(c)
    return phase


def call_metrics(result):
    calls = result.get("model_calls")
    repair = [c for c in calls or [] if c.get("task") == "repair"]
    phase = repair_phase_calls(calls)
    attempts = (result.get("repair") or {}).get("attempts") or []
    # Actual absence of calls is zero; absent logging with recorded attempts is missing.
    complete_log = isinstance(calls, list) and len(repair) >= len(attempts)
    seconds = [c.get("seconds") for c in phase]
    out = {
        "calls": len(repair),
        "recheck_calls": len(phase) - len(repair),
        "call_log_complete": complete_log,
        "seconds": sum(seconds) if complete_log and all(_number(x) for x in seconds) else None,
    }
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        tokens = [(c.get("usage") or {}).get(field) for c in phase]
        # A successful response after an HTTP retry may omit usage for lost responses.
        available = (
            complete_log
            and all(type(x) is int and x >= 0 for x in tokens)
            and all(c.get("http_attempts", 1) == 1 for c in phase)
        )
        out[field] = sum(tokens) if available else None
        out["reported_" + field] = sum(x for x in tokens if type(x) is int and x >= 0)
    return out


def retry_trajectory(result, output_documents, deployment=None):
    """Prefix evidence from one realized five-round trajectory, not independent reruns."""
    if not repair_entered(result):
        return None
    attempts = (result.get("repair") or {}).get("attempts") or []
    rows = {}
    for budget in (1, 3, 5):
        prefix = attempts[:budget]
        accepted = any((a.get("validation") or {}).get("status") == "accepted" for a in prefix)
        truncated = {
            "repair": {"attempts": prefix},
            "model_calls": repair_phase_calls(result.get("model_calls"), len(prefix)),
        }
        candidate = select_prefix_candidate(result, output_documents, budget)
        calls = call_metrics(truncated)
        evidence = (deployment or {}).get(str(budget)) or {}
        trusted = (
            evidence.get("candidate", {}).get("documents_hash") == candidate["documents_hash"]
            and evidence.get("candidate", {}).get("selection") == candidate["selection"]
        )
        if candidate["selection"] in {"no_candidate", "not_in_repair_cohort"}:
            deployment_status = "no_candidate"
        elif not candidate["policy_valid"]:
            deployment_status = "blocked"
        else:
            deployment_status = evidence.get("status", "not_evaluated") if trusted else "not_evaluated"
        check = (evidence.get("postcheck") or {}) if trusted else {}
        scored = check.get("status") == "scored" and isinstance(check.get("category_ids"), list)
        rows[str(budget)] = {
            "attempts": len(prefix),
            "accepted": accepted,
            "parse_success": candidate["parse_success"],
            "policy_valid": candidate["policy_valid"],
            "candidate_hash": candidate["documents_hash"],
            "deployment_status": deployment_status,
            "deployment_evidence_trusted": trusted,
            "pending": evidence.get("pending") if trusted else None,
            "api_accepted": evidence.get("api_accepted") if trusted else None,
            "quality_status": "scored" if scored else check.get("status", "missing_postcheck"),
            "post_categories": check["category_ids"] if scored else None,
            "repair_llm_seconds": calls["seconds"],
            "repair_prompt_tokens": calls["prompt_tokens"],
            "repair_completion_tokens": calls["completion_tokens"],
            "repair_total_tokens": calls["total_tokens"],
        }
    return rows


def retry_budget_metrics(cases):
    cohort = [c for c in cases if c["retry_trajectory"] is not None]
    out = {}
    for budget in ("1", "3", "5"):
        rows = [c["retry_trajectory"][budget] for c in cohort]
        n = len(rows)
        # Same pairing as repair_metrics: both formal rechecks complete under the frozen rules.
        paired = [
            (c["original_categories"], c["retry_trajectory"][budget]["post_categories"])
            for c in cohort
            if c.get("original_quality_status") == "scored"
            and c["retry_trajectory"][budget].get("quality_status") == "scored"
        ]
        before = sum(len(original) for original, _ in paired)
        after = sum(len(post) for _, post in paired)
        introduced = sum(len(set(post) - set(original)) for original, post in paired)
        passed = sum(r["deployment_status"] == "passed" for r in rows)
        known = sum(r["deployment_status"] in {"passed", "failed", "no_candidate", "blocked"} for r in rows)
        out[budget] = {
            "n": n,
            "parse_successes": sum(r["parse_success"] is True for r in rows),
            "psr": ratio(sum(r["parse_success"] is True for r in rows), n),
            "accepted": sum(r["accepted"] for r in rows),
            "accepted_rate": ratio(sum(r["accepted"] for r in rows), n),
            "attempts": measurement([r["attempts"] for r in rows]),
            **{key: measurement([r[key] for r in rows]) for key in (
                "repair_llm_seconds", "repair_prompt_tokens", "repair_completion_tokens",
                "repair_total_tokens",
            )},
            "deployment_passes": passed,
            "deployment_pr": ratio(passed, n) if known == n else None,
            "deployment_pr_lower_bound": ratio(passed, n),
            "deployment_missing_cases": n - known,
            "deployment_counts": dict(Counter(r["deployment_status"] for r in rows)),
            "deployment_note": "Each distinct valid prefix candidate is independently evaluated in KWOK.",
            "paired_quality_cases": len(paired),
            "initial_error_categories": before,
            "final_error_categories": after,
            "introduced_error_categories": introduced,
            "configuration_violation_reduction_rate": ratio(before - after, before),
            "average_introduced_errors": ratio(introduced, len(paired)),
            **detected_quality([(detected_categories(c), c["retry_trajectory"][budget]) for c in cohort]),
        }
    return out


def repair_metrics(cases):
    cohort = [c for c in cases if c["repair_entered"]]
    n = len(cohort)
    parsed = sum(c["candidate"]["parse_success"] is True for c in cohort)
    passed = sum(c["deployment_status"] == "passed" for c in cohort)
    known = sum(c["deployment_status"] in {"passed", "failed", "no_candidate", "blocked"} for c in cohort)
    quality = [c for c in cohort if c["quality_status"] == "scored"]
    before = sum(len(c["truth"]["category_ids"]) for c in quality)
    after = sum(len(c["post_categories"]) for c in quality)
    introduced = sum(len(set(c["post_categories"]) - set(c["truth"]["category_ids"])) for c in quality)
    all_quality = len(quality) == n and n > 0
    paired = [
        c for c in cohort
        if c.get("original_quality_status") == "scored" and c["quality_status"] == "scored"
    ]
    formal_before = sum(len(c["original_categories"]) for c in paired)
    formal_after = sum(len(c["post_categories"]) for c in paired)
    formal_introduced = sum(
        len(set(c["post_categories"]) - set(c["original_categories"])) for c in paired
    )
    complete_pairing = len(paired) == n and n > 0
    return {
        "cohort": "all cases entering repair, including no candidate and all failed attempts",
        "n": n,
        "parse_successes": parsed,
        "psr": ratio(parsed, n),
        "deployment_passes": passed,
        "pr": ratio(passed, n) if known == n else None,
        "pr_lower_bound": ratio(passed, n),
        "deployment_missing_cases": n - known,
        "deployment_counts": dict(Counter(c["deployment_status"] for c in cohort)),
        "candidate_counts": dict(Counter(c["candidate"].get("selection", "unreported") for c in cohort)),
        "api_accepted": sum(c.get("api_accepted") is True for c in cohort),
        "api_rejected": sum(c.get("api_accepted") is False for c in cohort),
        "api_unknown": sum(c.get("api_accepted") is None for c in cohort),
        "pending_true": sum(c["pending"] is True for c in cohort),
        "pending_false": sum(c["pending"] is False for c in cohort),
        "pending_unknown": sum(c["pending"] is None for c in cohort),
        "average_introduced_errors": ratio(introduced, n) if all_quality else None,
        "average_introduced_errors_legacy_percent": 100 * introduced / n if all_quality else None,
        "security_improvement": ratio(before - after, before) if all_quality else None,
        "configuration_violation_reduction_rate": (
            ratio(formal_before - formal_after, formal_before) if complete_pairing else None
        ),
        "formal_average_introduced_errors": ratio(formal_introduced, n) if complete_pairing else None,
        "paired_quality_cases": len(paired),
        "paired_quality_missing_cases": n - len(paired),
        "observed_paired_quality": {
            "n": len(paired),
            "initial_error_categories": formal_before,
            "final_error_categories": formal_after,
            "introduced_error_categories": formal_introduced,
            "configuration_violation_reduction_rate": ratio(
                formal_before - formal_after, formal_before
            ),
            "average_introduced_errors": ratio(formal_introduced, len(paired)),
        },
        **detected_quality([(detected_categories(c), c) for c in cohort]),
        "quality_scored_cases": len(quality),
        "quality_missing_cases": n - len(quality),
        "quality_coverage": ratio(len(quality), n),
        "quality_missing_reasons": dict(
            Counter(c["quality_status"] for c in cohort if c["quality_status"] != "scored")
        ),
        "quality_scope": "fresh deployment formal label union under frozen rules; not exhaustive MUS or runtime security",
        "observed_quality": {
            "n": len(quality),
            "initial_error_categories": before,
            "final_error_categories": after,
            "introduced_error_categories": introduced,
            "average_introduced_errors": ratio(introduced, len(quality)),
            "security_improvement": ratio(before - after, before),
        },
    }


def group_repair_metrics(cases):
    """Repair metrics for the cases annotated with each top-level group (multi-label)."""
    return {
        group: repair_metrics(
            [c for c in cases if any(x.startswith(group) for x in c["truth"].get("category_ids", []))]
        )
        for group in GROUPS
    }


def summarize(cases):
    repairs = [c for c in cases if c["repair_entered"]]
    return {
        "case_count": len(cases),
        "detection": {view: detection_metrics(cases, view) for view in VIEWS},
        "groups": {view: group_metrics(cases, view) for view in VIEWS},
        "repair": repair_metrics(cases),
        "repair_groups": group_repair_metrics(cases),
        "fixed_cohort_repair": fixed_cohort_repair_metrics(cases),
        "retry_budgets": retry_budget_metrics(cases),
        "efficiency": {
            **{
                key: measurement([c["timing"].get(key) for c in cases])
                for key in (
                    "first_modeling_and_solving_seconds",
                    "modeling_and_solving_seconds",
                    "detection_end_to_end_seconds",
                    "rule_validation_seconds",
                    "modeling_seconds",
                    "solver_search_seconds",
                    "method_seconds",
                    "deployment_seconds",
                )
            },
            "repair_loop_seconds": measurement([c["timing"].get("repair_loop_seconds") for c in repairs]),
            **{
                "repair_" + k: measurement([c["repair_calls"].get(k) for c in repairs])
                for k in ("seconds", "prompt_tokens", "completion_tokens", "total_tokens")
            },
        },
    }


def _case_record(run, record, method):
    relative = Path(record["case"])
    target = (run / "dataset" / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(run / "dataset"):
        raise ValueError("Invalid case path in experiment.json")
    result = read(target / "result.json")
    if not result.get("method_finished") or digest(result) != record.get("method_result_hash"):
        raise ValueError(f"Method result has changed: {record['case']}")
    try:
        docs, _ = load_documents(target)
    except (OSError, ValueError, TypeError, KeyError):
        if result.get("output_documents_hash") is not None:
            raise ValueError(f"Finalized manifests cannot be read: {record['case']}") from None
        docs = []  # Input failure was already recorded by the method; keep this case visible.
    if result.get("output_documents_hash") is not None and digest(docs) != result["output_documents_hash"]:
        raise ValueError(f"Finalized manifests have changed: {record['case']}")
    candidate = select_candidate(result, docs)
    expected = record.get("truth") or {}
    dep = read(target / "deployment.json") if (target / "deployment.json").exists() else {}
    trusted = (
        dep.get("method_result_hash") == record["method_result_hash"]
        and dep.get("rules_bundle_hash") == method["_rules_bundle_hash"]
        and dep.get("output_documents_hash") == result["output_documents_hash"]
    )
    evaluation = dep.get("paper_evaluation") or {}
    if candidate["selection"] in {"no_candidate", "not_in_repair_cohort"}:
        status = "no_candidate"
    elif not candidate["policy_valid"]:
        status = "blocked"
    else:
        status = evaluation.get("deployment_status", "not_evaluated") if trusted else "not_evaluated"
    post = evaluation.get("postcheck") or {} if trusted else {}
    original_post = evaluation.get("original_postcheck") or {} if trusted else {}
    quality = "missing_postcheck"
    final_categories = None
    if not expected.get("category_labelled"):
        quality = "missing_truth_categories"
    elif post.get("status") == "scored" and isinstance(post.get("category_ids"), list):
        quality, final_categories = "scored", post["category_ids"]
    elif post:
        quality = post.get("status", "missing_postcheck")
    timing = dict((result.get("detection", {}).get("formal") or {}).get("timing") or {})
    timing.update(
        detection_end_to_end_seconds=result.get("detection", {}).get("seconds"),
        method_seconds=result.get("seconds"),
        repair_loop_seconds=result.get("repair_seconds"),
        deployment_seconds=dep.get("deployment_seconds") if trusted else None,
    )
    selected = {k: v for k, v in candidate.items() if k != "documents"}
    return {
        "case": record["case"],
        "run": str(run),
        "truth": expected,
        "method_result_hash": record["method_result_hash"],
        "rules_bundle_hash": method["_rules_bundle_hash"],
        "predictions": predictions(result.get("detection") or {}),
        "repair_entered": repair_entered(result),
        "candidate": selected,
        "deployment_status": status,
        "deployment_evidence_trusted": trusted,
        "pending": evaluation.get("pending") if trusted else None,
        "api_accepted": evaluation.get("api_accepted") if trusted else None,
        "quality_status": quality,
        "post_categories": final_categories,
        "original_quality_status": original_post.get("status", "missing_original_postcheck"),
        "original_categories": (
            original_post.get("category_ids")
            if original_post.get("status") == "scored" and isinstance(original_post.get("category_ids"), list)
            else None
        ),
        "original_postcheck_snapshot_hash": original_post.get("snapshot_hash"),
        "postcheck_snapshot_hash": post.get("snapshot_hash"),
        "timing": timing,
        "repair_calls": call_metrics(result),
        "retry_trajectory": retry_trajectory(
            result, docs, dep.get("retry_deployments") if trusted else None
        ),
    }


def format_report(report):
    def number(value, percent=False):
        if value is None:
            return "N/A"
        return f"{100 * value:.2f}%" if percent else f"{value:.4f}"

    lines = [
        "=== ConfDR Paper Metrics ===",
        "Rates use [0,1] in JSON; N/A means missing evidence or zero denominator.",
        "",
    ]
    tables = list(report["datasets"].items())
    if len(tables) > 1 and "detection" in report["overall"]:
        tables.append(("Overall", report["overall"]))
    for name, metrics in tables:
        lines += [
            f"--- {name} ({metrics['case_count']} cases) ---",
            "Detection: view | TP FP FN TN | P R F1 Acc | coverage | Avg Jaccard (scored/missing) | exact match",
        ]
        for view, m in metrics["detection"].items():
            lines.append(
                f"{view} | {m['tp']} {m['fp']} {m['fn']} {m['tn']} | "
                + " ".join(number(m[k]) for k in ("precision", "recall", "f1", "accuracy"))
                + f" | {number(m['coverage'], True)} | {number(m['avg_jaccard'])} ({m['jaccard_scored_cases']}/{m['jaccard_missing_cases']})"
                + f" | {number(m['exact_match'], True)}"
            )
            if m["unknown_pending"] or m["unknown_nonpending"] or m["unlabelled"]:
                lines.append(
                    f"  Unknown Pending/non-Pending={m['unknown_pending']}/{m['unknown_nonpending']}; unlabelled={m['unlabelled']}; "
                    f"recall/accuracy including abstentions={number(m['recall_including_abstentions'])}/{number(m['accuracy_including_abstentions'])}"
                )
        m = metrics["repair"]
        fixed = metrics["fixed_cohort_repair"]
        lines += [
            f"Repair: N={m['n']} | PSR={number(m['psr'], True)} | PR={number(m['pr'], True)} "
            f"(lower bound={number(m['pr_lower_bound'], True)}, missing={m['deployment_missing_cases']}) | "
            f"Resolved={number(m['resolved'], True)} ({m['resolved_categories']}/{m['detected_categories']}) | "
            f"Introduced={number(m['introduced'], True)} ({m['introduced_cases']}/{m['n']})",
            f"Fixed truth-Pending cohort: N={fixed['n']} | end-to-end success={number(fixed['success_rate'], True)} "
            f"(lower bound={number(fixed['success_rate_lower_bound'], True)}, missing={fixed['deployment_missing_cases']})",
            f"Fixed cohort Formal AIE={number(fixed['formal_average_introduced_errors'])} categories/case | "
            f"CVRR={number(fixed['configuration_violation_reduction_rate'], True)} | "
            f"paired quality coverage={fixed['paired_quality_cases']}/{fixed['n']}",
            f"Pending True/False/Unknown={m['pending_true']}/{m['pending_false']}/{m['pending_unknown']}",
            "Repair groups: group | N | PSR | PR | Pending True/False/Unknown | Resolved | Introduced",
        ]
        for group, g in metrics["repair_groups"].items():
            if g["n"]:
                lines.append(
                    f"{group} | {g['n']} | {number(g['psr'], True)} | {number(g['pr'], True)} | "
                    f"{g['pending_true']}/{g['pending_false']}/{g['pending_unknown']} | "
                    f"{number(g['resolved'], True)} | {number(g['introduced'], True)}"
                )
        lines.append("Groups: view | group | truth-positive | TP FP FN TN | recall incl. UNKNOWN | Jaccard")
        for view, groups in metrics["groups"].items():
            for group, g in groups.items():
                lines.append(
                    f"{view} | {group} | {g['truth_positive']} | "
                    f"{g['tp']} {g['fp']} {g['fn']} {g['tn']} | "
                    f"{number(g['recall_including_abstentions'])} | {number(g['jaccard'])}"
                )
        lines.append(
            "Retry trajectory: budget | N | PSR | accepted | KWOK PR (missing) | repair LLM seconds | "
            "total tokens | Resolved / Introduced"
        )
        for budget, curve in metrics["retry_budgets"].items():
            lines.append(
                f"{budget} | {curve['n']} | {number(curve['psr'])} | "
                f"{number(curve['accepted_rate'])} | "
                f"{number(curve['deployment_pr'], True)} ({curve['deployment_missing_cases']}) | "
                f"{number(curve['repair_llm_seconds']['average'])} | "
                f"{number(curve['repair_total_tokens']['average'])} | "
                f"{number(curve.get('resolved'), True)} / {number(curve.get('introduced'), True)}"
            )
        lines += [
            "Efficiency: metric | average | total | recorded/missing",
        ]
        for key in (
            "first_modeling_and_solving_seconds",
            "modeling_and_solving_seconds",
            "repair_seconds",
            "repair_prompt_tokens",
            "repair_completion_tokens",
            "repair_total_tokens",
        ):
            e = metrics["efficiency"][key]
            lines.append(
                f"{key} | {number(e['average'])} | {number(e['total'])} | {e['recorded_cases']}/{e['missing_cases']}"
            )
        lines.append("")
    lines += [
        "Binary metrics score explicit is_pending truth; category Micro-F1 is secondary (JSON).",
        "Repair time/tokens count repair LLM calls only. Detection time excludes rule validation and LLM.",
        "Groups are multi-label one-vs-rest; a case may contribute to multiple groups.",
        "Resolved/Introduced cover every repair case: detected categories versus the recheck of the deployed output;",
        "an output that is not deployed or not fully rechecked resolves nothing, and rejected outputs introduce an error.",
        "Legacy SI/AIE/CVRR remain in JSON only; SI/AIE mix human truth and postcheck labels, CVRR covers paired rechecks.",
        "Retry budgets are prefixes of one realized trajectory; KWOK PR requires each prefix's own deployment evidence.",
    ]
    return "\n".join(lines) + "\n"


def write_metrics(destination, report):
    write(Path(destination) / "paper-metrics.json", report)
    write(Path(destination) / "paper-metrics.txt", format_report(report))


def evaluate_metrics(runs, output=None, per_run=False):
    """Read-only analysis of method/evaluation artifacts; no cluster or model requests.

    Every run gets its own report inside its own directory, so a detection run is never
    read out of a file that also carries repair cases (and vice versa).  `output`, when
    given, additionally receives the combined report: all runs under `datasets` plus the
    case-weighted `overall` block.  A single run without `output` therefore writes exactly
    one file pair, in the run directory, as before.  `per_run` is what the CLI's
    `--per-run` passes: several runs, each writing only its own directory, no combined
    report anywhere.
    """
    paths = [Path(p).resolve() for p in runs]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Provide one or more distinct completed run directories")
    if len(paths) > 1 and output is None and not per_run:
        raise ValueError("Multiple runs require --output for the aggregated report")
    datasets, rule_hashes = {}, set()
    per_run = []
    for run in paths:
        method = read(run / "experiment.json")
        if method.get("stage") != "method_completed" or not method.get("finished"):
            raise ValueError("Metrics require a completed method stage")
        catalog = read(run / "rules-used.json")
        if digest(source_content(catalog)) != method.get("rules_hash"):
            raise ValueError("Frozen rules have changed")
        bundle_hash = digest(catalog)
        if method.get("rules_bundle_hash", bundle_hash) != bundle_hash:
            raise ValueError("Frozen rule validation material has changed")
        method["_rules_bundle_hash"] = bundle_hash
        rule_hashes.add(bundle_hash)
        records = method.get("records", [])
        if len({r["case"] for r in records}) != len(records):
            raise ValueError("Duplicate cases in run")
        rows = [_case_record(run, r, method) for r in records]
        metrics = summarize(rows)
        datasets[str(run)] = metrics
        per_run.append((run, metrics, rows))
    if len(rule_hashes) > 1:
        raise ValueError("Overall metrics require the same frozen rules and validation material across runs")
    for run, metrics, rows in per_run:
        write_metrics(
            run,
            {
                "schema_version": "paper-metrics-2",
                "datasets": {str(run): metrics},
                "overall": metrics,
                "cases": rows,
            },
        )
    report = {
        "schema_version": "paper-metrics-2",
        "datasets": datasets,
        "overall": summarize([row for _, _, rows in per_run for row in rows]),
        "cases": [row for _, _, rows in per_run for row in rows],
    }
    destination = Path(output).resolve() if output else None
    if destination is not None and destination not in paths:
        write_metrics(destination, report)
    return report
