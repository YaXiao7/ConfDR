"""First-check, aggregate Z3 and combined predictions; truth is used only after detection."""

from confdr.runtime.attribution import attributed_categories
from confdr.runtime.evaluation import compare_labels, multilabel_metrics
from confdr.shared.constraint_labels import constraint_labels


def conflict_views(detection: dict) -> dict:
    formal = detection.get("formal") or {}
    initial = formal.get("initial_core")
    return {
        "single_round": {
            "solver_status": formal.get("solver_status", "not_run"),
            "recorded": initial is not None,
            "minimality_proved": False,
            "constraint_ids": [c["constraint_id"] for c in initial or []],
        },
        "all_conflicts": {
            "algorithm": formal.get("conflict_method", "MUS_enumeration"),
            "exhaustive": formal.get(
                "all_conflicts_enumerated",
                formal.get("conflict_method") != "label_skipping"
                and formal.get("core_search_complete", False),
            ),
            "minimality_proved": formal.get("minimality_proved", False),
            "minimality_scope": formal.get("minimality_scope"),
            "core_contexts": [[c["constraint_id"] for c in core] for core in formal.get("core_contexts", [])],
            "unminimized_cores": [
                [c["constraint_id"] for c in core] for core in formal.get("unminimized_cores", [])
            ],
            "skipping_rounds": formal.get("search", {}).get("rounds", []),
            "rounds": [
                {"round": i, "constraint_ids": [c["constraint_id"] for c in core]}
                for i, core in enumerate(formal.get("cores", []), 1)
            ],
            "unique_constraint_ids": [c["constraint_id"] for c in formal.get("core", [])],
            "search_complete": formal.get("core_search_complete", False),
            "reason": formal.get("reason"),
        },
    }


def _formal_prediction(formal, core):
    status = {"unsat": "fault", "sat": "healthy"}.get(formal.get("status"), "unknown")
    return {
        "status": status,
        "category_ids": attributed_categories(core),
    }


def compare_detection(expected: dict, detection: dict) -> dict:
    formal = detection.get("formal") or {}
    first = (
        compare_labels(expected, _formal_prediction(formal, formal["initial_core"]))
        if "initial_core" in formal
        else {
            **compare_labels(expected, {"status": "unknown", "category_ids": []}),
            "reason": "first_core_not_recorded",
        }
    )
    return {
        "single_round": first,
        "all_conflicts": {
            **compare_labels(expected, _formal_prediction(formal, formal.get("core", []))),
            "algorithm": formal.get("conflict_method", "MUS_enumeration"),
        },
        "combined_detection": compare_labels(expected, detection),
    }


def evaluate_detection_views(records: list[dict]) -> dict:
    views = {}
    for name in ("single_round", "all_conflicts", "combined_detection"):
        adapted = []
        for r in records:
            d = r.get("result", {}).get("detection", {})
            formal = d.get("formal") or {}
            predicted = (
                d
                if name == "combined_detection"
                else _formal_prediction(
                    formal, formal.get("initial_core" if name == "single_round" else "core", [])
                )
            )
            if name == "single_round" and "initial_core" not in formal:
                predicted = {"status": "unknown", "category_ids": []}
            adapted.append({"truth": r.get("truth", {}), "result": {"detection": predicted}})
        views[name] = {
            "available_cases": len(adapted),
            "unrecorded_cases": sum(
                "initial_core" not in (r.get("result", {}).get("detection", {}).get("formal") or {})
                for r in records
            ),
            **multilabel_metrics(adapted),
        }
    return views


def _display_labels(formal):
    # Both text reports use the same mapping, including when rendering historical JSON.
    items = {c["constraint_id"]: c for c in [*formal.get("initial_core", []), *formal.get("core", [])]}
    return constraint_labels(items.values())


def _constraint_line(item, labels):
    return "   " + labels[item["constraint_id"]]


def format_single_round(case_id: str, detection: dict) -> str:
    formal = detection.get("formal") or {}
    labels = _display_labels(formal)
    lines = [f"=== Single-Round Verification Result for Case: {case_id} ===", ""]
    if "initial_core" not in formal:
        lines.append(
            "First Z3 core was not recorded."
            if formal.get("solver_status")
            else "NOT RUN: input processing failed; no first Z3 result."
        )
    else:
        core = formal["initial_core"]
        status = formal.get("solver_status", "not_run")
        if status == "unsat":
            lines.append(f"UNSAT: {len(core)} constraints in unsat core.")
        elif status == "sat":
            lines.append("SAT: original constraints are satisfiable.")
        elif status == "unknown":
            lines.append("UNKNOWN: solver could not determine satisfiability.")
        else:
            lines.append("NOT RUN: input or compilation error.")
        lines.extend(_constraint_line(c, labels) for c in core)
        lines.append(
            "Category labels: " + str(sorted({c["category_id"] for c in core if c.get("category_id")}))
        )
        if attributed_categories(core) != sorted({c["category_id"] for c in core if c.get("category_id")}):
            lines.append("Attributed fault categories: " + str(attributed_categories(core)))
    return "\n".join(lines)


def format_multiple_rounds(case_id: str, detection: dict) -> str:
    formal = detection.get("formal") or {}
    labels = _display_labels(formal)
    lines = [f"=== Verification Result for Case: {case_id} ===", ""]
    evidence = {c["constraint_id"]: c for c in formal.get("core", [])}
    mus_mode = formal.get("conflict_method") == "MUS_enumeration"
    rows = formal.get("search", {}).get("rounds", [])
    lines.append("Method: " + ("MUS enumeration" if mus_mode else "label skipping"))
    if mus_mode:
        contexts = formal.get("core_contexts", [])
        for index, core in enumerate(formal.get("cores", [])):
            lines.append(f"--- MUS {index + 1} ---")
            proved = formal.get("minimality_proved", False)
            kind = "subset-minimal constraints" if proved else "constraints; minimality not recorded"
            lines.append(f"UNSAT: {len(core)} {kind} relative to fixed background.")
            lines.extend(_constraint_line(c, labels) for c in core)
            if index < len(contexts) and contexts[index]:
                lines.append("Supporting fixed background labels:")
                lines.extend(_constraint_line(c, labels) for c in contexts[index])
            lines.append(
                "Category labels: " + str(sorted({c["category_id"] for c in core if c.get("category_id")}))
            )
        for index, core in enumerate(formal.get("unminimized_cores", []), 1):
            lines.append(f"--- Unminimized core {index} ---")
            lines.append("UNSAT evidence retained; minimality has not been proved.")
            lines.extend(_constraint_line(c, labels) for c in core)
        if formal.get("solver_status") == "sat":
            lines.append("SAT: original constraints are satisfiable; no MUS exists in this model.")
        elif formal.get("solver_status") == "unknown":
            lines.append("UNKNOWN: solver could not determine satisfiability.")
        complete = formal.get("all_conflicts_enumerated", False)
        lines.append(
            f"MUS enumeration complete: {'yes' if complete else 'no'}; verified MUSes: {len(formal.get('cores', []))}"
        )
    for row in rows:
        lines.append(f"--- Round {row['round']} ---")
        if row["status"] == "unsat":
            lines.append(f"UNSAT in round {row['round']}: {len(row['core'])} constraints in core.")
            lines.extend(_constraint_line(evidence[f"constraint_{i}"], labels) for i in row["core"])
            lines.append(
                f"Category labels: {row.get('category_ids', [])}; new categories: {row.get('new_category_ids', [])}"
            )
            if row.get("removed"):
                lines.append(
                    "Skipped for next round: " + str([labels[f"constraint_{i}"] for i in row["removed"]])
                )
        elif row["status"] == "sat":
            lines.append(
                "SAT: System became satisfiable after filtering conflicting constraints."
                if row["round"] > 1
                else "SAT: original constraints are satisfiable."
            )
        else:
            lines.append("UNKNOWN: solver could not determine satisfiability.")
    if not rows and not mus_mode:
        lines.append("No label-skipping rounds recorded.")
    lines += [
        "",
        f"Total number of unique conflicting constraints detected: {len(evidence)}",
        "Union of all unsatisfiable constraint labels:",
    ]
    lines.extend(_constraint_line(c, labels) for c in evidence.values())
    lines.append(f"Union of category labels: {formal.get('core_category_ids', formal.get('category_ids', []))}")
    if formal.get("core_category_ids") != formal.get("category_ids"):
        lines.append(f"Attributed fault categories: {formal.get('category_ids', [])}")
    for storage in formal.get("storage_diagnostics", []):
        if "dynamic" not in storage:
            continue
        if storage["core_category_ids"] or storage["dynamic"]["required_evidence"]:
            lines.append(
                f"Storage {storage['claim']}: static={storage['static']['status']}, "
                f"dynamic={storage['dynamic']['status']}; {storage['attribution']}"
            )
            for request in storage["dynamic"]["required_evidence"]:
                lines.append(f"Missing evidence: {request['fact']} ({request['provisioner']})")
    lines.append(f"Overall formal result: {formal.get('status', 'not_run').upper()}")
    if formal.get("solver_status") == "unsat":
        lines.append(
            "Original constraints remain UNSAT; enumeration does not repair the configuration."
            if mus_mode
            else "Original constraints remain UNSAT; filtered SAT does not mean repaired."
        )
    if not formal.get("core_search_complete"):
        lines.append(f"Search incomplete: {formal.get('reason', detection.get('error') or 'not_run')}")
    elif formal.get("status") == "unknown":
        lines.append(f"Reason: {formal.get('reason')}")
    lines.append(
        "Scope: subset-minimal category constraints relative to fixed background; completeness applies only to the checked model."
        if mus_mode
        else "Scope: detected conflict union; cores are not necessarily minimal and label skipping is not exhaustive MUS enumeration."
    )
    if detection.get("llm_skipped_reason"):
        lines.append(f"LLM auxiliary detection: skipped ({detection['llm_skipped_reason']})")
    elif detection.get("llm_trigger"):
        auxiliary = detection.get("llm") or {}
        labels = sorted({c for finding in auxiliary.get("findings", []) for c in finding["category_ids"]})
        lines.append(
            f"LLM auxiliary detection: triggered by UNKNOWN; result={auxiliary.get('status', 'failed')}; labels={labels}"
        )
    if detection.get("llm_trigger"):
        lines.append(
            f"Final detection: {detection.get('status', 'unknown')} | labels={detection.get('category_ids', [])}"
        )
    if detection.get("error"):
        lines.append(f"Error: {detection['error']}")
    return "\n".join(lines)


def format_conflicts(case_id: str, detection: dict) -> str:
    return format_single_round(case_id, detection) + "\n\n" + format_multiple_rounds(case_id, detection)


def format_comparison(comparison: dict) -> str:
    if not comparison["scored"]:
        return "Labels: unscored (" + comparison["reason"] + ")"
    return (
        f"Labels: truth={comparison['truth']}; predicted={comparison['predicted']}; "
        f"TP={comparison['tp']}; FP={comparison['fp']}; FN={comparison['fn']}; "
        f"exact_match={comparison['exact_match']}"
    )
