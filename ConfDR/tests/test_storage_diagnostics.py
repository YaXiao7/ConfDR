import copy

import pytest

from confdr.runtime.attribution import attributed_categories
from confdr.runtime.storage_diagnostics import storage_diagnostics
from confdr.shared.kubernetes import make_world


@pytest.fixture
def storage_case(case):
    next(iter(case["WaitingDeployPods"].values()))["volumes"] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}
    ]
    case["ClusterState"]["PVCs"] = {"default": {"data": {
        "storageClassName": "fast", "requestsGi": "2Gi", "accessModes": ["ReadWriteOnce"],
    }}}
    return case


def diagnose_storage(case, core=(), *, profile="scheduling-v1"):
    world = make_world(case, profile=profile)
    return storage_diagnostics(case, world, list(core))[0]


def test_empty_class_disables_dynamic_without_inventing_parameter_fault(storage_case):
    storage_case["ClusterState"]["PVCs"]["default"]["data"]["storageClassName"] = ""
    row = diagnose_storage(storage_case)
    assert row["path_policy"] == "static_only"
    assert row["static"]["status"] == "no_candidate"
    assert row["dynamic"]["status"] == "ineligible"
    assert row["dynamic"]["required_evidence"] == []


def test_v2_default_gap_is_one_missing_class_root_cause(storage_case):
    del storage_case["ClusterState"]["PVCs"]["default"]["data"]["storageClassName"]
    storage_case["ClusterState"]["PVs"] = {}
    row = diagnose_storage(storage_case, profile="scheduling-v2")
    assert row["path_policy"] == "default_provisioning_gap"
    assert row["default_provisioning_gap"] is True
    assert "not three independent root causes" in row["repair_guidance"]
    assert diagnose_storage(storage_case)["default_provisioning_gap"] is False


def test_unknown_driver_contract_stays_unknown_and_explains_prerequisite(storage_case):
    storage_case["ClusterState"]["StorageClasses"] = {"fast": {"provisioner": "example.org/csi"}}
    before = copy.deepcopy(storage_case)
    row = diagnose_storage(storage_case)
    assert storage_case == before
    assert row["dynamic"]["status"] == "unknown"
    assert row["dynamic"]["unknown_checks"] == ["provisioner_parameters_valid"]
    assert row["dynamic"]["required_evidence"][0]["provisioner"] == "example.org/csi"
    storage_case["ProvisionerContracts"] = {"example.org/csi": {
        "allowed_parameters": [], "required_parameters": [],
    }}
    row = diagnose_storage(storage_case)
    assert row["dynamic"]["status"] == "compatible"
    assert row["dynamic"]["controller_and_backend_health"] == "not_established_by_configuration_checks"


def test_missing_class_and_incompatible_pv_are_alternative_path_evidence(storage_case):
    storage_case["ClusterState"]["PVs"] = {"small": {
        "storageClassName": "fast", "capacityGi": "1Gi", "accessModes": ["ReadWriteOnce"],
        "phase": "Available", "labels": {},
    }}
    core = [{"category_id": category, "rule_id": rule, "entities": {"c": "default/data"}}
            for category, rule in [
                ("S2", "S2/S2.class.dynamic-storage-class-exists"),
                ("S3", "S3/S3.path-selection"),
                ("S3", "S3/S3.static-binding-compatibility"),
            ]]
    row = diagnose_storage(storage_case, core)
    assert row["static"]["candidates"][0]["failed_checks"] == ["capacity_ok"]
    assert row["attribution"] == "alternative_path_conflict_evidence"
    assert row["path_policy"] == "no_exclusive_path_intent_proven"
    assert attributed_categories(core) == ["S2", "S3"]
    # Once an existing PV is compatible, the missing dynamic alternative alone is not a finding.
    storage_case["ClusterState"]["PVs"]["small"]["capacityGi"] = "3Gi"
    row = diagnose_storage(storage_case)
    assert row["static"]["status"] == "compatible"
    assert row["core_category_ids"] == []


def test_unknown_inventory_is_not_reported_as_empty(storage_case):
    del storage_case["ClusterState"]["PVs"]
    assert diagnose_storage(storage_case)["static"]["status"] == "inventory_unknown"


def test_fixed_volume_prevents_selecting_an_unrelated_compatible_pv(storage_case):
    storage_case["ClusterState"]["PVCs"]["default"]["data"]["volumeName"] = "missing"
    storage_case["ClusterState"]["PVs"] = {"other": {
        "storageClassName": "fast", "capacityGi": "3Gi", "accessModes": ["ReadWriteOnce"],
        "phase": "Available", "labels": {},
    }}
    row = diagnose_storage(storage_case)
    assert row["static"]["candidates"][0]["failed_checks"] == ["fixed_to"]
    assert row["dynamic"]["status"] == "ineligible"
