"""StatefulSet-generated claims remain visible to diagnosis without precreating PVC manifests."""

from confdr.collection.collector import build_snapshot
from confdr.shared.kubernetes import rebuild_case
from confdr.validation.kwok import environment_documents


def statefulset():
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "alpha", "namespace": "dgraph"},
        "spec": {
            "serviceName": "alpha",
            "replicas": 3,
            "selector": {"matchLabels": {"app": "alpha"}},
            "template": {
                "metadata": {"labels": {"app": "alpha"}},
                "spec": {"containers": [
                    {"name": "alpha", "image": "registry.k8s.io/pause:3.9",
                     "volumeMounts": [{"name": "datadir", "mountPath": "/dgraph"}]}
                ]},
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "datadir"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "anything",
                    "resources": {"requests": {"storage": "5Gi"}},
                },
            }],
        },
    }


def test_rebuild_creates_per_ordinal_claims_and_references():
    case = rebuild_case(
        [statefulset()],
        {"WaitingDeployPods": {}, "ClusterState": {"Nodes": {}, "LimitRanges": []}},
    )
    assert not case["_unsupported_manifest_features"]
    claims = case["ClusterState"]["PVCs"]["dgraph"]
    assert sorted(claims) == [f"datadir-alpha-{i}" for i in range(3)]
    for i in range(3):
        key = f"dgraph/StatefulSet/alpha/{i}"
        assert case["WaitingDeployPods"][key]["volumes"] == [
            {"name": "datadir", "persistentVolumeClaim": {"claimName": f"datadir-alpha-{i}"}}
        ]
        assert claims[f"datadir-alpha-{i}"]["requestsGi"] == "5Gi"


def test_collection_preserves_uncreated_ordinals_but_uses_observed_pvc():
    doc = statefulset()
    resources = {
        "nodes": [{
            "metadata": {"name": "node-1", "labels": {"kubernetes.io/hostname": "node-1"}},
            "spec": {"taints": []},
            "status": {"allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"},
                       "conditions": [{"type": "Ready", "status": "True"}]},
        }],
        "pods": [{
            "metadata": {"name": "alpha-0", "namespace": "dgraph", "uid": "pod-0",
                         "ownerReferences": [{"kind": "StatefulSet", "name": "alpha"}]},
            "spec": {"containers": doc["spec"]["template"]["spec"]["containers"],
                     "volumes": [{"name": "datadir", "persistentVolumeClaim": {
                         "claimName": "datadir-alpha-0"}}]},
            "status": {"phase": "Pending"},
        }],
        "pvcs": [{
            "metadata": {"name": "datadir-alpha-0", "namespace": "dgraph", "uid": "claim-0"},
            "spec": doc["spec"]["volumeClaimTemplates"][0]["spec"],
            "status": {"phase": "Pending"},
        }],
        "pvs": [],
        "storageclasses": [],
        "csinodes": [],
        "events": [],
    }
    case, observations = build_snapshot([doc], resources)
    assert not case["_unsupported_manifest_features"]
    assert case["ClusterState"]["PVCs"]["dgraph"]["datadir-alpha-0"]["STATUS"] == "Pending"
    assert case["ClusterState"]["PVCs"]["dgraph"]["datadir-alpha-1"]["STATUS"] == "NotObserved"
    assert observations["pvc_status"]["dgraph/datadir-alpha-0"]["phase"] == "Pending"
    assert observations["pvc_status"]["dgraph/datadir-alpha-1"]["phase"] == "NotObserved"
    assert not any(d["kind"] == "PersistentVolumeClaim" for d in environment_documents(case, [doc]))
