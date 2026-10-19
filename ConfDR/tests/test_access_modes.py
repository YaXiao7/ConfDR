"""Driver capability contracts for dynamically provisioned access modes."""

import z3
from test_taxonomy_atoms import add_storage, snapshot

from confdr.shared.kubernetes import make_world
from confdr.shared.logic import Entity, compile_expr, configure_vocabulary, typecheck
from confdr.shared.storage import access_modes_supported, parameters_valid, provisioner_contracts

ACCESS_PREDICATE = {
    "name": "access_modes_supported",
    "arguments": ["Claim"],
    "returns": "Bool",
    "meaning": "Requested access modes are supported by the provisioner contract",
}
CONTRACT = {
    "example.test/block": {
        "access_modes": {
            "Filesystem": {"supported": ["ReadWriteOnce"], "unsupported": ["ReadWriteMany"]}
        }
    }
}


def dynamic_case(modes, provisioner="example.test/block", volume_mode=None):
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"] = {}
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim["storageClassName"] = "fast"
    claim["accessModes"] = modes
    if volume_mode:
        claim["volumeMode"] = volume_mode
    case["ClusterState"]["StorageClasses"] = {"fast": {"provisioner": provisioner}}
    case["ProvisionerContracts"] = CONTRACT
    return case


def value(case):
    return make_world(case, profile="scheduling-v2").values["access_modes_supported", "default/data"]


def test_capability_is_three_valued():
    assert value(dynamic_case(["ReadWriteOnce"])) is True
    assert value(dynamic_case(["ReadWriteMany"])) is False
    assert value(dynamic_case(["ReadWriteOnce", "ReadWriteMany"])) is False
    assert value(dynamic_case(["ReadOnlyMany"])) is None  # Not covered by the contract.
    assert value(dynamic_case(["ReadWriteMany"], volume_mode="Block")) is None
    assert value(dynamic_case(["ReadWriteMany"], provisioner="example.test/unknown")) is None


def test_missing_class_propagates_configuration_status():
    case = dynamic_case(["ReadWriteMany"])
    case["ClusterState"]["StorageClasses"] = {}
    assert value(case) is False  # Same value as provisioner_configured for a missing class.


def test_registry_entries_cover_documented_block_storage_provisioners():
    contracts = provisioner_contracts({})
    claim = {"accessModes": ["ReadWriteMany"]}
    for provisioner in ("kubernetes.io/gce-pd", "kubernetes.io/azure-disk", "kubernetes.io/aws-ebs",
                        "pd.csi.storage.gke.io"):
        assert access_modes_supported(claim, {"provisioner": provisioner}, contracts) is False
        assert access_modes_supported(
            {"accessModes": ["ReadWriteOnce"]}, {"provisioner": provisioner}, contracts
        ) is True
        # Capability-only contracts leave parameter validation unknown, as without a contract.
        assert parameters_valid({"provisioner": provisioner, "parameters": {}}, contracts) is None


def test_case_contracts_override_registry():
    contracts = provisioner_contracts({"ProvisionerContracts": {"kubernetes.io/gce-pd": {}}})
    assert access_modes_supported(
        {"accessModes": ["ReadWriteMany"]}, {"provisioner": "kubernetes.io/gce-pd"}, contracts
    ) is None


def test_rule_with_declared_predicate_reports_rejected_mode():
    configure_vocabulary([ACCESS_PREDICATE])
    try:
        rule = {
            "quantifier": "forall", "variable": "p", "sort": "Pod",
            "body": {"quantifier": "forall", "variable": "c", "sort": "Claim", "body": {
                "op": "implies", "args": [
                    {"op": "and", "args": [
                        {"call": "uses", "args": [{"var": "p"}, {"var": "c"}]},
                        {"call": "claim_exists", "args": [{"var": "c"}]},
                        {"call": "dynamic", "args": [{"var": "c"}]},
                    ]},
                    {"call": "access_modes_supported", "args": [{"var": "c"}]},
                ]}},
        }
        typecheck(rule)
        for modes, expected in ((["ReadWriteOnce"], z3.sat), (["ReadWriteMany"], z3.unsat)):
            world = make_world(dynamic_case(modes), profile="scheduling-v2")
            solver = z3.Solver()
            solver.add(*world.assignment_axioms(), *world.support_axioms("scheduling-v2"))
            dynamic = world.call("dynamic", [Entity("Claim", "default/data")])
            solver.add(dynamic, compile_expr(rule, world))
            assert solver.check() == expected
    finally:
        configure_vocabulary([])
