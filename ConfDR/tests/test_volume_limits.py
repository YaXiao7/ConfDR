"""Interpreter tests use test-only formulas; no production R5 rule is supplied."""

import copy

import pytest
import z3
from test_taxonomy_atoms import catalog_for_test, check, snapshot

from confdr.collection.collector import build_snapshot
from confdr.collection.files import object_key
from confdr.runtime.repair import parse_candidate
from confdr.runtime.solver import solve
from confdr.shared.io import yaml_text
from confdr.shared.kubernetes import rebuild_case
from confdr.shared.logic import Entity
from confdr.validation.kwok import environment_documents


def scenario(limit=2):
    case = snapshot()
    case["ClusterState"]["CSINodes"] = {"n": csi_node(limit)}
    return case


def csi_node(limit):
    driver = {"name": "example.csi", "nodeID": "n"}
    if limit is not None:
        driver["allocatable"] = {"count": limit}
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "CSINode",
        "metadata": {"name": "n"},
        "spec": {"drivers": [driver]},
    }


def add_claim(case, pod, name, driver="example.csi", handle=None):
    cluster = case["ClusterState"]
    cluster["PVCs"].setdefault("default", {})[name] = {"volumeName": name, "storageClassName": ""}
    cluster["PVs"][name] = {
        "csi": {"driver": driver, "volumeHandle": handle or name},
        "storageClassName": "",
        "capacityGi": "1Gi",
        "accessModes": ["ReadWriteOnce"],
    }
    pod["volumes"] = pod.get("volumes") or []
    pod["volumes"].append({"name": name, "persistentVolumeClaim": {"claimName": name}})


def background(case, name):
    pod = {"namespace": "default", "nodeName": "n", "volumes": []}
    case["ClusterState"]["ExistingPods"][name] = pod
    return pod


@pytest.mark.parametrize("count,expected", [(0, z3.sat), (1, z3.sat), (2, z3.sat), (3, z3.unsat)])
def test_budget_boundary(count, expected):
    case = scenario(2)
    for i in range(count):
        add_claim(case, case["WaitingDeployPods"]["p"], str(i))
    status, world, _ = check(case, "R5")
    assert status == expected and not world.missing


def test_duplicate_handles_and_existing_shared_volume_count_once():
    case = scenario(1)
    add_claim(case, background(case, "a"), "a", handle="shared")
    add_claim(case, background(case, "b"), "b", handle="shared")
    add_claim(case, case["WaitingDeployPods"]["p"], "c", handle="shared")
    assert check(case, "R5")[0] == z3.sat
    add_claim(case, case["WaitingDeployPods"]["p"], "d")
    assert check(case, "R5")[0] == z3.unsat


def test_background_already_over_limit_does_not_block_pure_reuse():
    case = scenario(1)
    for name in ["a", "b"]:
        add_claim(case, background(case, name), name)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    assert check(case, "R5")[0] == z3.sat


def test_joint_target_competition_and_shared_volume():
    case = scenario(1)
    case["WaitingDeployPods"]["q"] = copy.deepcopy(case["WaitingDeployPods"]["p"])
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    add_claim(case, case["WaitingDeployPods"]["q"], "b")
    assert check(case, "R5")[0] == z3.unsat
    case["ClusterState"]["PVs"]["b"]["csi"]["volumeHandle"] = "a"
    assert check(case, "R5")[0] == z3.sat


def test_nodes_and_drivers_have_separate_budgets():
    case = scenario(1)
    case["ClusterState"]["CSINodes"]["n"]["spec"]["drivers"].append(
        {"name": "other.csi", "nodeID": "n", "allocatable": {"count": 1}}
    )
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    add_claim(case, case["WaitingDeployPods"]["p"], "b", driver="other.csi")
    assert check(case, "R5")[0] == z3.sat
    case["ClusterState"]["Nodes"]["m"] = copy.deepcopy(case["ClusterState"]["Nodes"]["n"])
    case["ClusterState"]["CSINodes"]["m"] = csi_node(4)
    add_claim(case, case["WaitingDeployPods"]["p"], "c")
    assert check(case, "R5")[0] == z3.sat  # m is feasible
    case["WaitingDeployPods"]["p"]["nodeSelector"] = {"kubernetes.io/hostname": "n"}
    case["ClusterState"]["Nodes"]["n"]["labels"]["kubernetes.io/hostname"] = "n"
    case["ClusterState"]["Nodes"]["m"]["labels"]["kubernetes.io/hostname"] = "m"
    assert check(case, "R5", "P1")[0] == z3.unsat


@pytest.mark.parametrize("missing", ["CSINodes", "ExistingPods", "PVs", "PVCs"])
def test_missing_required_evidence_is_unknown(missing):
    case = scenario(1)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    del case["ClusterState"][missing]
    report = solve(case, catalog_for_test("R5"))
    assert report["status"] == "unknown" and report["missing_evidence"]


def test_known_absent_limit_and_zero_limit_are_distinct():
    case = scenario(None)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    report = solve(case, catalog_for_test("R5"))
    assert report["status"] == "sat" and report["formal_complete"]
    case["ClusterState"]["CSINodes"]["n"] = csi_node(0)
    assert solve(case, catalog_for_test("R5"))["status"] == "unsat"


def test_non_csi_without_csi_inventory_needs_no_r5_evidence():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["volumes"] = [{"name": "temp", "emptyDir": {}}]
    report = solve(case, catalog_for_test("R5"))
    assert report["status"] == "sat" and report["formal_complete"]


def test_missing_named_class_with_static_hostpath_has_no_unknown_csi_driver():
    case = snapshot()
    cluster = case["ClusterState"]
    cluster["StorageClasses"] = {}
    cluster["PVs"]["static"] = {
        "hostPath": {"path": "/mnt/static"},
        "storageClassName": "missing-storageclass",
        "capacityGi": "2Gi",
        "accessModes": ["ReadWriteOnce"],
    }
    cluster["PVCs"].setdefault("default", {})["data"] = {
        "storageClassName": "missing-storageclass",
        "requestsGi": "1Gi",
        "accessModes": ["ReadWriteOnce"],
    }
    case["WaitingDeployPods"]["p"]["volumes"] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}
    ]
    report = solve(case, catalog_for_test("R5"))
    assert report["status"] == "sat" and report["formal_complete"]

    case["collection"] = {"errors": [{"resource": "storageclasses", "error": "Forbidden"}]}
    report = solve(case, catalog_for_test("R5"))
    assert report["status"] == "unknown" and report["missing_evidence"]


def test_unbound_static_and_dynamic_paths_are_counted_by_selected_identity():
    case = scenario(0)
    cluster = case["ClusterState"]
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    cluster["PVCs"]["default"]["a"].pop("volumeName")
    cluster["PVCs"]["default"]["a"]["storageClassName"] = "fast"
    cluster["PVs"]["a"]["storageClassName"] = "fast"
    cluster["StorageClasses"]["fast"] = {"provisioner": "example.csi"}
    for decision in ["bind", "dynamic"]:
        args = [Entity("Claim", "default/a")]
        if decision == "bind":
            args.append(Entity("Volume", "a"))
        assert (
            check(case, "R5", extra=lambda w, decision=decision, args=args: [w.call(decision, args)])[0]
            == z3.unsat
        )
    # R5 does not duplicate S4's obligation to choose a storage path.
    assert check(case, "R5")[0] == z3.sat


def test_direct_node_name_bypasses_scheduler_volume_filter():
    case = scenario(0)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    case["WaitingDeployPods"]["p"]["nodeName"] = "n"
    assert check(case, "R5")[0] == z3.sat


@pytest.mark.parametrize("method", ["label_skipping", "mus"])
def test_both_detection_methods_expose_r5_evidence(method):
    case = scenario(0)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    result = solve(case, catalog_for_test("R5"), conflict_method=method)
    assert result["status"] == "unsat" and result["category_ids"] == ["R5"]
    assert any("volume_count_" in ref for item in result["core"] for ref in item["fact_refs"])


def test_collect_and_kwok_restore_preserve_csi_and_background_volumes(documents):
    node = {"metadata": {"name": "n"}, "status": {"allocatable": {"cpu": "2", "memory": "2Gi", "pods": "20"}}}
    bg = copy.deepcopy(documents[0])
    bg["metadata"]["name"] = "background"
    bg["spec"]["nodeName"] = "n"
    bg["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "a"}}]
    pv = {
        "metadata": {"name": "a"},
        "spec": {
            "csi": {"driver": "example.csi", "volumeHandle": "disk1"},
            "capacity": {"storage": "1Gi"},
            "accessModes": ["ReadWriteOnce"],
        },
    }
    resources = {
        "nodes": [node],
        "pods": [bg],
        "csinodes": [csi_node(2)],
        "pvs": [pv],
        "pvcs": [],
        "storageclasses": [],
    }
    case, _ = build_snapshot(documents, resources)
    assert case["ClusterState"]["CSINodes"]["n"]["spec"]["drivers"][0]["allocatable"]["count"] == 2
    restored = environment_documents(case, documents)
    assert next(d for d in restored if d["kind"] == "PersistentVolume")["spec"]["csi"] == pv["spec"]["csi"]
    assert "hostPath" not in next(d for d in restored if d["kind"] == "PersistentVolume")["spec"]
    assert next(d for d in restored if d["kind"] == "CSINode")["spec"] == csi_node(2)["spec"]
    assert next(d for d in restored if d["kind"] == "Pod")["spec"]["volumes"] == bg["spec"]["volumes"]
    resources.pop("csinodes")
    case, _ = build_snapshot(documents, resources, errors=[{"resource": "csinodes", "error": "Forbidden"}])
    assert "CSINodes" not in case["ClusterState"]


def test_csinode_manifest_does_not_hide_failed_collection_or_override_observation(documents):
    case = scenario(0)
    add_claim(case, case["WaitingDeployPods"]["p"], "a")
    # Test reconstruction with a stable target identity, not manifest mismatches.
    case["WaitingDeployPods"] = {}
    document = copy.deepcopy(documents[0])
    document["spec"]["volumes"] = [{"name": "a", "persistentVolumeClaim": {"claimName": "a"}}]
    rebuilt = rebuild_case([document, csi_node(100)], case)
    assert solve(rebuilt, catalog_for_test("R5"))["status"] == "unsat"
    del case["ClusterState"]["CSINodes"]
    case["collection"] = {"errors": [{"resource": "csinodes", "error": "Forbidden"}]}
    rebuilt = rebuild_case([document, csi_node(100)], case)
    assert solve(rebuilt, catalog_for_test("R5"))["status"] == "unknown"


def test_repair_cannot_fabricate_driver_capacity(documents):
    original = documents + [csi_node(1)]
    candidate = copy.deepcopy(original)
    candidate[-1]["spec"]["drivers"][0]["allocatable"]["count"] = 100
    with pytest.raises(ValueError, match="CSI driver limits"):
        parse_candidate(yaml_text(candidate), original)
    with pytest.raises(ValueError, match="fabricating status"):
        parse_candidate(yaml_text(original), documents)
    assert object_key(csi_node(1)) == ("CSINode", "", "n")


def test_r5_heldout_scenarios_validate_composition():
    from test_taxonomy_atoms import ROOT

    from confdr.shared.io import read
    from confdr.shared.verification import verify_scenarios

    taxonomy = read(ROOT / "config/taxonomy.yaml")
    catalog = catalog_for_test("R5")
    category = next(c for c in taxonomy["categories"] if c["id"] == "R5")
    catalog["taxonomy"]["categories"] = [category]
    catalog["rules"][0]["requirement_ids"] = ["R5.driver-volume-budget"]
    catalog["verification_material"]["tests"] = taxonomy["validation_cases"]
    report = verify_scenarios(catalog, "R5", timeout_ms=3000)
    assert report["passed"], report


def test_port_holder_does_not_inherit_background_volume_references():
    case = scenario(1)
    case["ClusterState"]["Nodes"]["n"]["used_ports"] = [{"hostPort": 80, "protocol": "TCP"}]
    documents = environment_documents(case, [])
    holder = next(d for d in documents if d["kind"] == "Pod")
    assert holder["spec"]["containers"][0]["name"] == "port-holder"
    assert not holder["spec"].get("volumes")


def test_legacy_and_ephemeral_sources_do_not_silently_pass():
    for source in [
        {"ephemeral": {"volumeClaimTemplate": {}}},
        {"awsElasticBlockStore": {"volumeID": "disk"}},
    ]:
        case = scenario(1)
        case["WaitingDeployPods"]["p"]["volumes"] = [{"name": "unresolved", **source}]
        report = solve(case, catalog_for_test("R5"))
        assert report["status"] == "unknown" and report["missing_evidence"]
