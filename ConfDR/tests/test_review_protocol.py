"""Regressions reproduced by my-rules-v2: notes, incorrect oracles and candidate loss."""

import copy

import pytest

from confdr.formalization.generation import generate_rules
from confdr.shared.contracts import verification_base
from confdr.shared.io import digest
from confdr.shared.logic import typecheck
from confdr.shared.review import review_passed
from confdr.shared.verification import verify_catalog, verify_scenarios


def verdict(issues=None, **overrides):
    return {
        "equivalent": True,
        "coverage_complete": True,
        "issues": issues or [],
        "summary": "Agrees under the supplied contracts.",
        **overrides,
    }


@pytest.mark.parametrize(
    "issue,passed",
    [
        ({"kind": "note", "message": "Memory accounting is delegated to the declared atom."}, True),
        ({"kind": "rule_error", "message": "Missing dynamic guard."}, False),
        ({"kind": "contract_gap", "message": "Two source definitions contradict each other."}, False),
        ("Legacy issue text must not be silently accepted.", False),
        ({"kind": "unexpected", "message": "Unknown issue kind."}, False),
    ],
)
def test_review_notes_do_not_mask_actual_defects(issue, passed):
    assert review_passed(verdict([issue])) is passed
    assert not review_passed(verdict([issue], equivalent=False))
    assert not review_passed(verdict([issue], coverage_complete=False))


class ReplayTransport:
    backend = model = "offline-regression"

    def __init__(self, catalog, *, reject_reference_once=False, degrade_candidate=False):
        self.catalog = catalog
        self.reference_calls = self.candidate_calls = 0
        self.reject_reference_once = reject_reference_once
        self.degrade_candidate = degrade_candidate

    def complete(self, task, system, payload, schema):
        rules = copy.deepcopy(self.catalog["rules"])
        if task == "plan_vocabulary":
            return {"predicates": []}
        if task == "independent_spec":
            assert "previous_candidate" not in payload and "verification_feedback" not in payload
            self.reference_calls += 1
            tests = [
                {
                    "id": str(i),
                    "category_id": rules[0]["category_id"],
                    "description": f"Selector permits placement: {allowed}",
                    "domains": {"Pod": ["p"], "Node": ["n"]},
                    "facts": [{"predicate": "selector_ok", "arguments": ["p", "n"], "value": allowed}],
                    "expected": "sat" if allowed else "unsat",
                }
                for i, allowed in enumerate((True, False, True, False))
            ]
            return {"rules": rules, "tests": tests}
        if task == "generate_rules":
            self.candidate_calls += 1
            if self.degrade_candidate and self.candidate_calls > 1:
                rules[0]["formula"] = {"var": "c"}
            return {"rules": rules}
        assert task == "semantic_review"  # no lossy LLM backtranslation call
        assert payload["rules"] and payload["rendered_rules"]
        if payload["review_subject"] == "reference":
            if self.reject_reference_once and self.reference_calls == 1:
                return verdict(
                    [{"kind": "rule_error", "message": "Incorrectly forces an optional decision."}]
                )
            return verdict()
        if self.degrade_candidate:
            return verdict([{"kind": "rule_error", "message": "Needs a source guard."}])
        return verdict([{"kind": "note", "message": "Atom implementation is a separate obligation."}])


def test_reference_must_pass_source_review_before_it_judges_candidates(catalog):
    llm = ReplayTransport(catalog, reject_reference_once=True)
    result = generate_rules(llm, catalog["taxonomy"], verifier=llm)
    assert llm.reference_calls == 2 and llm.candidate_calls == 1
    assert result["validation"]["status"] == "empirically_validated"
    attempts = result["generation"]["reference_attempts"]
    assert attempts[0]["stage"] == "reference_semantic_review"
    assert attempts[0]["verification"]["status"] == "rejected"
    assert attempts[0]["specification"]["rules"]


def test_later_broken_ast_cannot_overwrite_a_machine_verified_candidate(catalog):
    llm = ReplayTransport(catalog, degrade_candidate=True)
    result = generate_rules(llm, catalog["taxonomy"], verifier=llm)
    assert result["rules"][0]["formula"] == catalog["rules"][0]["formula"]
    assert result["validation"]["status"] == "unverified"  # preserve actual review failure
    selected = result["generation"]["selected_candidates"]["test.constraint"]
    assert selected["round"] == 1 and not selected["accepted"]
    attempts = result["generation"]["attempts"]
    assert attempts[-1]["candidate"][0]["formula"] == {"var": "c"}
    assert "Free variable: c" in attempts[-1]["verification"]["errors"][0]
    review = result["verification_material"]["reviews"]["test.constraint"]
    executable = [{k: v for k, v in r.items() if k != "fol"} for r in result["rules"]]
    assert review["candidate_hash"] == digest(executable)


def test_reference_review_is_invalidated_by_reference_changes(catalog):
    llm = ReplayTransport(catalog)
    result = generate_rules(llm, catalog["taxonomy"], verifier=llm)
    ref = result["verification_material"]["reference"]["categories"]["test.constraint"][0]
    # Title-only edits leave bounded equivalence unchanged but invalidate the review signature.
    ref["title"] = "Changed reference"
    assert verify_catalog(result)["status"] == "unverified"


def test_source_projection_does_not_create_dangling_category_references(catalog):
    cat = catalog["taxonomy"]["categories"][0]
    knowledge = [{"id": "shared", "categories": [cat["id"], "another"], "text": "Shared source"}]
    base = verification_base(catalog["taxonomy"], cat, knowledge)
    assert base["retrieved_sources"][0]["categories"] == [cat["id"]]
    assert base["retrieved_sources"][0]["provenance_categories"] == [cat["id"], "another"]
    assert knowledge[0]["categories"] == [cat["id"], "another"]


def test_type_error_reports_exact_scope_and_path():
    expr = {
        "quantifier": "forall",
        "variable": "v",
        "sort": "Volume",
        "body": {"call": "bind", "args": [{"var": "c"}, {"var": "v"}]},
    }
    with pytest.raises(ValueError, match=r"Free variable: c at \$/body/args/0; bound variables: .*Volume"):
        typecheck(expr)


def scenario_catalog():
    rule = {
        "formula": {
            "quantifier": "forall",
            "variable": "c",
            "sort": "Claim",
            "body": {
                "op": "implies",
                "args": [
                    {"call": "dynamic", "args": [{"var": "c"}]},
                    {"call": "storage_class_exists", "args": [{"var": "c"}]},
                ],
            },
        },
        "category_id": "guarded",
    }
    tests = []
    for i, (selected, exists) in enumerate(((True, True), (False, False), (True, False), (True, False))):
        tests.append(
            {
                "id": str(i),
                "category_id": "guarded",
                "description": "Test a selected provisioning path",
                "domains": {"Pod": ["p"], "Node": ["n"], "Claim": ["claim"]},
                "facts": [{"predicate": "storage_class_exists", "arguments": ["claim"], "value": exists}],
                "decisions": [{"predicate": "dynamic", "arguments": ["claim"], "value": selected}],
                "expected": "unsat" if selected and not exists else "sat",
            }
        )
    return {
        "taxonomy": {"semantics_profile": "scheduling-v1"},
        "rules": [rule],
        "verification_material": {"tests": tests},
    }


def test_selected_decision_can_be_tested_without_inventing_path_existence_rule():
    c = scenario_catalog()
    assert verify_scenarios(c, "guarded", timeout_ms=3000)["passed"]
    for test in c["verification_material"]["tests"]:
        test["decisions"] = []
    assert not verify_scenarios(c, "guarded", timeout_ms=3000)["passed"]


@pytest.mark.parametrize(
    "decision",
    [
        {"predicate": "storage_class_exists", "arguments": ["claim"], "value": True},
        {"predicate": "dynamic", "arguments": ["outside"], "value": True},
        {"predicate": "dynamic", "arguments": ["claim"], "value": 1},
        {"predicate": "assign", "arguments": ["p", "n"], "value": False},
    ],
)
def test_invalid_or_axiom_conflicting_decision_fixtures_are_rejected(decision):
    c = scenario_catalog()
    c["verification_material"]["tests"][0]["decisions"] = [decision]
    assert not verify_scenarios(c, "guarded", timeout_ms=3000)["passed"]


def test_decision_variables_are_still_forbidden_as_observations():
    c = scenario_catalog()
    c["verification_material"]["tests"][0]["facts"].append(
        {"predicate": "dynamic", "arguments": ["claim"], "value": True}
    )
    report = verify_scenarios(c, "guarded", timeout_ms=3000)
    assert not report["passed"] and any("must not override" in str(error) for error in report["errors"])


def test_partial_catalog_reports_each_category_without_certifying_failed_one(catalog):
    partial = copy.deepcopy(catalog)
    partial["taxonomy"]["categories"].append({"id": "missing", "name": "Missing", "description": "No rule"})
    report = verify_catalog(partial)
    assert report["status"] == "rejected"
    assert report["category_status"] == {"test.constraint": "bounded_verified", "missing": "rejected"}


def test_focused_generation_preserves_history_and_requires_explicit_known_categories(tmp_path, catalog):
    import yaml

    from confdr.formalization.service import generate
    from confdr.shared.io import read

    taxonomy = copy.deepcopy(catalog["taxonomy"])
    taxonomy["categories"].append({"id": "other", "name": "Other", "description": "Not selected"})
    path = tmp_path / "taxonomy.yaml"
    path.write_text(yaml.safe_dump(taxonomy))
    llm = ReplayTransport(catalog)
    report = generate(path, tmp_path / "rules", llm, llm, category_ids=["test.constraint"])
    assert report["status"] == "empirically_validated"
    evidence = read(tmp_path / "rules/verification.json")
    assert evidence["generation"]["category_ids"] == ["test.constraint"]
    assert evidence["generation"]["attempts"][0]["candidate"]
    assert evidence["generation"]["reference_attempts"][0]["specification"]
    assert len(list((tmp_path / "rules").iterdir())) == 3
    with pytest.raises(ValueError, match="Unknown or empty"):
        generate(path, tmp_path / "invalid", llm, llm, category_ids=["typo"])


def test_truncated_model_output_records_finish_reason(monkeypatch):
    import io
    import json
    import urllib.request

    from confdr.shared.llm import LLM, object_schema

    monkeypatch.setenv("CONFDR_LLM_API_KEY", "test-credential")
    response = {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps(response)))
    llm = LLM({"model": "test", "base_url": "https://example.invalid"})
    with pytest.raises(RuntimeError, match="finish_reason='length'"):
        llm.complete("independent_spec", "test", {}, object_schema({"number": {"type": "integer"}}))
    assert llm.calls[-1]["finish_reason"] == "length"
    assert "incomplete output" in llm.calls[-1]["error"]
