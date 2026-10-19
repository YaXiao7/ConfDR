"""Independent scenario expectations for the fifteen-category vocabulary.

The formulas below are test specifications, not production rules or a substitute
for independent validation of an LLM-generated catalog.
"""

import copy
from pathlib import Path

import pytest
import yaml
import z3

from confdr.collection.collector import build_snapshot
from confdr.collection.files import load_case
from confdr.formalization.generation import suite_schema, validate_taxonomy
from confdr.runtime.solver import solve
from confdr.shared.catalog import read_bundle
from confdr.shared.kubernetes import make_world, pod_from_spec, rebuild_case
from confdr.shared.logic import BASE_SIGNATURES, SORTS, Entity, compile_expr, typecheck
from confdr.shared.resources import extended_requests
from confdr.shared.storage import effective_class

ROOT = Path(__file__).resolve().parents[1]


def call(name, *variables):
    return {"call": name, "args": [{"var": v} for v in variables]}


def op(name, *args):
    return {"op": name, "args": list(args)}


def forall(variable, sort, body):
    return {"quantifier": "forall", "variable": variable, "sort": sort, "body": body}


def specifications():
    assigned = call("assign", "p", "n")

    def on_node(predicate):
        return forall("p", "Pod", forall("n", "Node", op("implies", assigned, call(predicate, "p", "n"))))

    rules = {}
    for code, request, budget in [
        ("R1", "cpu_request", "free_cpu"),
        ("R2", "memory_request", "free_memory"),
        ("R4", None, "pod_slots"),
    ]:
        amount = call(request, "p") if request else {"int": 1}
        total = {
            "quantifier": "sum",
            "variable": "p",
            "sort": "Pod",
            "body": op("ite", assigned, amount, {"int": 0}),
        }
        rules[code] = forall("n", "Node", op("le", total, call(budget, "n")))
    total = {
        "quantifier": "sum",
        "variable": "p",
        "sort": "Pod",
        "body": op("ite", assigned, call("resource_request", "p", "r"), {"int": 0}),
    }
    rules["R3"] = forall(
        "r",
        "Resource",
        forall(
            "n",
            "Node",
            op("implies", call("is_gpu_resource", "r"), op("le", total, call("resource_free", "n", "r"))),
        ),
    )
    for code, predicate in [
        ("A1", "scheduling_tolerates"),
        ("P2", "pod_affinity_ok"),
        ("P3", "pod_anti_affinity_ok"),
    ]:
        rules[code] = on_node(predicate)
    rules["P1"] = op("and", on_node("selector_ok"), on_node("node_affinity_ok"))
    skew = op(
        "sub",
        op("add", call("spread_count", "t", "n"), call("spread_self_match", "t")),
        call("spread_global_min", "t"),
    )
    rules["P4"] = forall(
        "t",
        "Spread",
        forall(
            "p",
            "Pod",
            forall(
                "n",
                "Node",
                op(
                    "implies",
                    op("and", call("spread_applies", "t", "p"), assigned),
                    op("and", call("spread_has_key", "t", "n"), op("le", skew, call("spread_max_skew", "t"))),
                ),
            ),
        ),
    )
    rules["S1"] = forall(
        "c",
        "Claim",
        op(
            "and",
            call("claim_exists", "c"),
            op("implies", call("claim_exists", "c"), call("fixed_volume_exists", "c")),
        ),
    )
    rules["S2"] = forall(
        "c",
        "Claim",
        op(
            "implies",
            op("and", call("claim_exists", "c"), call("dynamic", "c")),
            op(
                "and",
                *[
                    call(x, "c")
                    for x in (
                        "storage_class_exists",
                        "provisioner_configured",
                        "provisioner_parameters_valid",
                    )
                ],
            ),
        ),
    )
    exists = {"quantifier": "exists", "variable": "v", "sort": "Volume", "body": call("bind", "c", "v")}
    matching = op(
        "and",
        *[
            call(x, "c", "v")
            for x in ("class_ok", "access_ok", "mode_ok", "capacity_ok", "volume_available", "pv_selector_ok")
        ],
        op("implies", call("has_fixed_volume", "c"), call("fixed_to", "c", "v")),
    )
    rules["S3"] = forall(
        "c",
        "Claim",
        op(
            "implies",
            op("and", call("claim_exists", "c"), call("fixed_volume_exists", "c")),
            op(
                "and",
                op("or", exists, op("and", call("dynamic", "c"), call("dynamic_eligible", "c"))),
                forall("v", "Volume", op("implies", call("bind", "c", "v"), matching)),
            ),
        ),
    )
    rules["S4"] = forall(
        "p",
        "Pod",
        forall(
            "c",
            "Claim",
            forall(
                "n",
                "Node",
                op(
                    "implies",
                    op("and", call("uses", "p", "c"), assigned),
                    op(
                        "and",
                        op("implies", call("dynamic", "c"), call("dynamic_node_ok", "c", "n")),
                        forall(
                            "v",
                            "Volume",
                            op("implies", call("bind", "c", "v"), call("volume_node_ok", "v", "n")),
                        ),
                    ),
                ),
            ),
        ),
    )
    distinct = op("ne", {"var": "p"}, {"var": "q"})
    rules["E1"] = op(
        "and",
        on_node("host_ports_free"),
        forall(
            "p",
            "Pod",
            forall(
                "q",
                "Pod",
                forall(
                    "n",
                    "Node",
                    op(
                        "implies",
                        op("and", distinct, assigned, call("assign", "q", "n")),
                        call("ports_disjoint", "p", "q"),
                    ),
                ),
            ),
        ),
    )
    new = call("volume_count_new", "p", "n", "d")
    before = call("volume_count_before", "p", "n", "d")
    guard = op(
        "and",
        call("uses_scheduler", "p"),
        assigned,
        call("volume_limit_applies", "n", "d"),
        op("gt", new, {"int": 0}),
    )
    rules["R5"] = forall(
        "p",
        "Pod",
        forall(
            "n",
            "Node",
            forall(
                "d",
                "Driver",
                op("implies", guard, op("le", op("add", before, new), call("volume_limit", "n", "d"))),
            ),
        ),
    )
    return rules


def snapshot():
    pod = pod_from_spec(
        {"containers": [{"name": "main", "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}]},
        {"namespace": "default", "labels": {"app": "test"}},
    )
    return {
        "WaitingDeployPods": {"p": pod},
        "ClusterState": {
            "Nodes": {
                "n": {
                    "cpu": 1000,
                    "mem": str(1024**3),
                    "remain_pods": 1,
                    "labels": {"zone": "a", "disk": "ssd"},
                    "taints": [],
                    "used_ports": [],
                    "extended_free": {},
                }
            },
            "ExistingPods": {},
            "PVCs": {},
            "PVs": {},
            "StorageClasses": {},
            "NamespaceLabels": {"default": {}},
        },
    }


def check(case, *codes, extra=None, profile="scheduling-v1"):
    world = make_world(case, profile=profile)
    solver = z3.Solver()
    solver.add(*world.assignment_axioms(), *world.support_axioms(profile))
    for code in codes:
        formula = specifications()[code]
        typecheck(formula)
        solver.add(compile_expr(formula, world))
    if extra:
        solver.add(*extra(world))
    result = solver.check()
    return result, world, solver


def add_storage(case):
    case["WaitingDeployPods"]["p"]["volumes"] = [{"persistentVolumeClaim": {"claimName": "data"}}]
    case["ClusterState"]["PVCs"] = {
        "default": {"data": {"storageClassName": "", "requestsGi": "1Gi", "accessModes": ["ReadWriteOnce"]}}
    }
    case["ClusterState"]["PVs"] = {
        "v": {
            "storageClassName": "",
            "capacityGi": "1Gi",
            "accessModes": ["ReadWriteOnce"],
            "phase": "Available",
            "labels": {"tier": "fast"},
        }
    }


def hard_affinity(value, mode="podAffinity"):
    return {
        mode: {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {"topologyKey": "zone", "labelSelector": {"matchLabels": {"app": value}}}
            ]
        }
    }


def spread(**overrides):
    return {
        "topologyKey": "zone",
        "maxSkew": 1,
        "whenUnsatisfiable": "DoNotSchedule",
        "labelSelector": {"matchLabels": {"app": "test"}},
        **overrides,
    }


def test_taxonomy_and_vocabulary_are_english_and_complete():
    taxonomy = validate_taxonomy(yaml.safe_load((ROOT / "config/taxonomy.yaml").read_text()))
    assert len(taxonomy["groups"]) == 5
    assert {c["id"] for c in taxonomy["categories"]} == set(specifications())
    assert not any("\u4e00" <= c <= "\u9fff" for spec in BASE_SIGNATURES.values() for c in spec[2])
    assert set(suite_schema()["properties"]["tests"]["items"]["properties"]["domains"]["properties"]) == set(
        SORTS
    )


@pytest.mark.parametrize("code,field", [("R1", "cpu"), ("R2", "mem"), ("R4", "remain_pods")])
def test_capacity_boundary_and_competition(code, field):
    case = snapshot()
    assert check(case, code)[0] == z3.sat
    case["WaitingDeployPods"]["q"] = copy.deepcopy(case["WaitingDeployPods"]["p"])
    assert check(case, code)[0] == z3.unsat
    case["ClusterState"]["Nodes"]["n"][field] = (
        str(2 * 1024**3) if field == "mem" else 2000 if field == "cpu" else 2
    )
    assert check(case, code)[0] == z3.sat


def test_gpu_names_and_missing_observations():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["extended_requests"] = {"nvidia.com/gpu": 1}
    node = case["ClusterState"]["Nodes"]["n"]
    node["extended_free"] = {"amd.com/gpu": 5}
    assert check(case, "R3")[0] == z3.unsat
    node["extended_free"]["nvidia.com/gpu"] = 1
    assert check(case, "R3")[0] == z3.sat
    del node["extended_free"]
    assert "resource_free/n/nvidia.com/gpu" in check(case, "R3")[1].missing


def test_extended_init_peaks_and_invalid_fraction():
    spec = {
        "containers": [{"resources": {"limits": {"nvidia.com/gpu": 1}}}],
        "initContainers": [
            {"restartPolicy": "Always", "resources": {"limits": {"nvidia.com/gpu": 2}}},
            {"resources": {"limits": {"nvidia.com/gpu": 4}}},
        ],
    }
    assert extended_requests(spec) == {"nvidia.com/gpu": 6}
    spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] = "0.5"
    with pytest.raises(ValueError, match="integer"):
        extended_requests(spec)


@pytest.mark.parametrize(
    "effect,expected", [("NoSchedule", z3.unsat), ("NoExecute", z3.unsat), ("PreferNoSchedule", z3.sat)]
)
def test_taint_effects_and_matching(effect, expected):
    case = snapshot()
    case["ClusterState"]["Nodes"]["n"]["taints"] = [{"key": "dedicated", "value": "gpu", "effect": effect}]
    assert check(case, "A1")[0] == expected
    case["WaitingDeployPods"]["p"]["tolerations"] = [
        {"key": "dedicated", "operator": "Exists", "effect": effect}
    ]
    assert check(case, "A1")[0] == z3.sat


def test_node_selector_and_required_affinity_are_conjunctive():
    case = snapshot()
    pod = case["WaitingDeployPods"]["p"]
    pod["nodeSelector"] = {"disk": "ssd"}
    pod["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["b"]}]}
                ]
            }
        }
    }
    assert check(case, "P1")[0] == z3.unsat
    pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ].append({"matchExpressions": [{"key": "zone", "operator": "In", "values": ["a"]}]})
    assert check(case, "P1")[0] == z3.sat


def test_affinity_future_cycle_and_self_affinity():
    case = snapshot()
    pods = case["WaitingDeployPods"]
    pods["p"]["labels"] = {"app": "a"}
    pods["p"]["affinity"] = hard_affinity("b")
    pods["q"] = {**copy.deepcopy(pods["p"]), "labels": {"app": "b"}, "affinity": hard_affinity("a")}
    assert check(case, "P2")[0] == z3.unsat
    for pod in pods.values():
        pod["labels"] = {"app": "a"}
        pod["affinity"] = hard_affinity("a")
    assert check(case, "P2")[0] == z3.sat


def test_existing_pod_anti_affinity_is_enforced():
    case = snapshot()
    existing = {
        **copy.deepcopy(case["WaitingDeployPods"]["p"]),
        "nodeName": "n",
        "affinity": hard_affinity("test", "podAntiAffinity"),
    }
    case["ClusterState"]["ExistingPods"] = {"other": existing}
    assert check(case, "P3")[0] == z3.unsat
    existing["affinity"] = {}
    assert check(case, "P3")[0] == z3.sat


def test_spread_min_domains_and_ordered_counts():
    case = snapshot()
    pod = case["WaitingDeployPods"]["p"]
    pod["topologySpreadConstraints"] = [spread(minDomains=2)]
    case["WaitingDeployPods"]["q"] = copy.deepcopy(pod)
    assert check(case, "P4")[0] == z3.unsat
    case["ClusterState"]["Nodes"]["m"] = {
        **copy.deepcopy(case["ClusterState"]["Nodes"]["n"]),
        "labels": {"zone": "b"},
    }
    result, world, solver = check(case, "P4")
    assert result == z3.sat
    model = solver.model()
    assert z3.is_true(
        model.eval(world.call("assign", [Entity("Pod", "p"), Entity("Node", "n")]))
    ) != z3.is_true(model.eval(world.call("assign", [Entity("Pod", "q"), Entity("Node", "n")])))


def test_spread_soft_constraints_and_missing_background():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["topologySpreadConstraints"] = [spread(whenUnsatisfiable="ScheduleAnyway")]
    assert check(case, "P4")[1].domains["Spread"] == []
    case["WaitingDeployPods"]["p"]["topologySpreadConstraints"] = [spread()]
    del case["ClusterState"]["ExistingPods"]
    assert check(case, "P4")[1].missing


def test_pvc_and_explicit_pv_references():
    case = snapshot()
    add_storage(case)
    assert check(case, "S1")[0] == z3.sat
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim["volumeName"] = "missing"
    assert check(case, "S1")[0] == z3.unsat
    del case["ClusterState"]["PVCs"]["default"]["data"]
    assert check(case, "S1")[0] == z3.unsat


def test_storage_selector_capacity_and_prebinding():
    case = snapshot()
    add_storage(case)
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim["selector"] = {"matchLabels": {"tier": "slow"}}
    assert check(case, "S3")[0] == z3.unsat
    claim["selector"]["matchLabels"]["tier"] = "fast"
    assert check(case, "S3")[0] == z3.sat
    claim["requestsGi"] = "2Gi"
    assert check(case, "S3")[0] == z3.unsat
    claim["requestsGi"] = "1Gi"
    claim["selector"]["matchLabels"]["tier"] = "slow"
    claim["volumeName"] = "v"
    assert check(case, "S3")[0] == z3.sat


def test_dynamic_provisioning_contract_and_no_existing_pv():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"] = {}
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim["storageClassName"] = "fast"
    assert check(case, "S2", "S3")[0] == z3.unsat
    case["ClusterState"]["StorageClasses"] = {
        "fast": {"provisioner": "example.test/csi", "parameters": {"pool": "a"}}
    }
    assert "provisioner_parameters_valid/default/data" in check(case, "S2", "S3")[1].missing
    case["ProvisionerContracts"] = {
        "example.test/csi": {
            "allowed_parameters": ["pool"],
            "required_parameters": ["pool"],
            "parameter_values": {"pool": ["a"]},
        }
    }
    result, world, _ = check(case, "S2", "S3")
    assert result == z3.sat and not world.missing
    case["ClusterState"]["StorageClasses"]["fast"]["parameters"]["pool"] = "b"
    assert check(case, "S2", "S3")[0] == z3.unsat


def test_default_provisioning_boundary_is_versioned_and_requires_empty_pv_inventory():
    production_v2 = read_bundle(ROOT / "rules/rules-v2-default-sc/rules.yaml")
    case = snapshot()
    add_storage(case)
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    del claim["storageClassName"]
    case["ClusterState"]["PVs"] = {}
    old = make_world(case, profile="scheduling-v1")
    new = make_world(case, profile="scheduling-v2")
    assert old.values["dynamic_eligible", "default/data"] is False
    assert new.values["dynamic_eligible", "default/data"] is True
    assert check(case, "S2", profile="scheduling-v1")[0] == z3.sat
    assert check(case, "S3", profile="scheduling-v1")[0] == z3.unsat
    # S2 is an implication on the chosen dynamic path; S3 requires a path.
    assert check(case, "S2", profile="scheduling-v2")[0] == z3.sat
    assert check(case, "S3", profile="scheduling-v2")[0] == z3.sat
    assert check(case, "S2", "S3", profile="scheduling-v2")[0] == z3.unsat
    assert solve(case, production_v2)["category_ids"] == ["S2"]
    claim["storageClassName"] = None
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is True
    del claim["storageClassName"]

    claim["storageClassName"] = ""
    assert check(case, "S2", profile="scheduling-v2")[0] == z3.sat
    assert check(case, "S3", profile="scheduling-v2")[0] == z3.unsat
    assert solve(case, production_v2)["category_ids"] == ["S3"]

    del claim["storageClassName"]
    case["ClusterState"]["PVs"]["v"] = {
        "storageClassName": "", "capacityGi": "1Gi", "accessModes": ["ReadWriteOnce"],
        "phase": "Available", "labels": {},
    }
    claim["accessModes"] = ["ReadWriteMany"]
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is False
    assert check(case, "S2", profile="scheduling-v2")[0] == z3.sat
    assert check(case, "S3", profile="scheduling-v2")[0] == z3.unsat
    assert solve(case, production_v2)["category_ids"] == ["S3"]
    claim["accessModes"] = ["ReadWriteOnce"]
    assert check(case, "S2", "S3", profile="scheduling-v2")[0] == z3.sat

    del case["ClusterState"]["PVs"]
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is None
    result = solve(case, production_v2)
    assert result["status"] == "unknown"
    assert "ClusterState/PVs" in result["missing_evidence"]
    case["ClusterState"]["PVs"] = {}
    del case["ClusterState"]["StorageClasses"]
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is None
    result = solve(case, production_v2)
    assert result["status"] == "unknown"
    assert "ClusterState/StorageClasses" in result["missing_evidence"]


def test_v2_named_missing_class_prefers_observed_same_class_static_pv():
    case = snapshot()
    add_storage(case)
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim["storageClassName"] = "manual"
    claim["requestsGi"] = "2Gi"
    case["ClusterState"]["PVs"]["v"]["storageClassName"] = "manual"
    assert make_world(case, profile="scheduling-v1").values["dynamic_eligible", "default/data"] is True
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is False
    production_v2 = read_bundle(ROOT / "rules/rules-v2-default-sc/rules.yaml")
    assert solve(case, production_v2)["category_ids"] == ["S3"]
    case["ClusterState"]["PVs"] = {}
    assert make_world(case, profile="scheduling-v2").values["dynamic_eligible", "default/data"] is True
    assert solve(case, production_v2)["category_ids"] == ["S2"]


def test_storage_topology_constrains_only_selected_volume():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"]["v"]["nodeAffinity"] = {
        "required": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "zone", "operator": "In", "values": ["b"]}]}]
        }
    }
    assert check(case, "S3", "S4")[0] == z3.unsat
    case["ClusterState"]["PVs"]["other"] = {
        **copy.deepcopy(case["ClusterState"]["PVs"]["v"]),
        "nodeAffinity": {},
    }
    assert check(case, "S3", "S4")[0] == z3.sat


def test_ports_protocols_background_and_targets():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["host_ports"] = [{"hostPort": 80, "protocol": "TCP"}]
    node = case["ClusterState"]["Nodes"]["n"]
    node["used_ports"] = [{"hostPort": 80, "protocol": "UDP"}]
    assert check(case, "E1")[0] == z3.sat
    node["used_ports"][0]["protocol"] = "TCP"
    assert check(case, "E1")[0] == z3.unsat
    node["used_ports"] = []
    case["WaitingDeployPods"]["q"] = copy.deepcopy(case["WaitingDeployPods"]["p"])
    assert check(case, "E1")[0] == z3.unsat


def test_default_storage_class_and_explicit_empty():
    classes = {
        "first": {"metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}}
    }
    assert effective_class({}, classes) == "first"
    assert effective_class({"storageClassName": ""}, classes) == ""
    classes["second"] = copy.deepcopy(classes["first"])
    assert effective_class({}, classes) is None


def test_collection_gpu_background_and_storage_metadata():
    target = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "target"},
        "spec": {"containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": 1}}}]},
    }
    background = {
        "metadata": {"name": "background"},
        "spec": {"nodeName": "n", "containers": [{"resources": {"limits": {"nvidia.com/gpu": 2}}}]},
        "status": {"phase": "Running"},
    }
    node = {
        "metadata": {"name": "n"},
        "status": {"allocatable": {"cpu": "4", "memory": "4Gi", "pods": "10", "nvidia.com/gpu": "4"}},
    }
    case, _ = build_snapshot(
        [target],
        {
            "nodes": [node],
            "pods": [background],
            "pvcs": [],
            "pvs": [{"metadata": {"name": "v", "labels": {"tier": "fast"}}, "spec": {}}],
            "storageclasses": [{"metadata": {"name": "fast"}, "parameters": {"pool": "a"}}],
        },
    )
    assert case["ClusterState"]["Nodes"]["n"]["extended_free"]["nvidia.com/gpu"] == 2
    assert case["ClusterState"]["PVs"]["v"]["labels"] == {"tier": "fast"}
    assert case["ClusterState"]["StorageClasses"]["fast"]["parameters"] == {"pool": "a"}
    target["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] = 3
    rebuilt = rebuild_case([target], case)
    assert check(rebuilt, "R3")[0] == z3.unsat


def test_shared_storage_uniqueness_and_shared_claim():
    case = snapshot()
    add_storage(case)
    case["WaitingDeployPods"]["q"] = copy.deepcopy(case["WaitingDeployPods"]["p"])
    assert check(case, "S3")[0] == z3.sat
    case["WaitingDeployPods"]["q"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other"
    case["ClusterState"]["PVCs"]["default"]["other"] = copy.deepcopy(
        case["ClusterState"]["PVCs"]["default"]["data"]
    )
    assert check(case, "S3")[0] == z3.unsat


def test_claim_uid_prevents_reusing_stale_bound_volume():
    case = snapshot()
    add_storage(case)
    claim = case["ClusterState"]["PVCs"]["default"]["data"]
    claim.update(uid="new", volumeName="v")
    case["ClusterState"]["PVs"]["v"].update(
        phase="Bound", claimRef={"namespace": "default", "name": "data", "uid": "old"}
    )
    assert check(case, "S3")[0] == z3.unsat
    case["ClusterState"]["PVs"]["v"]["claimRef"]["uid"] = "new"
    assert check(case, "S3")[0] == z3.sat


def test_joint_cpu_and_node_selection_conflict():
    case = snapshot()
    nodes = case["ClusterState"]["Nodes"]
    nodes["n"]["cpu"] = 500
    nodes["m"] = {**copy.deepcopy(nodes["n"]), "cpu": 1000, "labels": {"disk": "hdd"}}
    case["WaitingDeployPods"]["p"]["nodeSelector"] = {"disk": "ssd"}
    assert check(case, "R1")[0] == z3.sat
    assert check(case, "P1")[0] == z3.sat
    assert check(case, "R1", "P1")[0] == z3.unsat


@pytest.mark.parametrize("policy,expected", [("Ignore", z3.sat), ("Honor", z3.unsat)])
def test_spread_taint_inclusion_policy(policy, expected):
    case = snapshot()
    case["WaitingDeployPods"]["p"]["topologySpreadConstraints"] = [
        spread(minDomains=2, nodeTaintsPolicy=policy)
    ]
    nodes = case["ClusterState"]["Nodes"]
    nodes["n"]["taints"] = [{"key": "reserved", "effect": "NoSchedule"}]
    nodes["m"] = {**copy.deepcopy(nodes["n"]), "labels": {"zone": "b"}, "taints": []}
    case["ClusterState"]["ExistingPods"] = {
        "old-a": {**copy.deepcopy(case["WaitingDeployPods"]["p"]), "nodeName": "n"},
        "old-b": {**copy.deepcopy(case["WaitingDeployPods"]["p"]), "nodeName": "m"},
    }
    assert check(case, "P4", "A1")[0] == expected


def test_spread_match_label_keys_and_namespace():
    case = snapshot()
    pod = case["WaitingDeployPods"]["p"]
    pod["labels"]["revision"] = "new"
    pod["topologySpreadConstraints"] = [spread(minDomains=2, matchLabelKeys=["revision"])]
    other = {**copy.deepcopy(pod), "nodeName": "n", "labels": {"app": "test", "revision": "old"}}
    case["ClusterState"]["ExistingPods"] = {"old": other}
    assert check(case, "P4")[0] == z3.sat
    other["labels"]["revision"] = "new"
    assert check(case, "P4")[0] == z3.unsat
    other["namespace"] = "another"
    assert check(case, "P4")[0] == z3.sat


def test_spread_with_one_matching_peer_is_feasible_in_one_or_three_domains():
    case = snapshot()
    pod = case["WaitingDeployPods"]["p"]
    pod["topologySpreadConstraints"] = [spread()]
    case["ClusterState"]["ExistingPods"] = {
        "peer": {"namespace": "default", "labels": {"app": "test"}, "nodeName": "n"}
    }
    # With one domain, the incoming Pod increases its count by maxSkew=1.
    assert check(case, "P4")[0] == z3.sat
    first = case["ClusterState"]["Nodes"]["n"]
    case["ClusterState"]["Nodes"].update({
        "m": {**copy.deepcopy(first), "labels": {"zone": "b"}},
        "o": {**copy.deepcopy(first), "labels": {"zone": "c"}},
    })
    # With three domains, the Pod can use an empty domain.
    assert check(case, "P4")[0] == z3.sat


def test_affinity_terms_must_match_the_same_candidate_pod():
    case = snapshot()
    pod = case["WaitingDeployPods"]["p"]
    pod["labels"] = {"app": "incoming"}
    pod["affinity"] = hard_affinity("a")
    pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"].extend(
        hard_affinity("b")["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    )
    case["ClusterState"]["ExistingPods"] = {
        key: {**copy.deepcopy(pod), "labels": {"app": key}, "nodeName": "n", "affinity": None}
        for key in ("a", "b")
    }
    assert check(case, "P2")[0] == z3.unsat


def test_unknown_advanced_affinity_is_not_silently_accepted():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["affinity"] = hard_affinity("test")
    case["WaitingDeployPods"]["p"]["affinity"]["podAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ][0]["matchLabelKeys"] = ["revision"]
    assert "pod_affinity_ok/p/n" in check(case, "P2")[1].missing


def test_pvc_namespace_is_part_of_reference_identity():
    case = snapshot()
    add_storage(case)
    case["WaitingDeployPods"]["p"]["namespace"] = "other"
    assert check(case, "S1")[0] == z3.unsat


def test_wffc_topology_and_unresolved_immediate_supply():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"] = {}
    case["ClusterState"]["PVCs"]["default"]["data"]["storageClassName"] = "fast"
    sc = {
        "provisioner": "example.test/csi",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowedTopologies": [{"matchLabelExpressions": [{"key": "zone", "values": ["a"]}]}],
    }
    case["ClusterState"]["StorageClasses"] = {"fast": sc}
    assert check(case, "S3", "S4")[0] == z3.sat
    sc["allowedTopologies"][0]["matchLabelExpressions"][0]["values"] = ["b"]
    assert check(case, "S3", "S4")[0] == z3.unsat
    sc["volumeBindingMode"] = "Immediate"
    assert "dynamic_node_ok/default/data/n" in check(case, "S3", "S4")[1].missing


def test_host_port_addresses_follow_scheduler_not_socket_semantics():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["host_ports"] = [{"hostPort": 80, "hostIP": "::"}]
    case["ClusterState"]["Nodes"]["n"]["used_ports"] = [{"hostPort": 80, "hostIP": "127.0.0.1"}]
    assert check(case, "E1")[0] == z3.sat
    case["ClusterState"]["Nodes"]["n"]["used_ports"][0]["hostIP"] = "0.0.0.0"
    assert check(case, "E1")[0] == z3.unsat


def catalog_for_test(*codes):
    taxonomy = yaml.safe_load((ROOT / "config/taxonomy.yaml").read_text())
    # These test-only formulas exercise atom semantics; extraction contracts have dedicated tests.
    taxonomy["categories"] = [
        {
            k: v
            for k, v in c.items()
            if k not in {"requirements", "allowed_atoms", "excluded_scope", "test_policy"}
        }
        for c in taxonomy["categories"]
        if c["id"] in codes
    ]
    rules = [
        {
            "id": f"test/{c}",
            "category_id": c,
            "title": f"Test specification for {c}",
            "source_ids": [f"test:{c}"],
            "formula": specifications()[c],
        }
        for c in codes
    ]
    return {
        "schema_version": "2.0",
        "taxonomy": taxonomy,
        "rules": rules,
        "knowledge": [{"id": f"test:{c}", "categories": [c], "text": "Test-only constraint"} for c in codes],
        "verification_material": {
            "origin": "external_reference",
            "reference": {
                "categories": {c: [copy.deepcopy(r) for r in rules if r["category_id"] == c] for c in codes}
            },
        },
    }


def test_runtime_reports_gpu_conflict_and_legacy_inventory_gap():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["extended_requests"] = {"nvidia.com/gpu": 1}
    catalog = catalog_for_test("R3")
    result = solve(case, catalog)
    assert result["status"] == "unsat" and result["category_ids"] == ["R3"]
    del case["WaitingDeployPods"]["p"]["extended_requests"]
    result = solve(case, catalog)
    assert result["status"] == "unknown" and not result["formal_complete"]
    assert "WaitingDeployPods/p/extended_requests" in result["missing_evidence"]


def test_runtime_spread_order_and_complete_fifteen_category_input():
    case = snapshot()
    case["WaitingDeployPods"]["p"]["topologySpreadConstraints"] = [spread(minDomains=2)]
    case["WaitingDeployPods"]["q"] = copy.deepcopy(case["WaitingDeployPods"]["p"])
    case["ClusterState"]["Nodes"]["m"] = {
        **copy.deepcopy(case["ClusterState"]["Nodes"]["n"]),
        "labels": {"zone": "b"},
    }
    result = solve(case, catalog_for_test(*specifications()))
    assert result["status"] == "sat" and result["formal_complete"]
    assert set(result["scheduling_order"]) == {"p", "q"}
    del case["WaitingDeployPods"]["p"]["topologySpreadConstraints"]
    result = solve(case, catalog_for_test("P4"))
    assert result["status"] == "unknown"


def test_english_taxonomy_rejects_chinese_rule_titles():
    catalog = catalog_for_test("R1")
    catalog["rules"][0]["title"] = "\u8d44\u6e90\u4e0d\u8db3"
    assert solve(snapshot(), catalog)["unverified_categories"] == ["R1"]


def test_case_loader_preserves_contracts_and_resource_names(tmp_path):
    import json

    case = snapshot()
    case["ProvisionerContracts"] = {"example.org/csi": {"allowed_parameters": [], "required_parameters": []}}
    case["gpu_resource_names"] = ["example.org/slice"]
    (tmp_path / "case.json").write_text(json.dumps(case))
    loaded, _, _, _ = load_case(tmp_path)
    assert loaded["ProvisionerContracts"] == case["ProvisionerContracts"]
    assert loaded["gpu_resource_names"] == case["gpu_resource_names"]


def test_candidate_removes_unbound_pvc_selector_without_stale_fields():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVCs"]["default"]["data"]["selector"] = {"matchLabels": {"tier": "slow"}}
    doc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data"},
        "spec": {
            "storageClassName": "",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }
    assert check(case, "S3")[0] == z3.unsat
    rebuilt = rebuild_case([doc], case)
    assert "selector" not in rebuilt["ClusterState"]["PVCs"]["default"]["data"]
    assert check(rebuilt, "S3")[0] == z3.sat


def test_bound_volume_ownership_is_not_reset_by_candidate_yaml():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"]["v"].update(
        phase="Bound", claimRef={"namespace": "default", "name": "someone-else"}
    )
    doc = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": "v"},
        "spec": {"capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"]},
    }
    rebuilt = rebuild_case([doc], case)
    assert rebuilt["ClusterState"]["PVs"]["v"]["phase"] == "Bound"
    assert check(rebuilt, "S3")[0] == z3.unsat


def test_present_empty_pvc_selector_disables_dynamic_provisioning():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVs"] = {}
    case["ClusterState"]["PVCs"]["default"]["data"].update(storageClassName="fast", selector={})
    case["ClusterState"]["StorageClasses"] = {"fast": {"provisioner": "example.org/csi"}}
    assert check(case, "S3")[0] == z3.unsat


def test_namespace_dictionary_cannot_masquerade_as_a_claim():
    case = snapshot()
    add_storage(case)
    case["WaitingDeployPods"]["p"]["namespace"] = "absent"
    case["WaitingDeployPods"]["p"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "default"
    assert check(case, "S1")[0] == z3.unsat


def test_flat_legacy_claim_cannot_cross_namespaces():
    case = snapshot()
    add_storage(case)
    case["ClusterState"]["PVCs"] = case["ClusterState"]["PVCs"]["default"]
    assert check(case, "S1")[0] == z3.sat
    case["WaitingDeployPods"]["p"]["namespace"] = "absent"
    assert check(case, "S1")[0] == z3.unsat
