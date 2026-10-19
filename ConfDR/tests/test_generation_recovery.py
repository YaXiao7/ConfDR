"""Regression tests for invalid independent oracles and compositional AST failures."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from confdr.formalization.generation import generate_rules
from confdr.formalization.service import revalidate
from confdr.shared.catalog import read_bundle, save_bundle
from confdr.shared.contracts import semantic_contract
from confdr.shared.logic import configure_vocabulary
from confdr.shared.verification import generation_schema, verify_catalog


def specification(catalog):
    cat = catalog["taxonomy"]["categories"][0]["id"]
    return {
        "rules": copy.deepcopy(catalog["rules"]),
        "tests": [
            {
                "id": str(i),
                "category_id": cat,
                "description": f"Selector match is {allowed}",
                "domains": {
                    "Pod": ["p"],
                    "Node": ["n"],
                    "Claim": [],
                    "Volume": [],
                    "Resource": [],
                    "Spread": [],
                },
                "facts": [{"predicate": "selector_ok", "arguments": ["p", "n"], "value": allowed}],
                "expected": "sat" if allowed else "unsat",
            }
            for i, allowed in enumerate((False, True, False, True))
        ],
    }


class Transport:
    backend = model = "test"

    def __init__(self, catalog, invalid=None, permanent=False):
        self.spec = specification(catalog)
        self.invalid = invalid
        self.permanent = permanent
        self.reference_calls = 0
        self.candidate_calls = 0

    def complete(self, task, system, payload, schema):
        if task == "plan_vocabulary":
            return {"predicates": []}
        if task == "independent_spec":
            assert "rules" not in payload and "previous_candidate" not in payload
            assert "verification_feedback" not in payload
            assert "language_contract" in payload and "semantic_contract" in payload
            self.reference_calls += 1
            if self.reference_calls > 1:
                assert payload["specification_feedback"]["status"] == "rejected"
            spec = copy.deepcopy(self.spec)
            if self.invalid and (self.reference_calls == 1 or self.permanent):
                self.invalid(spec)
            return spec
        if task == "generate_rules":
            assert "reference" not in payload and "tests" not in payload
            assert "previous_specification" not in payload
            self.candidate_calls += 1
            return {"rules": copy.deepcopy(self.spec["rules"])}
        if task == "backtranslate":
            assert payload["semantic_contract"]["domains"]["Pod"].startswith("Only target")
            return {"interpretation": "Assigned target Pods satisfy the selector atom."}
        assert task == "semantic_review"
        return {"equivalent": True, "coverage_complete": True, "issues": [], "summary": "Agrees."}


def free_variable(spec):
    spec["rules"][0]["formula"] = {"var": "p"}


def wrong_label(spec):
    for test in spec["tests"]:
        test["expected"] = "unsat" if test["expected"] == "sat" else "sat"


def missing_fact(spec):
    spec["tests"][0]["facts"] = []


def foreign_test(spec):
    spec["tests"][0]["category_id"] = "other"


@pytest.mark.parametrize("invalid", [free_variable, wrong_label, missing_fact, foreign_test])
def test_reference_is_repaired_before_candidate_generation(catalog, invalid):
    transport = Transport(catalog, invalid)
    result = generate_rules(transport, catalog["taxonomy"], verifier=transport)
    assert result["validation"]["status"] == "empirically_validated"
    assert transport.reference_calls == 2 and transport.candidate_calls == 1
    attempts = result["generation"]["reference_attempts"]
    assert [a["verification"]["status"] for a in attempts] == ["rejected", "qualified"]
    assert len(result["verification_material"]["tests"]) == 4  # failed suites never leak into the oracle


def test_unrepairable_reference_does_not_trigger_candidate_retries(catalog):
    transport = Transport(catalog, free_variable, permanent=True)
    result = generate_rules(transport, catalog["taxonomy"], verifier=transport, max_rounds=2)
    assert transport.reference_calls == 2 and transport.candidate_calls == 0
    assert result["validation"]["status"] == "rejected"
    assert not result["verification_material"]["reference"]["categories"]
    assert "Independent reference" in result["generation"]["attempts"][0]["error"]


def test_refresh_reference_uses_the_same_repair_gate(tmp_path, catalog):
    save_bundle(tmp_path / "rules", catalog)
    transport = Transport(catalog, free_variable)
    path = tmp_path / "rules/rules.yaml"
    before = path.read_bytes()
    assert revalidate(path, verifier=transport, refresh_reference=True)["status"] == "empirically_validated"
    assert transport.reference_calls == 2
    assert path.read_bytes() == before
    assert read_bundle(path)["validation"]["status"] == "empirically_validated"


@pytest.mark.parametrize(
    "formula",
    [
        {"op": "ite", "args": [{"bool": True}, {"int": 1}]},
        {"op": "implies", "args": [{"bool": True}] * 3},
        {"call": "uses_scheduler", "args": [{"var": "p"}, {"var": "n"}]},
    ],
)
def test_schema_rejects_wrong_arity(catalog, formula):
    configure_vocabulary()
    rule = copy.deepcopy(catalog["rules"][0])
    rule["formula"] = formula
    assert list(Draft202012Validator(generation_schema()).iter_errors({"rules": [rule]}))


def test_invalid_oracle_error_is_attributed_to_reference(catalog):
    cat = catalog["taxonomy"]["categories"][0]["id"]
    catalog["verification_material"]["reference"]["categories"][cat][0]["formula"] = {"var": "p"}
    report = verify_catalog(catalog)
    assert report["categories"][0]["failure_stage"] == "reference_structure"
    assert report["categories"][0]["checks"] == 0


def test_contract_explicitly_separates_decisions_and_observations():
    contract = semantic_contract("scheduling-v1")
    assert "namespace/name" in contract["domains"]["Claim"]
    assert "schedule_rank" in contract["decisions"]
    assert "unknown" in contract["observation_semantics"]
    assert "boundPV" in json.dumps(contract)


def test_failed_reference_does_not_block_other_categories(catalog):
    taxonomy = copy.deepcopy(catalog["taxonomy"])
    second = {**taxonomy["categories"][0], "id": "other"}
    taxonomy["categories"].append(second)

    class MixedTransport(Transport):
        def complete(self, task, system, payload, schema):
            if task in {"independent_spec", "generate_rules"}:
                cat = payload["taxonomy"]["categories"][0]["id"]
                result = copy.deepcopy(self.spec)
                for rule in result["rules"]:
                    rule.update(id=f"{cat}/check", category_id=cat, source_ids=[f"taxonomy:{cat}"])
                for test in result["tests"]:
                    test["category_id"] = cat
                if cat != "other":
                    assert task == "independent_spec"
                    free_variable(result)
                return result if task == "independent_spec" else {"rules": result["rules"]}
            return super().complete(task, system, payload, schema)

    transport = MixedTransport(catalog)
    result = generate_rules(transport, taxonomy, verifier=transport, max_rounds=2)
    assert [r["category_id"] for r in result["rules"]] == ["other"]
    assert "other" in result["verification_material"]["reviews"]
    assert result["validation"]["status"] == "rejected"  # partial success must not certify the whole catalog


def test_llm_response_error_preserves_schema_path(monkeypatch):
    import io
    import urllib.request

    from confdr.shared.llm import LLM, object_schema

    monkeypatch.setenv("CONFDR_LLM_API_KEY", "test-credential")
    envelope = {"choices": [{"finish_reason": "stop", "message": {"content": '{"number": "bad"}'}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps(envelope)))
    llm = LLM({"model": "test", "base_url": "https://example.invalid"})
    with pytest.raises(RuntimeError, match=r"Schema validation at .*number"):
        llm.complete("test", "test", {}, object_schema({"number": {"type": "integer"}}))
