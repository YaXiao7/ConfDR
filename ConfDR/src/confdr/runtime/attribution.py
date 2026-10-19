"""Explain conflict roles per claim without discarding the original core."""


def is_requirement(item: dict, requirement: str, legacy_rule_id: str) -> bool:
    """Match a constraint instance by its taxonomy requirement; fall back to the rule-v1 rule ID."""
    return requirement in (item.get("requirement_ids") or []) or item.get("rule_id") == legacy_rule_id


def is_path_selection(item: dict) -> bool:
    return is_requirement(item, "S3.path", "S3/S3.path-selection")


def attribute_conflicts(core: list[dict]) -> list[dict]:
    """Separate a path-selection obligation from the constraints blocking that path.

    This is evidence attribution, not a proof of a unique root cause. In particular,
    static incompatibility and an unavailable dynamic alternative remain visible.
    Never use another claim's evidence to suppress a path-selection finding.
    """
    result = []
    for item in core:
        claim = item.get("entities", {}).get("c")
        explaining = [
            other for other in core
            if claim and other.get("entities", {}).get("c") == claim
            and other.get("category_id") in {"S2", "S4"}
        ] if is_path_selection(item) else []
        result.append({
            "constraint_id": item.get("constraint_id"),
            "rule_id": item.get("rule_id"),
            "category_id": item.get("category_id"),
            "claim": claim,
            "role": "supporting_path_obligation" if explaining else "conflict_evidence",
            "explained_by": [other.get("constraint_id") or other["rule_id"] for other in explaining],
            "reason": (
                "The same claim's dynamic configuration or topology blocks the required storage path"
                if explaining else "Retained conflict evidence; not necessarily an independent root cause"
            ),
        })
    return result


def attributed_categories(core: list[dict]) -> list[str]:
    """Treat S3 path selection as a dependency when S2 or S4 explains the same claim.

    A path-selection constraint requires a claim to select a storage path. If the
    selected path is rejected by the dynamic-configuration (S2) or topology
    (S4) requirement, S3 participates in the UNSAT core but is not an
    independent binding failure. Static-compatibility and fixed-choice
    conflicts remain S3 findings.
    """
    return sorted({
        item["category_id"] for item in attribute_conflicts(core)
        if item["category_id"] and item["role"] == "conflict_evidence"
    })
