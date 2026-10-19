"""Regression boundaries for dataset ownership, admission resources and readable detection reports."""

import copy
import json

import pytest

from confdr.collection.collector import Collector, build_snapshot
from confdr.collection.files import load_case, object_key, ownership_locations
from confdr.collection.service import collect_case
from confdr.runtime.evaluation import truth
from confdr.runtime.pipeline import diagnose
from confdr.runtime.reporting import evaluate_detection_views, format_multiple_rounds, format_single_round
from confdr.shared.io import write, yaml_text
from confdr.shared.kubernetes import apply_resource_defaults, rebuild_case
from confdr.validation.kwok import KwokValidator


@pytest.fixture
def resources_fixture():
    app = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "app"},
        "spec": {"containers": [{"name": str(i), "image": "example.invalid/app:1"} for i in range(3)]},
    }
    background = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "original-system-reservation", "namespace": "kube-system"},
        "spec": {
            "nodeName": "node",
            "containers": [
                {
                    "name": "reservation",
                    "image": "example.invalid/placeholder:1",
                    "resources": {"requests": {"cpu": "728m", "memory": "700816Ki"}},
                }
            ],
        },
        "status": {"phase": "Running"},
    }
    node = {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": "node"},
        "status": {
            "allocatable": {"cpu": "1", "memory": "2Gi", "pods": "10"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    limit = {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "defaults"},
        "spec": {"limits": [{"type": "Container", "defaultRequest": {"cpu": "100m"}}]},
    }
    documents = [app, background, node, limit]
    locations = {
        object_key(d): ("apps/" if d is app else "envs/") + str(i) + ".yaml" for i, d in enumerate(documents)
    }
    resources = {
        "nodes": [node],
        "pods": [background],
        "limitranges": [limit],
        "events": [],
        "pvcs": [],
        "pvs": [],
        "storageclasses": [],
        "namespaces": [],
    }
    return documents, resources, locations


def test_collector_counts_env_workload_once_and_applies_admission_defaults(resources_fixture):
    documents, resources, locations = resources_fixture
    snapshot, obs = build_snapshot(documents, resources, document_locations=locations)
    assert list(snapshot["WaitingDeployPods"]) == ["default/Pod/app/0"]
    target = snapshot["WaitingDeployPods"]["default/Pod/app/0"]
    assert target["cpu"] == 300
    node = snapshot["ClusterState"]["Nodes"]["node"]
    assert node["cpu"] == 272 and node["remain_pods"] == 9
    assert int(node["mem"]) == 2 * 1024**3 - 700816 * 1024
    assert list(snapshot["ClusterState"]["ExistingPods"]) == ["kube-system/original-system-reservation"]
    assert len(obs["pod_status"]) == 1
    assert ownership_locations(snapshot)[object_key(documents[1])].startswith("envs/")
    candidate = rebuild_case(documents, snapshot)
    assert candidate["WaitingDeployPods"] == snapshot["WaitingDeployPods"]
    documents[0]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "50m"}}
    assert rebuild_case(documents, snapshot)["WaitingDeployPods"]["default/Pod/app/0"]["cpu"] == 250


def test_daemonset_targets_expand_from_visible_nodes(resources_fixture):
    documents, resources, locations = resources_fixture
    daemonset = {
        "apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "agent"},
        "spec": {"template": {"metadata": {"labels": {"app": "agent"}}, "spec": {
            "nodeSelector": {"pool": "workload"},
            "containers": [{"name": "agent", "image": "example.invalid/agent:1"}],
        }}},
    }
    documents[0] = daemonset
    resources["nodes"][0]["metadata"]["labels"] = {"pool": "workload"}
    other = copy.deepcopy(resources["nodes"][0])
    other["metadata"]["name"] = "other"
    other["metadata"]["labels"] = {"pool": "system"}
    resources["nodes"].append(other)
    locations = {object_key(d): ("apps/" if d is daemonset else "envs/") + str(i) + ".yaml"
                 for i, d in enumerate(documents)}
    snapshot, _ = build_snapshot(documents, resources, document_locations=locations)
    targets = snapshot["WaitingDeployPods"]
    assert list(targets) == ["default/DaemonSet/agent/node"]
    assert targets["default/DaemonSet/agent/node"]["daemonset_target_node"] == "node"
    assert not snapshot["_unsupported_manifest_features"]
    rebuilt = rebuild_case(documents, snapshot, document_locations=locations)
    assert list(rebuilt["WaitingDeployPods"]) == list(targets)


def test_live_admitted_resources_never_turn_scheduled_node_into_placement_intent(resources_fixture):
    documents, resources, locations = resources_fixture
    live = copy.deepcopy(documents[0])
    live["spec"]["nodeName"] = "node"
    live["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "400m"}}
    live["status"] = {"phase": "Running"}
    resources["pods"].append(live)
    snapshot, obs = build_snapshot(documents, resources, document_locations=locations)
    target = snapshot["WaitingDeployPods"]["default/Pod/app/0"]
    assert target["cpu"] == 400 and not target.get("nodeName")
    assert snapshot["ClusterState"]["Nodes"]["node"]["cpu"] == 272
    assert obs["pod_status"]["default/Pod/app/0"]["nodeName"] == "node"
    # Unmodelled resource mutations cannot silently disappear and create a false passing candidate.
    assert any(
        "admission resource mutation" in s
        for s in rebuild_case(documents, snapshot)["_unsupported_manifest_features"]
    )


def test_limit_defaults_namespace_explicit_limits_and_ambiguous_policies(resources_fixture):
    documents, resources, _ = resources_fixture
    spec = documents[0]["spec"]
    limits = resources["limitranges"]
    assert apply_resource_defaults(spec, "other", limits)[0] == spec | {
        "containers": [c | {"resources": {}} for c in spec["containers"]]
    }
    explicit = {"containers": [{"resources": {"limits": {"cpu": "500m"}}}]}
    effective, errors = apply_resource_defaults(explicit, "default", limits)
    assert effective["containers"][0]["resources"]["requests"]["cpu"] == "500m" and not errors
    conflicting = copy.deepcopy(limits[0])
    conflicting["spec"]["limits"][0]["defaultRequest"]["cpu"] = "200m"
    assert apply_resource_defaults(spec, "default", limits + [conflicting])[1]


def test_failed_limit_inventory_stays_unknown_after_rebuild(resources_fixture):
    documents, resources, locations = resources_fixture
    snapshot, _ = build_snapshot(
        documents,
        resources,
        document_locations=locations,
        errors=[{"resource": "limitranges", "error": "denied"}],
    )
    assert (
        "Resource default inventory was not observed"
        in rebuild_case(documents, snapshot)["_unsupported_manifest_features"]
    )


@pytest.mark.parametrize("kwok", [False, True])
def test_collection_service_recovers_stale_env_targets_and_preserves_ownership(
    tmp_path, monkeypatch, resources_fixture, kwok
):
    documents, resources, locations = resources_fixture
    source, output = tmp_path / "input", tmp_path / "collected"
    for doc in documents:
        write(source / locations[object_key(doc)], yaml_text([doc]))
    stale, _ = build_snapshot(documents, resources)  # Historical bug: every workload treated as target.
    write(source / "case.json", stale)
    write(source / "truth.json", {"is_pending": True, "pending_labels": ["R1"]})
    with pytest.raises(ValueError, match="envs workload"):
        load_case(source)
    calls = []

    def collect(self, docs, **kwargs):
        calls.append(kwargs)
        return build_snapshot(docs, resources, document_locations=kwargs["document_locations"])

    monkeypatch.setattr(Collector, "collect", collect)
    if kwok:

        def probe(self, docs, case, *, phase):
            snapshot, obs = collect(None, docs, document_locations=ownership_locations(case))
            return {"status": "failed", "reason": "pending", "case": snapshot, "observations": obs}

        monkeypatch.setattr(KwokValidator, "probe", probe)
    collect_case(source, output, {}, kwok=kwok, kubeconfig=None if kwok else "fake-config")
    snapshot, _, _, _ = load_case(output)
    assert len(snapshot["WaitingDeployPods"]) == 1
    assert snapshot["ClusterState"]["Nodes"]["node"]["cpu"] == 272
    assert calls[0]["document_locations"][object_key(documents[1])].startswith("envs/")
    assert (output / "truth.json").read_bytes() == (source / "truth.json").read_bytes()


def test_kwok_applies_env_workloads_without_target_instrumentation(monkeypatch, resources_fixture):
    documents, resources, locations = resources_fixture
    snapshot, _ = build_snapshot(documents, resources, document_locations=locations)
    validator = KwokValidator()
    applied, restored, waited = [], [], []
    monkeypatch.setattr(validator, "_command", lambda *a, **k: "")
    monkeypatch.setattr(validator, "_restore_environment", lambda config, docs: restored.extend(docs))
    monkeypatch.setattr(validator, "_kubectl", lambda *a, **k: applied.append(k.get("stdin", "")))
    monkeypatch.setattr(
        validator, "_wait", lambda config, token, pods, claims, phase: waited.append((pods, claims)) or {}
    )

    def collect(self, docs, **kwargs):
        assert kwargs["document_locations"][object_key(documents[1])].startswith("envs/")
        return build_snapshot(docs, resources, document_locations=kwargs["document_locations"])

    monkeypatch.setattr(Collector, "collect", collect)
    result = validator._phase(documents, snapshot, phase="original")
    assert result["status"] != "error", result
    assert waited == [(1, 0)]
    assert any(d["metadata"]["name"] == "original-system-reservation" for d in restored)
    assert all("original-system-reservation" not in yaml for yaml in applied)
    assert len(result["case"]["WaitingDeployPods"]) == 1


def test_unknown_payload_excludes_oracle_pod_info(case, catalog):
    catalog.pop("verification_material")

    class Auxiliary:
        attempts = 0

        def complete(self, task, system, payload, schema):
            self.attempts += 1
            assert set(payload) in ({"case", "categories"}, {"case", "categories", "response_correction"})
            assert all(not k.startswith("_") for k in payload["case"])
            if self.attempts == 2:
                return {
                    "status": "unknown", "confidence": 0.99, "summary": "No configuration evidence",
                    "healthy_evidence": [], "findings": [],
                }
            return {
                "status": "fault",
                "confidence": 0.99,
                "summary": "Observed failure",
                "healthy_evidence": [],
                "findings": [
                    {
                        "category_ids": [],
                        "verification_scope": "runtime",
                        "summary": "Crash evidence",
                        "evidence": [
                            {"path": "/pod_info/reason", "value_json": json.dumps("CrashLoopBackOff")}
                        ],
                    }
                ],
            }

    result = diagnose(case, catalog, Auxiliary(), observations={"reason": "CrashLoopBackOff"})
    assert result["llm_trigger"] == "unknown" and result["status"] == "unknown"
    assert result["diagnostic_attempts"][0]["valid"] is False
    assert not result["category_ids"]
    assert "triggered by UNKNOWN" in format_multiple_rounds("case", result)


def test_reports_separate_raw_sat_from_incomplete_formal_coverage():
    detection = {
        "status": "unknown",
        "formal": {
            "solver_status": "sat",
            "status": "unknown",
            "initial_core": [],
            "core": [],
            "core_search_complete": True,
            "reason": "missing_rules",
        },
    }
    first = format_single_round("case", detection)
    assert "SAT: original constraints" in first and "0 constraints in first unsat core" not in first
    assert "Overall formal result: UNKNOWN" in format_multiple_rounds("case", detection)


def test_all_views_use_same_cases_and_missing_first_check_is_an_abstention():
    record = {
        "truth": {"status": "fault", "category_ids": ["R1"], "category_labelled": True},
        "result": {"detection": {"status": "unknown"}},
    }
    result = evaluate_detection_views([record])
    assert all(v["scored_cases"] == 1 and v["micro"]["fn"] == 1 for v in result.values())
    assert result["single_round"]["abstentions"] == 1


def test_empty_pending_labels_require_explicit_annotation_completeness(tmp_path):
    data = {"is_pending": True, "pending_labels": []}
    write(tmp_path / "truth.json", data)
    assert not truth(tmp_path)["category_labelled"]
    write(tmp_path / "truth.json", data | {"annotation_status": "complete"})
    assert truth(tmp_path)["category_labelled"]


def test_unused_env_claim_is_not_a_deployment_target(resources_fixture):
    documents, resources, locations = resources_fixture
    claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "background-data"}, "spec": {}}
    documents.append(claim)
    locations[object_key(claim)] = "envs/background-claim.yaml"
    _, obs = build_snapshot(documents, resources, document_locations=locations)
    assert obs["pvc_status"] == {}
    documents[0]["spec"]["volumes"] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": "background-data"}}
    ]
    _, obs = build_snapshot(documents, resources, document_locations=locations)
    assert obs["pvc_status"]["default/background-data"]["phase"] == "NotObserved"
