"""Extraction must cover declared obligations, not just match a second model."""

import copy
from pathlib import Path

import pytest
import yaml

from confdr.formalization.generation import validate_taxonomy
from confdr.shared.requirements import check_coverage, check_test_coverage
from confdr.shared.review import validate_obligation_review
from confdr.shared.verification import check_structure


def structured(catalog):
    result = copy.deepcopy(catalog)
    category = result["taxonomy"]["categories"][0]
    category.update(
        requirements=[
            {
                "id": "placement",
                "when": "assign(p,n)",
                "guarded": True,
                "must": "selector_ok(p,n)",
                "required_atoms": ["assign", "selector_ok"],
            }
        ],
        allowed_atoms=["assign", "selector_ok"],
        test_policy="obligation-boundaries-v1",
    )
    result["rules"][0]["requirement_ids"] = ["placement"]
    return result


def test_structured_requirements_are_checked_for_both_rule_and_reference(catalog):
    c = structured(catalog)
    check_structure(c, c["knowledge"])
    c["rules"][0]["requirement_ids"] = []
    with pytest.raises(ValueError, match="requirement_ids"):
        check_structure(c, c["knowledge"])


def test_missing_atom_or_cross_category_atom_is_rejected(catalog):
    c = structured(catalog)
    rule = c["rules"][0]
    rule["formula"] = {"bool": True}
    with pytest.raises(ValueError, match="missing atoms"):
        check_coverage(c["taxonomy"]["categories"][0], [rule])
    rule["formula"] = {"call": "node_ready", "args": [{"var": "n"}]}
    with pytest.raises(ValueError, match="outside its category"):
        check_coverage(c["taxonomy"]["categories"][0], [rule])


def test_scenarios_need_positive_negative_and_inactive_guards(catalog):
    category = structured(catalog)["taxonomy"]["categories"][0]
    tests = [
        {"requirement_ids": ["placement"], "scenario_kind": kind, "expected": expected}
        for kind, expected in [("positive", "sat"), ("negative", "unsat")]
    ]
    with pytest.raises(ValueError, match="guard_inactive"):
        check_test_coverage(category, tests)
    tests.append({"requirement_ids": ["placement"], "scenario_kind": "guard_inactive", "expected": "sat"})
    check_test_coverage(category, tests)
    tests[-1]["expected"] = "unsat"
    with pytest.raises(ValueError, match="contradicts"):
        check_test_coverage(category, tests)


def test_global_review_booleans_do_not_substitute_for_obligation_review(catalog):
    requirements = structured(catalog)["taxonomy"]["categories"][0]["requirements"]
    with pytest.raises(ValueError, match="guard and consequence"):
        validate_obligation_review({"equivalent": True, "coverage_complete": True}, requirements)
    validate_obligation_review(
        {
            "obligation_checks": [
                {
                    "requirement_id": "placement",
                    "guard_correct": True,
                    "consequence_correct": True,
                    "explanation": "Assignment implies selector match.",
                }
            ]
        },
        requirements,
    )


def test_all_fifteen_categories_have_explicit_contracts_and_storage_boundaries():
    taxonomy = validate_taxonomy(
        yaml.safe_load((Path(__file__).resolve().parents[1] / "config/taxonomy.yaml").read_text())
    )
    categories = {c["id"]: c for c in taxonomy["categories"]}
    assert len(categories) == 15
    assert all(c["requirements"] for c in categories.values())
    assert {"dynamic_eligible", "bind"} <= set(categories["S3"]["allowed_atoms"])
    assert not {"storage_class_exists", "volume_node_ok", "dynamic_node_ok"} & set(
        categories["S3"]["allowed_atoms"]
    )
    assert "uses_scheduler" in categories["P4"]["allowed_atoms"]
    assert all("uses_scheduler(p)" in r["when"] for r in categories["P4"]["requirements"])


def test_structured_generation_and_saved_evidence_validate_end_to_end(catalog):
    from test_review_protocol import ReplayTransport

    from confdr.formalization.generation import generate_rules
    from confdr.shared.verification import verify_catalog

    source = structured(catalog)

    class StructuredReplay(ReplayTransport):
        def complete(self, task, system, payload, schema):
            result = super().complete(task, system, payload, schema)
            if task == "independent_spec":
                for test in result["tests"]:
                    test.update(
                        requirement_ids=["placement"],
                        scenario_kind="positive" if test["expected"] == "sat" else "negative",
                    )
                # Assign to a second node: the failing selector on n has an inactive guard.
                result["tests"].append(
                    {
                        "id": "inactive",
                        "category_id": "test.constraint",
                        "description": "Unselected node imposes no selector check",
                        "domains": {"Pod": ["p"], "Node": ["n", "other"]},
                        "facts": [
                            {"predicate": "selector_ok", "arguments": ["p", "n"], "value": False},
                            {"predicate": "selector_ok", "arguments": ["p", "other"], "value": True},
                        ],
                        "decisions": [{"predicate": "assign", "arguments": ["p", "other"], "value": True}],
                        "expected": "sat",
                        "scenario_kind": "guard_inactive",
                        "requirement_ids": ["placement"],
                    }
                )
            if task == "semantic_review":
                result["obligation_checks"] = [
                    {
                        "requirement_id": "placement",
                        "guard_correct": True,
                        "consequence_correct": True,
                        "explanation": "Every selected placement implies the selector predicate.",
                    }
                ]
            return result

    model = StructuredReplay(source)
    result = generate_rules(model, source["taxonomy"], verifier=model)
    assert result["validation"]["status"] == "empirically_validated"
    # A hand-edited acceptance Boolean cannot bypass a missing obligation audit.
    result["verification_material"]["reviews"]["test.constraint"]["review"].pop("obligation_checks")
    assert verify_catalog(result)["status"] == "unverified"
