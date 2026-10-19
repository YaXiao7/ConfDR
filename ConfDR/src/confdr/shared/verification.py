"""Automatic taxonomy-to-FOL extraction, finite-domain semantic validation and counterexample-guided correction."""

from __future__ import annotations

import copy
import json
from functools import lru_cache

import z3

from confdr.shared.contracts import verification_base
from confdr.shared.io import digest
from confdr.shared.logic import (
    DECISIONS,
    SORTS,
    Entity,
    World,
    compile_expr,
    configure_vocabulary,
    expression_schema,
    semantics_version,
    typecheck,
    vocabulary,
)
from confdr.shared.review import REVIEW_PROTOCOL, review_passed

ACCEPTED = {"bounded_verified", "empirically_validated"}

# Check empty domains, single elements and competition among multiple objects. Report bounds explicitly; never claim an unbounded proof.
BOUNDS = [
    (0, 1, 0, 0, 0, 0, 0),
    (1, 0, 1, 0, 1, 1, 1),
    (1, 1, 1, 1, 1, 1, 1),
    (2, 2, 2, 2, 2, 2, 2),
    (3, 2, 2, 2, 2, 2, 2),
]


def generation_schema() -> dict:
    properties = {
        "id": {"type": "string"},
        "category_id": {"type": "string"},
        "title": {"type": "string"},
        "formula": {"$ref": "#/$defs/expression"},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rules"],
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [key for key in properties if key != "requirement_ids"],
                    "properties": properties,
                },
            }
        },
        "$defs": {"expression": expression_schema()},
    }


def check_structure(catalog: dict, knowledge: list[dict]) -> None:
    configure_vocabulary(catalog.get("taxonomy", {}).get("predicates"))
    if catalog.get("taxonomy", {}).get("semantics_profile") not in {None, "scheduling-v1", "scheduling-v2"}:
        raise ValueError("Unknown semantics_profile")
    if catalog.get("taxonomy", {}).get("language") == "en" and any(
        "\u4e00" <= char <= "\u9fff" for item in vocabulary() for char in item["meaning"]
    ):
        raise ValueError("Predicate meanings must be in English")
    if catalog.get("schema_version") != "2.0":
        raise ValueError(
            "A v2 AST rule catalog is required; legacy builder catalogs cannot be executed directly"
        )
    categories = catalog.get("taxonomy", {}).get("categories", [])
    category_ids = {item["id"] for item in categories}
    if not categories or len(category_ids) != len(categories):
        raise ValueError("taxonomy must contain unique categories")
    rules = catalog.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
        raise ValueError("The number of rules must be between 1 and 100")
    seen, covered = set(), set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {
            "id",
            "category_id",
            "title",
            "formula",
            "source_ids",
            "requirement_ids",
            "fol",
        }:
            raise ValueError("Rule contains invalid fields")
        if not isinstance(rule["id"], str) or not rule["id"] or rule["id"] in seen:
            raise ValueError("Rule IDs must be nonempty and unique")
        cat = rule["category_id"]
        if cat not in category_ids:
            raise ValueError(f"Unknown category: {cat}")
        allowed_sources = {x["id"] for x in knowledge if cat in x["categories"]}
        if not rule.get("source_ids") or not set(rule["source_ids"]) <= allowed_sources:
            raise ValueError(f"Rule {rule['id']} is missing a valid source for its category")
        if not isinstance(rule.get("title"), str) or not rule["title"].strip():
            raise ValueError("Rule is missing a title")
        if catalog["taxonomy"].get("language") == "en" and any(
            "\u4e00" <= char <= "\u9fff" for char in rule["id"] + rule["title"]
        ):
            raise ValueError("Rule IDs and titles must be in English")
        typecheck(rule["formula"])
        seen.add(rule["id"])
        covered.add(cat)
    if covered != category_ids:
        raise ValueError(f"Uncovered categories: {sorted(category_ids - covered)}")
    from confdr.shared.requirements import check_coverage

    for category in categories:
        check_coverage(category, [r for r in rules if r["category_id"] == category["id"]])


def verify_catalog(
    catalog: dict,
    contracts: dict | None = None,
    knowledge: list[dict] | None = None,
    *,
    timeout_ms: int = 3000,
    case_bound: tuple[int, ...] | None = None,
) -> dict:
    material = catalog.get("verification_material", {})
    auto_reference = contracts is None and material.get("origin") == "blind_llm"
    contracts = contracts if contracts is not None else material.get("reference", {})
    knowledge = knowledge if knowledge is not None else catalog.get("knowledge", [])
    configure_vocabulary(catalog.get("taxonomy", {}).get("predicates"))
    # Key the validation cache by actual formulas, reference contracts and source content; do not trust persisted validation fields.
    material = {k: catalog.get(k) for k in ("schema_version", "taxonomy", "rules")}
    material["semantics_version"] = semantics_version(catalog.get("taxonomy", {}).get("semantics_profile"))
    report = copy.deepcopy(
        _verify_cached(
            json.dumps(material, sort_keys=True),
            json.dumps(contracts, sort_keys=True),
            json.dumps(knowledge, sort_keys=True),
            timeout_ms,
            case_bound,
        )
    )

    if auto_reference and report["status"] == "bounded_verified":
        report["scope"] = "bounded_agreement_with_independent_llm_plus_tests"
        report["natural_language_correctness_proved"] = False
        report["assurance"] = verify_semantics({**catalog, "knowledge": knowledge}, timeout_ms=timeout_ms)
        report["status"] = "empirically_validated" if report["assurance"]["passed"] else "unverified"
    categories = catalog.get("taxonomy", {}).get("categories", [])
    if len(categories) > 1 and report["status"] not in ACCEPTED:
        report["category_validation"] = {}
        for category in categories:
            cat = category["id"]
            subset = {
                **catalog,
                "taxonomy": {**catalog["taxonomy"], "categories": [category]},
                "rules": [r for r in catalog.get("rules", []) if r.get("category_id") == cat],
            }
            checked = verify_catalog(
                subset,
                contracts=None if auto_reference else contracts,
                knowledge=knowledge,
                timeout_ms=timeout_ms,
                case_bound=case_bound,
            )
            report["category_validation"][cat] = checked
        report["category_status"] = {
            cat: checked["status"] for cat, checked in report["category_validation"].items()
        }
    else:
        report["category_status"] = {
            item["category_id"]: item["status"] for item in report.get("categories", [])
        }
        for item in report.get("assurance", {}).get("categories", []):
            passed = (
                item["review_valid_for_content"]
                and item.get("reference_review_valid_for_content", True)
                and item["scenarios"]["passed"]
            )
            report["category_status"][item["category_id"]] = (
                "empirically_validated" if passed else "unverified"
            )
    return report


@lru_cache(maxsize=32)
def _verify_cached(
    catalog_json: str,
    contracts_json: str,
    knowledge_json: str,
    timeout_ms: int,
    case_bound: tuple[int, ...] | None,
) -> dict:
    catalog, contracts, knowledge = map(json.loads, (catalog_json, contracts_json, knowledge_json))
    configure_vocabulary(catalog.get("taxonomy", {}).get("predicates"))
    bounds = BOUNDS + ([case_bound] if case_bound is not None and case_bound not in BOUNDS else [])
    report = {
        "status": "rejected",
        "scope": "bounded_equivalence_to_reference",
        "unbounded_theorem_proved": False,
        "reference_hash": digest(contracts),
        "catalog_hash": digest(catalog),
        "natural_language_correctness_proved": False,
        "bounds": [dict(zip(SORTS, b, strict=True)) for b in bounds],
        "categories": [],
        "errors": [],
    }
    if timeout_ms <= 0:
        report["errors"] = ["Validation timeout_ms must be positive"]
        return report
    try:
        check_structure(catalog, knowledge)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        report["errors"] = [str(exc)]
        return report
    for category in catalog["taxonomy"]["categories"]:
        cat = category["id"]
        reference = contracts.get("categories", {}).get(cat)
        if not reference:
            report["categories"].append(
                {
                    "category_id": cat,
                    "status": "unverified",
                    "reason": "Cannot establish semantic agreement without an independent reference contract",
                }
            )
            continue
        generated = [r["formula"] for r in catalog["rules"] if r["category_id"] == cat]
        result = {"category_id": cat, "status": "bounded_verified", "checks": 0, "counterexamples": []}
        try:
            check_structure(
                {
                    **catalog,
                    "taxonomy": {**catalog["taxonomy"], "categories": [category]},
                    "rules": reference,
                },
                knowledge,
            )
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            result.update(status="rejected", failure_stage="reference_structure", reason=str(exc))
            report["categories"].append(result)
            continue
        for bound in bounds:
            world = World(
                {s: [f"{s}{i}" for i in range(n)] for s, n in zip(SORTS, bound, strict=True)}, symbolic=True
            )
            try:
                candidate = z3.And([compile_expr(x, world) for x in generated])
                expected = z3.And([compile_expr(x["formula"], world) for x in reference])
            except (ValueError, TypeError, KeyError, RecursionError, z3.Z3Exception) as exc:
                result.update(status="rejected", reason=str(exc))
                break
            axioms = world.assignment_axioms() + world.support_axioms(
                catalog["taxonomy"].get("semantics_profile")
            )
            for direction, formula in [
                ("candidate_not_reference", z3.And(candidate, z3.Not(expected))),
                ("reference_not_candidate", z3.And(expected, z3.Not(candidate))),
            ]:
                solver = z3.Solver()
                solver.set(timeout=timeout_ms)
                solver.add(*axioms, *world.assumptions, formula)
                status = solver.check()
                result["checks"] += 1
                if status == z3.unknown and result["status"] != "rejected":
                    result.update(status="unverified", reason=solver.reason_unknown())
                elif status == z3.sat:
                    model = solver.model()
                    result["status"] = "rejected"
                    result["counterexamples"].append(
                        {
                            "direction": direction,
                            "domains": world.domains,
                            "facts": {
                                "/".join(k): str(model.eval(v, model_completion=True))
                                for k, v in world.symbols.items()
                            },
                        }
                    )
            if result["status"] != "bounded_verified":
                break
        report["categories"].append(result)
    statuses = {x["status"] for x in report["categories"]}
    report["status"] = (
        "bounded_verified"
        if statuses == {"bounded_verified"}
        else "rejected"
        if "rejected" in statuses
        else "unverified"
    )
    return report


def verify_scenarios(catalog: dict, category_id: str, *, timeout_ms: int) -> dict:
    """Test labels come from independent specifications hidden from the candidate; results do not prove natural-language equivalence."""
    from confdr.shared.logic import load_facts, vocabulary

    tests = [
        t
        for t in catalog.get("verification_material", {}).get("tests", [])
        if t.get("category_id") == category_id
    ]
    formulas = [r["formula"] for r in catalog["rules"] if r["category_id"] == category_id]
    result = {"passed": False, "checks": [], "errors": [], "killed_mutants": 0}
    if len(tests) < 4 or any(sum(t.get("expected") == s for t in tests) < 2 for s in ("sat", "unsat")):
        result["errors"].append("Each category requires at least two healthy and two faulty blind tests")
        return result
    from confdr.shared.requirements import check_test_coverage

    category = next((c for c in catalog["taxonomy"].get("categories", []) if c["id"] == category_id), {})
    try:
        check_test_coverage(category, tests)
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result
    seen = set()
    discriminated_rules = set()
    for test in tests:
        try:
            if not isinstance(test.get("description"), str) or not test["description"].strip():
                raise ValueError("Test is missing an independent natural-language description")
            if test["id"] in seen:
                raise ValueError("Duplicate test ID")
            seen.add(test["id"])
            domains = {**{s: [] for s in SORTS}, **test["domains"]}
            if set(domains) != set(SORTS) or any(
                not isinstance(v, list)
                or len(v) > 8
                or any(not isinstance(x, str) or not x for x in v)
                or len(set(v)) != len(v)
                for v in domains.values()
            ):
                raise ValueError("Test domains are invalid or exceed limits")
            if not domains["Pod"] or not domains["Node"]:
                raise ValueError("Fault labels must not be manufactured using an empty scheduling domain")
            world = World(domains)
            load_facts(world, test["facts"])
            decision_constraints = []
            seen_decisions = set()
            decisions = test.get("decisions", [])
            if not isinstance(decisions, list) or len(decisions) > 100000:
                raise ValueError("Scenario decisions must be a bounded list")
            signatures = {item["name"]: item for item in vocabulary()}
            for decision in decisions:
                if not isinstance(decision, dict) or set(decision) != {"predicate", "arguments", "value"}:
                    raise ValueError("Scenario decision fields must be predicate/arguments/value")
                name, args, value = decision["predicate"], decision["arguments"], decision["value"]
                if not isinstance(name, str) or name not in DECISIONS:
                    raise ValueError("Scenario decisions may constrain only decision variables")
                signature = signatures[name]
                sorts = signature["arguments"]
                if not isinstance(args, list) or len(args) != len(sorts):
                    raise ValueError("Scenario decision argument count does not match")
                if any(
                    not isinstance(arg, str) or arg not in domains[sort]
                    for arg, sort in zip(args, sorts, strict=True)
                ):
                    raise ValueError("Scenario decision references an entity outside the domain")
                if type(value) is not (bool if signature["returns"] == "Bool" else int):
                    raise ValueError("Scenario decision value type does not match")
                key = (name, *args)
                if key in seen_decisions:
                    raise ValueError("Duplicate scenario decision")
                seen_decisions.add(key)
                symbol = world.call(name, [Entity(sort, arg) for arg, sort in zip(args, sorts, strict=True)])
                decision_constraints.append(symbol == value)
            constraints = [compile_expr(x, world) for x in formulas]
            axioms = world.assignment_axioms() + world.support_axioms(
                catalog["taxonomy"].get("semantics_profile")
            )
            if world.missing:
                raise ValueError(f"Test does not provide all facts: {sorted(world.missing)}")
            solver = z3.Solver()
            solver.set(timeout=timeout_ms)
            solver.add(*axioms, *world.assumptions, *decision_constraints)
            if solver.check() != z3.sat:
                raise ValueError("Underlying test facts/axioms are themselves unsatisfiable")
            solver.add(*constraints)
            actual = str(solver.check())
            result["checks"].append({"id": test["id"], "expected": test["expected"], "actual": actual})
            if actual != test["expected"]:
                result["errors"].append({"test": test, "actual": actual})
            # Delete each constraint to check test discrimination and avoid a suite that validates only axioms.
            if actual == "unsat":
                for i in range(len(constraints)):
                    mutant = z3.Solver()
                    mutant.set(timeout=timeout_ms)
                    mutant.add(
                        *axioms,
                        *world.assumptions,
                        *decision_constraints,
                        *constraints[:i],
                        *constraints[i + 1 :],
                    )
                    if mutant.check() == z3.sat:
                        result["killed_mutants"] += 1
                        discriminated_rules.add(i)
        except (ValueError, KeyError, TypeError, z3.Z3Exception) as exc:
            result["errors"].append(str(exc))
    if not result["killed_mutants"]:
        result["errors"].append(
            "Tests did not kill any constraint-deletion mutants; more discriminating cases are needed"
        )
    if category.get("requirements") and len(discriminated_rules) != len(formulas):
        result["errors"].append(
            "Each generated rule needs a scenario that fails only while that rule is present; avoid redundant rules and untested obligations"
        )
    result["discriminated_rule_count"] = len(discriminated_rules)
    result["passed"] = not result["errors"]
    return result


def verify_semantics(catalog: dict, *, timeout_ms: int) -> dict:
    result = {"passed": True, "categories": []}
    material = catalog["verification_material"]
    for category in catalog["taxonomy"]["categories"]:
        cat = category["id"]
        rules = [
            {k: v for k, v in r.items() if k != "fol"} for r in catalog["rules"] if r["category_id"] == cat
        ]
        base = verification_base(catalog["taxonomy"], category, catalog["knowledge"])
        review = material.get("reviews", {}).get(cat, {})
        verdict = review.get("review", {})
        valid_review = (
            review.get("candidate_hash") == digest(rules)
            and review.get("source_hash") == digest(base)
            and review_passed(verdict)
        )
        reference_valid = True
        if material.get("protocol") == REVIEW_PROTOCOL:
            reference = material.get("reference", {}).get("categories", {}).get(cat, [])
            ref_review = material.get("reference_reviews", {}).get(cat, {})
            reference_valid = bool(
                ref_review.get("protocol") == REVIEW_PROTOCOL
                and ref_review.get("candidate_hash") == digest(reference)
                and ref_review.get("source_hash") == digest(base)
                and review_passed(ref_review.get("review", {}))
            )
            valid_review = valid_review and review.get("protocol") == REVIEW_PROTOCOL
        from confdr.shared.review import validate_obligation_review

        try:
            validate_obligation_review(verdict, category.get("requirements", []))
        except ValueError:
            valid_review = False
        try:
            validate_obligation_review(
                material.get("reference_reviews", {}).get(cat, {}).get("review", {}),
                category.get("requirements", []),
            )
        except ValueError:
            reference_valid = False
        scenarios = verify_scenarios(catalog, cat, timeout_ms=timeout_ms)
        result["categories"].append(
            {
                "category_id": cat,
                "review_valid_for_content": valid_review,
                "reference_review_valid_for_content": reference_valid,
                "scenarios": scenarios,
            }
        )
        result["passed"] = bool(result["passed"] and valid_review and reference_valid and scenarios["passed"])
    return result
