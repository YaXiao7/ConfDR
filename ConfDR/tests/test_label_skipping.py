"""End-to-end boundaries for detection, label skipping and response-only correction."""

import copy
import json

import z3
from test_experiment import TransportDouble

from confdr.collection.files import object_key
from confdr.runtime.conflicts import skip_conflicts
from confdr.runtime.pipeline import diagnose
from confdr.runtime.repair import repair_fault
from confdr.runtime.reporting import format_conflicts
from confdr.shared.io import yaml_text
from confdr.shared.kubernetes import rebuild_case
from confdr.shared.llm import grounded_evidence


def test_skip_keeps_shared_constraints_and_finds_disjoint_faults():
    x, y = z3.Bools("protected_x protected_y")
    result = skip_conflicts([x, y, z3.Not(x), z3.Not(y)], protected=[0, 1])
    assert result["solver_status"] == "unsat"
    assert result["terminal_status"] == "sat"
    assert result["complete"] and not result["exhaustive"]
    assert result["model"] is None  # No witness falsely attributed to original input.
    assert len(result["cores"]) == 2
    assert set().union(*(set(r["removed"]) for r in result["rounds"])) == {2, 3}
    assert all(not ({0, 1} & set(r["skipped_before"])) for r in result["rounds"])


def test_skipping_continues_past_ten_checks_until_sat():
    variables = [z3.Bool(f"uncapped_{index}") for index in range(12)]
    formulas = [*variables, *(z3.Not(variable) for variable in variables)]
    result = skip_conflicts(formulas, protected=range(12))
    assert result["solver_status"] == "unsat"
    assert result["terminal_status"] == "sat"
    assert result["complete"] and result["reason"] == "filtered_constraints_satisfiable"
    assert result["checks"] == 13
    assert len(result["cores"]) == 12
    assert len({item for row in result["rounds"] for item in row["removed"]}) == 12


def test_protected_structural_fault_stops_without_claiming_sat():
    x = z3.Bool("protected")
    structural = skip_conflicts([x, z3.Not(x)], protected=[0, 1])
    assert structural["reason"] == "protected_constraints_inconsistent"
    assert structural["terminal_status"] == "unsat" and not structural["complete"]


def test_budget_stop_preserves_last_real_unsat(monkeypatch):
    import confdr.runtime.conflicts as module

    ticks = iter([0, 0, 2, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 2))
    x = z3.Bool("budget_x")
    result = skip_conflicts([x, z3.Not(x)], protected=[0], search_timeout_s=1)
    assert result["reason"] == "search_timeout"
    assert result["terminal_status"] == "unsat"


def test_filtered_sat_llm_cannot_erase_original_fault(case, documents, catalog):
    class Healthy(TransportDouble):
        def complete(self, task, system, payload, schema):
            assert task == "diagnose"
            assert payload["formal"]["solver_status"] == "unsat"
            assert payload["formal"]["terminal_status"] == "sat"
            return {
                "status": "healthy",
                "confidence": 0.99,
                "summary": "Model claims healthy",
                "findings": [],
                "healthy_evidence": [
                    {
                        "path": "/documents/0/metadata/name",
                        "value_json": json.dumps(documents[0]["metadata"]["name"]),
                    }
                ],
            }

    messages = []
    report = diagnose(
        case,
        catalog,
        Healthy(),
        documents=documents,
        configuration_only=True,
        repair_context={},
        progress=messages.append,
    )
    assert report["status"] == "fault" and report["category_ids"] == ["test.constraint"]
    assert not report["formal"]["formal_complete"]
    assert report["llm_trigger"] is None and report["llm"] is None
    assert any("System became satisfiable after filtering conflicting constraints" in m for m in messages)
    assert "ASSIGN_ONE_FOR_P_" in format_conflicts("case", report)
    assert "constraint_" not in format_conflicts("case", report)


def test_llm_response_correction_keeps_same_inputs(case, documents, catalog):
    class Corrected(TransportDouble):
        def complete(self, task, system, payload, schema):
            self.payloads.append(copy.deepcopy(payload))
            assert task == "diagnose"
            if len(self.payloads) == 1:
                return {
                    "status": "fault",
                    "confidence": 0.99,
                    "summary": "Missing evidence",
                    "findings": [],
                    "healthy_evidence": [],
                }
            assert payload["response_correction"]["validation_error"]
            assert payload["case"] == self.payloads[0]["case"]
            assert "pod_info" not in payload
            return {
                "status": "unknown",
                "confidence": 0.99,
                "summary": "No justified extra finding",
                "findings": [],
                "healthy_evidence": [],
            }

    model = Corrected()
    catalog.pop("verification_material")
    result = diagnose(case, catalog, model, documents=documents)
    assert len(model.payloads) == 2 and result["error"] is None
    assert result["status"] == "unknown"
    assert len(result["diagnostic_attempts"]) == 2


def test_bad_diagnostic_evidence_does_not_spend_new_repair_rounds(case, documents, catalog):
    class BadEvidence(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                candidate = copy.deepcopy(documents)
                candidate[0]["spec"]["nodeSelector"] = {"slot": "present"}
                return {"summary": "Selector fixed", "confidence": 0.99, "fixed_yaml": yaml_text(candidate)}
            if task == "diagnose":
                self.calls.append({"task": task})
                return {
                    "status": "healthy",
                    "summary": "Unsupported model claim",
                    "confidence": 0.99,
                    "findings": [],
                    "healthy_evidence": [],
                }
            return super().complete(task, system, payload, schema)

    model = BadEvidence()
    original = diagnose(case, catalog, model, documents=documents, formal_only=True)
    result = repair_fault(case, documents, original, catalog, model, observations={}, max_rounds=5)
    assert model.rounds == result["rounds_used"] == 1
    assert result["stop_reason"] == "detection_passed"
    assert sum(c["task"] == "diagnose" for c in model.calls) == 0
    assert result["status"] == "candidate_accepted"


def test_environment_workload_is_never_expanded_into_targets(case, documents):
    background = copy.deepcopy(documents[0])
    background["metadata"]["name"] = "background"
    locations = {object_key(documents[0]): "apps/app.yaml", object_key(background): "envs/background.yaml"}
    original = copy.deepcopy(case)
    original["ClusterState"]["ExistingPods"] = {"background": {"nodeName": "node", "cpu": 42}}
    candidate = rebuild_case(documents + [background], original, document_locations=locations)
    assert len(candidate["WaitingDeployPods"]) == 1
    assert candidate["ClusterState"]["ExistingPods"] == original["ClusterState"]["ExistingPods"]
    assert not candidate["_unsupported_manifest_features"]
    fresh = rebuild_case(
        documents + [background], {"ClusterState": original["ClusterState"]}, document_locations=locations
    )
    assert len(fresh["WaitingDeployPods"]) == 1


def test_known_empty_inventory_is_citable_but_absence_is_not():
    evidence = [{"path": "/case/ClusterState/PVs", "value_json": "{}"}]
    assert grounded_evidence(evidence, {"case": {"ClusterState": {"PVs": {}}}})
    assert not grounded_evidence(evidence, {"case": {"ClusterState": {}}})


def test_each_detection_restarts_full_constraint_set(case, documents, catalog):
    first = diagnose(case, catalog, TransportDouble(), documents=documents)
    second = diagnose(case, catalog, TransportDouble(), documents=documents)
    assert first["formal"]["category_ids"] == second["formal"]["category_ids"] == ["test.constraint"]
    assert second["formal"]["search"]["rounds"][0]["skipped_before"] == []


def test_actual_unknown_invokes_llm_without_calling_it_healthy(monkeypatch, case, documents, catalog):
    import confdr.runtime.pipeline as pipeline

    monkeypatch.setattr(
        pipeline,
        "solve",
        lambda *a, **kw: {
            "status": "unknown",
            "solver_status": "unknown",
            "terminal_status": "unknown",
            "formal_complete": False,
            "category_ids": [],
            "cores": [],
        },
    )
    model = TransportDouble()
    report = pipeline.diagnose(case, catalog, model, documents=documents)
    assert report["llm_trigger"] == "unknown" and report["status"] == "unknown"
    assert [c["task"] for c in model.calls] == ["diagnose"]


def test_transport_format_error_gets_one_correction_but_network_error_does_not(case, documents, catalog):
    class BrokenTransport(TransportDouble):
        def __init__(self, error):
            super().__init__()
            self.error = error
            self.count = 0

        def complete(self, task, system, payload, schema):
            self.count += 1
            if self.count == 1:
                raise RuntimeError(self.error)
            return super().complete(task, system, payload, schema)

    catalog.pop("verification_material")
    format_error = BrokenTransport("Invalid LLM response format: malformed JSON")
    response = diagnose(case, catalog, format_error, documents=documents)
    assert format_error.count == 2 and response["error"] is None
    network_error = BrokenTransport("LLM request failed or timed out")
    response = diagnose(case, catalog, network_error, documents=documents)
    assert network_error.count == 1 and response["error"]
    assert response["status"] == "unknown"
