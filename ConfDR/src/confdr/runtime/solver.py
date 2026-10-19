"""Validated AST -> concrete constraints -> label skipping (or optional MUS analysis)."""

from __future__ import annotations

import copy
import math
import time

import z3

from confdr.runtime.attribution import attribute_conflicts, attributed_categories
from confdr.runtime.conflicts import enumerate_conflicts, skip_conflicts
from confdr.runtime.storage_diagnostics import storage_diagnostics
from confdr.shared.constraint_labels import constraint_labels
from confdr.shared.kubernetes import make_world
from confdr.shared.logic import SORTS, Entity, compile_expr, configure_vocabulary, load_facts
from confdr.shared.verification import ACCEPTED, verify_catalog

STORAGE_PREDICATES = {
    "claim_exists",
    "uses",
    "class_ok",
    "access_ok",
    "mode_ok",
    "capacity_ok",
    "volume_available",
    "has_fixed_volume",
    "fixed_to",
    "dynamic_available",
    "volume_node_ok",
    "dynamic_node_ok",
    "bind",
    "dynamic",
    "fixed_volume_exists",
    "storage_class_exists",
    "dynamic_eligible",
    "provisioner_configured",
    "provisioner_parameters_valid",
    "pv_selector_ok",
    "access_modes_supported",
}


def _uses_predicates(expr: object, names: set[str]) -> bool:
    if isinstance(expr, dict):
        return expr.get("call") in names or any(_uses_predicates(value, names) for value in expr.values())
    if isinstance(expr, list):
        return any(_uses_predicates(value, names) for value in expr)
    return False


def _uses_sort(expr: object, sort: str) -> bool:
    if isinstance(expr, dict):
        return expr.get("sort") == sort or any(_uses_sort(value, sort) for value in expr.values())
    if isinstance(expr, list):
        return any(_uses_sort(value, sort) for value in expr)
    return False


def _category_catalog(catalog: dict, category: dict) -> dict:
    """Build an independently revalidatable single-category catalog so an invalid category does not block others."""
    cat = category["id"]
    subset = copy.deepcopy(catalog)
    subset["taxonomy"]["categories"] = [copy.deepcopy(category)]
    subset["rules"] = [
        copy.deepcopy(rule) for rule in catalog.get("rules", []) if rule.get("category_id") == cat
    ]
    subset["knowledge"] = [
        copy.deepcopy(item) for item in catalog.get("knowledge", []) if cat in item.get("categories", [])
    ]
    subset["repair_examples"] = [
        copy.deepcopy(item)
        for item in catalog.get("repair_examples", [])
        if cat in item.get("category_ids", [])
    ]
    material = subset.get("verification_material", {})
    reference = material.get("reference", {}).get("categories", {}).get(cat)
    material["reference"] = {"categories": {cat: copy.deepcopy(reference)} if reference else {}}
    material["tests"] = [test for test in material.get("tests", []) if test.get("category_id") == cat]
    review = material.get("reviews", {}).get(cat)
    material["reviews"] = {cat: copy.deepcopy(review)} if review else {}
    return subset


def solve(
    case: dict,
    catalog: dict,
    *,
    timeout_ms: int = 5000,
    search_timeout_s: float = 0,
    conflict_method: str = "label_skipping",
    progress=None,
) -> dict:
    """Check all usable rules, then skip concrete core members to find further conflicts.

    Shared constraints are protected. Original UNSAT remains UNSAT after filtering.
    The optional MUS mode enumerates overlapping conflicts. Neither method proves
    completeness beyond the declared rules and observed features.
    """
    started = time.monotonic()
    result = {
        "status": "unknown",
        "solver_status": "not_run",
        "reason": "",
        "source": "generated_fol",
        "core": [],
        "cores": [],
        "initial_core": [],
        "core_search_complete": False,
        "formal_complete": False,
        "assignment": {},
        "missing_evidence": [],
        "unverified_categories": [],
        "skipped_rules": [],
        "category_ids": [],
        "category_results": {},
        "category_validation": {},
    }
    try:
        if (
            type(timeout_ms) is not int
            or timeout_ms <= 0
            or not math.isfinite(search_timeout_s)
            or search_timeout_s < 0
        ):
            raise ValueError("Solver timeout_ms must be positive and search_timeout_s must be nonnegative")
        configure_vocabulary(catalog.get("taxonomy", {}).get("predicates"))
        categories = catalog.get("taxonomy", {}).get("categories", [])
        if not isinstance(categories, list) or not categories:
            raise ValueError("Rule catalog is missing taxonomy.categories")
        if not isinstance(catalog.get("rules"), list):
            raise ValueError("Rule catalog is missing rules")
        if catalog.get("schema_version") != "2.0":
            raise ValueError("A 2.0 AST rule catalog is required; regenerate other formats from the taxonomy")
        category_ids = [category["id"] for category in categories]
        if any(not isinstance(cid, str) or not cid.strip() for cid in category_ids) or len(
            set(category_ids)
        ) != len(category_ids):
            raise ValueError("Category IDs must be unique nonempty strings")
        rule_ids = [rule["id"] for rule in catalog["rules"]]
        if any(not isinstance(rid, str) or not rid for rid in rule_ids) or len(set(rule_ids)) != len(
            rule_ids
        ):
            raise ValueError("Rule IDs must be unique nonempty strings")
        if any(rule.get("category_id") not in category_ids for rule in catalog["rules"]):
            raise ValueError("Rule references an undeclared category")

        modeling_started = time.monotonic()
        profile = catalog["taxonomy"].get("semantics_profile")
        world = make_world(case, profile=profile)
        load_facts(world, case.get("ExtraFacts", []), extra_only=True)
        case_bound = tuple(len(world.domains[sort]) for sort in SORTS)
        world_seconds = time.monotonic() - modeling_started
        validation_started = time.monotonic()
        overall = verify_catalog(catalog, case_bound=case_bound, timeout_ms=timeout_ms)
        result["rule_validation"] = {
            key: overall.get(key)
            for key in ("status", "scope", "catalog_hash", "reference_hash", "bounds", "errors")
            if key in overall
        }

        accepted: set[str] = set()
        rules_by_category: dict[str, list[dict]] = {}
        if overall.get("status") in ACCEPTED:
            for category in categories:
                cat = category["id"]
                accepted.add(cat)
                rules_by_category[cat] = [rule for rule in catalog["rules"] if rule["category_id"] == cat]
                result["category_validation"][cat] = {"status": overall["status"], "source": "catalog"}
        else:
            for category in categories:
                cat = category["id"]
                subset = _category_catalog(catalog, category)
                validation = verify_catalog(subset, case_bound=case_bound, timeout_ms=timeout_ms)
                result["category_validation"][cat] = validation
                if validation.get("status") in ACCEPTED:
                    accepted.add(cat)
                    rules_by_category[cat] = subset["rules"]
                else:
                    reason = validation.get("errors") or validation.get("status", "unverified")
                    for rule in subset.get("rules", []) or [{"id": None, "category_id": cat}]:
                        result["skipped_rules"].append(
                            {"rule_id": rule.get("id"), "category_id": cat, "reason": reason}
                        )

        validation_seconds = time.monotonic() - validation_started
        grounding_started = time.monotonic()
        formulas: list = []
        evidence: list[dict] = []

        def track(formula, details):
            simplified = z3.simplify(formula)
            if z3.is_true(simplified):
                return
            evidence.append({"constraint_id": f"constraint_{len(evidence)}", **details})
            formulas.append(simplified)

        for pod, axiom in zip(world.domains["Pod"], world.assignment_axioms(), strict=True):
            track(
                axiom,
                {
                    "rule_id": "ASSIGN-ONE",
                    "category_id": None,
                    "title": "Each target Pod must be assigned to exactly one node",
                    "clause": "root",
                    "entities": {"p": pod},
                    "fact_refs": [],
                },
            )
        for i, axiom in enumerate(world.support_axioms(profile)):
            track(
                axiom,
                {
                    "rule_id": f"SHARED/{i}",
                    "category_id": None,
                    "title": "Scheduling order and storage path uniqueness",
                    "clause": "root",
                    "entities": {},
                    "fact_refs": [],
                },
            )
        if profile in {"scheduling-v1", "scheduling-v2"}:
            for p in world.domains["Pod"]:
                for n in world.domains["Node"]:
                    if not world.values["node_name_ok", p, n]:
                        track(
                            z3.Not(world.call("assign", [Entity("Pod", p), Entity("Node", n)])),
                            {
                                "rule_id": "SHARED/node-name",
                                "category_id": None,
                                "title": "Respect explicit nodeName",
                                "clause": "root",
                                "entities": {"p": p, "n": n},
                                "fact_refs": [f"node_name_ok/{p}/{n}"],
                            },
                        )

        def ground(expr, env, rule, clause="root"):
            # Split structures only when semantics and alternative relationships are preserved.
            world.expanded += 1
            if world.expanded > world.max_expanded:
                raise ValueError("Finite-domain expansion exceeds the budget")
            if expr.get("quantifier") == "forall":
                for name in world.domains[expr["sort"]]:
                    ground(
                        expr["body"],
                        {**env, expr["variable"]: Entity(expr["sort"], name)},
                        rule,
                        clause,
                    )
            elif expr.get("op") == "and":
                for index, arg in enumerate(expr["args"]):
                    ground(arg, env, rule, f"{clause}.{index}")
            elif expr.get("op") == "implies" and expr["args"][1].get("op") == "and":
                antecedent, consequent = expr["args"]
                for index, arg in enumerate(consequent["args"]):
                    ground(
                        {"op": "implies", "args": [antecedent, arg]},
                        env,
                        rule,
                        f"{clause}.then.{index}",
                    )
            else:
                world.reads.clear()
                formula = compile_expr(expr, world, env)
                track(
                    formula,
                    {
                        "rule_id": rule["id"],
                        "category_id": rule["category_id"],
                        # Requirement IDs are fixed in the taxonomy; generated rule IDs may change.
                        "requirement_ids": list(rule.get("requirement_ids") or []),
                        "title": rule["title"],
                        "clause": clause,
                        "entities": {key: value.name for key, value in env.items()},
                        "fact_refs": sorted(world.reads),
                    },
                )

        cluster = case.get("ClusterState", {})
        has_claims = any(
            (volume.get("persistentVolumeClaim") or {}).get("claimName")
            for pod in case.get("WaitingDeployPods", {}).values()
            for volume in pod.get("volumes") or []
        )
        missing_inventories = sorted({"PVCs", "PVs", "StorageClasses"} - set(cluster)) if has_claims else []
        if missing_inventories:
            result["missing_evidence"].extend(f"ClusterState/{name}" for name in missing_inventories)

        for category in categories:
            cat = category["id"]
            if cat not in accepted:
                continue
            for rule in rules_by_category.get(cat, []):
                formula_count, evidence_count = len(formulas), len(evidence)
                previous_missing = set(world.missing)
                previous_symbols = dict(world.symbols)
                assumptions_count = len(world.assumptions)
                try:
                    for sort, missing in world.domain_missing.items():
                        if missing and _uses_sort(rule["formula"], sort):
                            result["missing_evidence"].extend(missing)
                            raise ValueError(f"Incomplete {sort} inventory; cannot use an empty domain")
                    if "PVs" not in cluster and _uses_sort(rule["formula"], "Volume"):
                        raise ValueError("missing_PV_inventory_cannot_treat_as_empty_domain")
                    if missing_inventories and _uses_predicates(rule["formula"], STORAGE_PREDICATES):
                        raise ValueError("incomplete_storage_inventory: " + ", ".join(missing_inventories))
                    world.expanded = 0
                    ground(rule["formula"], {}, rule)
                except (ValueError, TypeError, KeyError, AttributeError, z3.Z3Exception) as exc:
                    del formulas[formula_count:]
                    del evidence[evidence_count:]
                    world.missing = previous_missing
                    world.symbols = previous_symbols
                    del world.assumptions[assumptions_count:]
                    result["skipped_rules"].append(
                        {"rule_id": rule.get("id"), "category_id": cat, "reason": str(exc)}
                    )

        display_labels = constraint_labels(evidence)
        for item in evidence:
            item["constraint_label"] = display_labels[item["constraint_id"]]
        seen_labels = set()

        def emit_round(row):
            labels = sorted({evidence[i]["category_id"] for i in row["core"] if evidence[i]["category_id"]})
            row["category_ids"] = labels
            row["new_category_ids"] = sorted(set(labels) - seen_labels)
            seen_labels.update(labels)
            if progress:
                phase = "Initial" if row["round"] == 1 else f"Skip {row['round'] - 1}"
                progress(
                    f"  {phase}: {row['status'].upper()} | labels={labels} | new={row['new_category_ids']}"
                )

        modeling_seconds = world_seconds + time.monotonic() - grounding_started
        if conflict_method == "label_skipping":
            search = skip_conflicts(
                formulas,
                world.assumptions,
                protected=[i for i, item in enumerate(evidence) if item["category_id"] is None],
                timeout_ms=timeout_ms,
                search_timeout_s=search_timeout_s,
                on_round=emit_round,
            )
        elif conflict_method == "mus":
            search = enumerate_conflicts(
                formulas,
                world.assumptions,
                protected=[i for i, item in enumerate(evidence) if item["category_id"] is None],
                timeout_ms=timeout_ms,
                search_timeout_s=search_timeout_s,
            )
        else:
            raise ValueError("Unknown conflict method")
        result["timing"] = {
            "rule_validation_seconds": round(validation_seconds, 6),
            "modeling_seconds": round(modeling_seconds, 6),
            "solver_search_seconds": search["seconds"],
            "modeling_and_solving_seconds": round(modeling_seconds + search["seconds"], 6),
            "first_modeling_and_solving_seconds": (
                round(modeling_seconds + search["first_check_seconds"], 6)
                if search.get("first_check_seconds") is not None
                else None
            ),
        }
        witness = search.pop("model")
        result["search"] = {
            key: value
            for key, value in search.items()
            if key not in {"cores", "initial_core", "core_contexts", "unminimized_cores"}
        }
        result["initial_core"] = [evidence[index] for index in search["initial_core"]]
        result["solver_status"] = search["solver_status"]
        result["terminal_status"] = search["terminal_status"]
        result["conflict_method"] = search["algorithm"]
        result["cores"] = [[evidence[index] for index in core] for core in search["cores"]]
        result["core_contexts"] = [
            [evidence[index] for index in core] for core in search.get("core_contexts", [])
        ]
        result["unminimized_cores"] = [
            [evidence[index] for index in core] for core in search.get("unminimized_cores", [])
        ]
        result["minimality_proved"] = search.get("minimality_proved", False)
        result["minimality_scope"] = search.get("minimality_scope")
        members = sorted(
            {
                index
                for core in (
                    search["cores"] + search.get("core_contexts", []) + search.get("unminimized_cores", [])
                )
                for index in core
            }
        )
        result["core"] = [evidence[index] for index in members]
        result["category_ids"] = attributed_categories(result["core"])
        result["conflict_attribution"] = attribute_conflicts(result["core"])
        result["storage_diagnostics"] = storage_diagnostics(case, world, result["core"])
        result["core_category_ids"] = sorted(
            {item["category_id"] for item in result["core"] if item["category_id"] is not None}
        )
        result["core_search_complete"] = search["complete"]
        result["all_conflicts_enumerated"] = bool(search.get("exhaustive"))
        result["constraints_count"] = len(formulas)
        all_categories = {category["id"] for category in categories}
        result["unverified_categories"] = sorted(all_categories - accepted)
        result["missing_evidence"] = sorted(set(result["missing_evidence"]) | world.missing)
        unsupported = case.get("_unsupported_manifest_features", [])
        result["unsupported_manifest_features"] = unsupported
        result["formal_complete"] = bool(
            world.domains["Pod"]
            and search["complete"]
            and not search.get("base_inconsistent")
            and (search["solver_status"] == "sat" or search.get("exhaustive"))
            and accepted == all_categories
            and not result["skipped_rules"]
            and not result["missing_evidence"]
            and not unsupported
        )
        if search["solver_status"] == "unsat":
            result.update(status="unsat", reason=search["reason"])
        elif search["solver_status"] == "sat" and result["formal_complete"]:
            result.update(status="sat", reason="constraints_satisfiable")
        else:
            reason = search["reason"]
            if not world.domains["Pod"]:
                reason = "no_target_pods"
            elif result["unverified_categories"]:
                reason = "rule_catalog_not_verified"
            elif result["missing_evidence"]:
                reason = "incomplete_snapshot"
            elif unsupported:
                reason = "unsupported_manifest_features"
            result.update(status="unknown", reason=reason)
        result["category_results"] = {
            cat: (
                "conflict_found"
                if cat in result["category_ids"]
                else "supporting_constraint_only"
                if cat in result["core_category_ids"]
                else "no_conflict_in_checked_model"
                if result["formal_complete"]
                else "unknown"
            )
            for cat in sorted(all_categories)
        }
        if witness is not None and world.domains["Node"]:
            if profile in {"scheduling-v1", "scheduling-v2"}:
                result["scheduling_order"] = sorted(
                    world.domains["Pod"],
                    key=lambda p: witness.eval(
                        world.call("schedule_rank", [Entity("Pod", p)]), model_completion=True
                    ).as_long(),
                )
            result["assignment"] = {
                pod: next(
                    (
                        node
                        for node in world.domains["Node"]
                        if z3.is_true(
                            witness.eval(
                                world.call("assign", [Entity("Pod", pod), Entity("Node", node)]),
                                model_completion=True,
                            )
                        )
                    ),
                    None,
                )
                for pod in world.domains["Pod"]
            }
    except (ValueError, TypeError, KeyError, AttributeError, z3.Z3Exception) as exc:
        result.update(status="unknown", reason=f"formal_input_or_compile_error: {exc}")
    finally:
        result["seconds"] = round(time.monotonic() - started, 4)
    return result
