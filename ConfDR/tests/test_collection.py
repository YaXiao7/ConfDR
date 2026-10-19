import copy

from confdr.collection.collector import build_snapshot
from confdr.collection.files import associate_targets, copy_case, discover, load_case, save_candidate
from confdr.shared.io import digest, read, write, yaml_text
from confdr.validation.kwok import environment_documents


def test_target_excluded_from_background_and_event_uid(documents):
    node = {
        "kind": "Node",
        "metadata": {"name": "node"},
        "status": {
            "allocatable": {"cpu": "1", "memory": "1Gi", "pods": "10"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    target = copy.deepcopy(documents[0])
    target["metadata"].update(namespace="default", uid="new")
    target["spec"]["nodeName"] = "node"
    target["status"] = {"phase": "Running"}
    background = copy.deepcopy(target)
    background["metadata"].update(namespace="other", uid="other")
    events = [
        {"involvedObject": {"kind": "Pod", "namespace": "default", "name": "app", "uid": uid}, "reason": uid}
        for uid in ("old", "new")
    ]
    case, obs = build_snapshot(
        documents,
        {
            "nodes": [node],
            "pods": [target, background],
            "events": events,
            "pvcs": [],
            "pvs": [],
            "storageclasses": [],
            "namespaces": [],
        },
    )
    assert case["ClusterState"]["Nodes"]["node"]["cpu"] == 990
    assert case["ClusterState"]["Nodes"]["node"]["remain_pods"] == 9
    assert list(case["ClusterState"]["ExistingPods"]) == ["other/app"]
    assert obs["candidate_hash"] == digest(documents)
    assert [e["reason"] for es in obs["pod_events"].values() for e in es] == ["new"]
    restored = environment_documents(case, documents)
    n = next(d for d in restored if d["kind"] == "Node")
    assert n["status"]["allocatable"]["cpu"] == "1000m"
    assert n["status"]["allocatable"]["memory"] == str(1024**3)


def test_controller_owner_chain(documents):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "controller", "namespace": "space"},
        "spec": {"template": {"spec": documents[0]["spec"]}},
    }
    live = copy.deepcopy(documents[0])
    live["metadata"] = {
        "name": "random",
        "namespace": "space",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "rs", "uid": "r1"}],
    }
    rs = {
        "kind": "ReplicaSet",
        "metadata": {
            "name": "rs",
            "namespace": "space",
            "uid": "r1",
            "ownerReferences": [{"kind": "Deployment", "name": "controller"}],
        },
    }
    _, obs = build_snapshot([deployment], {"nodes": [], "pods": [live], "replicasets": [rs]})
    assert next(iter(obs["pod_status"].values()))["actual_name"] == "random"
    rs["metadata"]["uid"] = "stale"
    _, obs = build_snapshot([deployment], {"nodes": [], "pods": [live], "replicasets": [rs]})
    assert next(iter(obs["pod_status"].values()))["phase"] == "NotObserved"


def test_output_preserves_files_and_truth(tmp_path, documents, case):
    source = tmp_path / "source" / "case-1"
    write(source / "apps/custom-name.yml", yaml_text(documents))
    write(source / "case.json", case)
    write(source / "truth.json", {"is_pending": True, "pending_labels": ["user-label"]})
    write(source / "res.txt", "historical output")
    assert discover(source.parent) == [source]
    _, docs, _, locations = load_case(source)
    target = tmp_path / "output"
    copy_case(source, target)
    docs[0]["spec"]["nodeSelector"] = {"slot": "present"}
    save_candidate(target, docs, locations, case, {"candidate_hash": digest(docs)})
    assert (target / "apps/custom-name.yml").is_file()
    assert read(target / "truth.json") == read(source / "truth.json")
    assert not (target / "res.txt").exists()
    assert "absent" in (source / "apps/custom-name.yml").read_text()


def test_old_cronjob_target_association():
    case = {"WaitingDeployPods": {"clock-cronpod": {"namespace": "default"}}}
    docs = [{"kind": "CronJob", "metadata": {"name": "clock"}}]
    assert associate_targets(case, docs)["WaitingDeployPods"]["clock-cronpod"]["workload"] == {
        "kind": "CronJob",
        "name": "clock",
    }


def test_historical_fixed_cases_are_explicit_opt_in(tmp_path):
    for name in ("a", "a_fixed"):
        write(tmp_path / name / "case.json", {})
    assert [p.name for p in discover(tmp_path)] == ["a"]
    assert [p.name for p in discover(tmp_path, include_fixed=True)] == ["a", "a_fixed"]


def test_background_ports_do_not_consume_an_extra_slot(environment):
    environment["ClusterState"]["ExistingPods"] = {"old": {"nodeName": "node", "cpu": 10, "mem": 8}}
    environment["ClusterState"]["Nodes"]["node"]["used_ports"] = [8080]
    docs = environment_documents(environment, [])
    node = next(x for x in docs if x["kind"] == "Node")
    holders = [x for x in docs if x["kind"] == "Pod"]
    assert node["status"]["capacity"]["pods"] == "21"
    assert len(holders) == 1
    assert holders[0]["spec"]["containers"][0]["ports"][0]["hostPort"] == 8080


def test_kubernetes_list_preserves_original_filename(tmp_path, documents, case):
    source = tmp_path / "source"
    write(source / "apps/list.yaml", yaml_text([{"apiVersion": "v1", "kind": "List", "items": documents}]))
    write(source / "case.json", case)
    _, docs, _, locations = load_case(source)
    assert docs == documents
    target = tmp_path / "out"
    copy_case(source, target)
    save_candidate(target, docs, locations, case, {})
    assert (target / "apps/list.yaml").is_file()
    assert load_case(target)[1] == documents


def test_cronjob_waits_for_declared_targets(documents):
    from confdr.shared.kubernetes import rebuild_case
    from confdr.validation.kwok import expected_targets

    cron = {
        "kind": "CronJob",
        "metadata": {"name": "timer"},
        "spec": {"jobTemplate": {"spec": {"parallelism": 2, "template": {"spec": documents[0]["spec"]}}}},
    }
    assert expected_targets([cron], 1) == (2, 0)
    assert len(rebuild_case([cron], {})["WaitingDeployPods"]) == 2
