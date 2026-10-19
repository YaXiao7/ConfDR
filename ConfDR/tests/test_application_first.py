"""Applications and environment are always detected together; edits prefer applications."""

import copy
import json

import pytest
from test_experiment import TransportDouble

from confdr.collection.files import manifest_scope, object_key
from confdr.runtime.pipeline import diagnose
from confdr.runtime.repair import repair_fault
from confdr.runtime.repair_scope import check_repair_scope, environment_request
from confdr.shared.io import yaml_text


def node():
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": "node", "labels": {"slot": "present"}},
        "spec": {},
    }


def request():
    return {
        "needed": True,
        "reason": "The application placement requirement is fixed; the environment label is incorrect.",
        "impact": "Changing this label affects other workloads selecting slot; recheck their requirements.",
        "evidence": [{"path": "/documents/1/metadata/labels/slot", "value_json": '"present"'}],
    }


def test_application_scope_respects_directory_ownership(documents):
    original = documents + [node()]
    locations = {object_key(documents[0]): "apps/app.yaml", object_key(original[1]): "envs/node.yaml"}
    candidate = copy.deepcopy(original)
    candidate[1]["metadata"]["labels"]["slot"] = "absent"
    with pytest.raises(ValueError, match="Application-first"):
        check_repair_scope(candidate, original, locations, "application")
    assert manifest_scope(documents[0], {object_key(documents[0]): "envs/background.yaml"}) == "environment"
    assert manifest_scope({"kind": "PersistentVolumeClaim"}) == "application"
    assert manifest_scope({"kind": "PersistentVolume"}) == "environment"


def test_justification_must_match_actual_environment_and_targets(case, documents):
    original = documents + [node()]
    approved = environment_request(request(), case, original, {})
    candidate = copy.deepcopy(original)
    candidate[1]["metadata"]["labels"]["slot"] = "absent"
    changes = check_repair_scope(candidate, original, {}, "environment", approved)
    assert changes[0]["name"] == "node"
    candidate.append(
        {
            "apiVersion": "v1",
            "kind": "StorageClass",
            "metadata": {"name": "unrelated"},
            "provisioner": "example",
        }
    )
    with pytest.raises(ValueError, match="outside the cited evidence"):
        check_repair_scope(candidate, original, {}, "environment", approved)
    bad = request()
    bad["evidence"][0]["value_json"] = '"invented"'
    with pytest.raises(ValueError, match="does not match"):
        environment_request(bad, case, original, {})
    bad["evidence"] = [{"path": "/documents/0/spec/nodeSelector/slot", "value_json": '"absent"'}]
    with pytest.raises(ValueError, match="not only application"):
        environment_request(bad, case, original, {})


def test_known_empty_inventory_differs_from_missing_inventory(case, documents):
    proposed = request()
    proposed["evidence"] = [{"path": "/case/ClusterState/PVs", "value_json": "{}"}]
    assert environment_request(proposed, case, documents, {})["creatable_kinds"] == ["PersistentVolume"]
    incomplete = copy.deepcopy(case)
    del incomplete["ClusterState"]["PVs"]
    with pytest.raises(ValueError, match="does not resolve"):
        environment_request(proposed, incomplete, documents, {})


def test_full_environment_is_rechecked_after_evidence_backed_escalation(case, documents, catalog):
    original = documents + [node()]
    locations = {object_key(documents[0]): "apps/app.yaml", object_key(original[1]): "envs/node.yaml"}

    class Model(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                assert payload["documents"] == original  # both inputs in both phases
                assert payload["case"]["ClusterState"]["Nodes"]
                if self.rounds == 1:
                    assert payload["repair_scope"]["phase"] == "application"
                    return {
                        "summary": "Application-only repair is not justified",
                        "confidence": 0.99,
                        "fixed_yaml": "",
                        "environment_change": request(),
                    }
                assert payload["repair_scope"]["phase"] == "environment"
                candidate = copy.deepcopy(original)
                candidate[1]["metadata"]["labels"]["slot"] = "absent"
                return {
                    "summary": "Correct the cited node label",
                    "confidence": 0.99,
                    "fixed_yaml": yaml_text(candidate),
                }
            if task == "diagnose" and payload.get("repair_context"):
                assert len(payload["documents"]) == 2
                assert payload["case"]["ClusterState"]["Nodes"]["node"]["labels"]["slot"] == "absent"
                assert payload["formal"]["status"] == "sat"
                assert payload["repair_context"]["environment_changes"]
                return {
                    "status": "healthy",
                    "summary": "Configuration placement matches",
                    "findings": [],
                    "healthy_evidence": [
                        {"path": "/documents/1/metadata/labels/slot", "value_json": json.dumps("absent")}
                    ],
                    "confidence": 0.99,
                }
            return super().complete(task, system, payload, schema)

    model = Model()
    detection = diagnose(case, catalog, model, documents=original)
    result = repair_fault(
        case, original, detection, catalog, model, observations={}, document_locations=locations, max_rounds=2
    )
    assert result["status"] == "candidate_accepted"
    assert [a["phase"] for a in result["attempts"]] == ["application", "environment"]
    assert result["environment_changes"][0]["name"] == "node"
    assert result["rounds_used"] == 2


def test_invalid_application_candidate_never_escalates(case, documents, catalog):
    original = documents + [node()]

    class Model(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                assert payload["repair_scope"]["phase"] == "application"
                bad = copy.deepcopy(original)
                bad[1]["metadata"]["labels"]["slot"] = "absent"
                return {
                    "summary": "Unauthorized environment edit",
                    "confidence": 0.99,
                    "fixed_yaml": yaml_text(bad),
                }
            return super().complete(task, system, payload, schema)

    model = Model()
    detection = diagnose(case, catalog, model, documents=original)
    result = repair_fault(case, original, detection, catalog, model, observations={}, max_rounds=2)
    assert result["status"] == "not_accepted"
    assert model.rounds == 2
    assert all(a["phase"] == "application" for a in result["attempts"])
    assert "Application-first" in result["reason"]


def test_escalation_does_not_exceed_total_round_budget(case, documents, catalog):
    original = documents + [node()]

    class Model(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                return {
                    "summary": "Application-only repair unavailable",
                    "confidence": 0.99,
                    "fixed_yaml": "",
                    "environment_change": request(),
                }
            return super().complete(task, system, payload, schema)

    model = Model()
    detection = diagnose(case, catalog, model, documents=original)
    result = repair_fault(case, original, detection, catalog, model, observations={}, max_rounds=1)
    assert model.rounds == result["rounds_used"] == 1
    assert result["stop_reason"] == "max_rounds_reached"
    assert result["status"] == "not_accepted"
