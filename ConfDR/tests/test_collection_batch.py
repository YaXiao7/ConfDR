import copy
import json

import pytest

from confdr.cli import main, parser
from confdr.collection.service import collect_case, collect_dataset
from confdr.shared.io import read, write, yaml_text
from confdr.validation.kwok import KwokValidator


def make_case(path, documents):
    write(path / "apps/app.yaml", yaml_text(documents))
    write(path / "truth.json", {"status": "fault"})
    write(path / "pod_info.json", {"old": True})


def fake_probe(self, documents, case, *, phase):
    return {
        "status": "failed",
        "reason": "pending",
        "case": {"WaitingDeployPods": {}, "collection": {"scope": "kwok_simulated_control_plane"}},
        "observations": {"pod_status": {"app": {"phase": "Pending"}}},
    }


def test_batch_cli_isolates_errors_and_preserves_hierarchy(tmp_path, documents, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "out"
    for name in ("a/one", "b/bad", "c/one", "d/one_fixed"):
        make_case(source / name, documents)
    write(source / "b/bad/apps/app.yaml", "[invalid YAML")
    calls = []

    def probe(self, docs, case, *, phase):
        calls.append(copy.deepcopy(docs))
        return fake_probe(self, docs, case, phase=phase)

    monkeypatch.setattr(KwokValidator, "probe", probe)
    assert main(["collect", "--dataset", str(source), "--kwok", "--output", str(output)]) == 2
    report = read(output / "collection-batch.json")
    assert [r["case_id"] for r in report["cases"]] == ["a/one", "b/bad", "c/one"]
    assert report["counts"]["collected"] == 2
    assert report["counts"]["deployment_failed"] == 2
    assert report["counts"]["error"] == 1
    assert len(calls) == 2
    assert "Invalid manifest YAML" in report["cases"][1]["reason"]
    assert not (output / "dataset/b/bad").exists()
    for name in ("a/one", "c/one"):
        assert read(output / "dataset" / name / "truth.json") == {"status": "fault"}
        assert read(output / "dataset" / name / "pod_info.json")["pod_status"]["app"]["phase"] == "Pending"
        assert read(source / name / "pod_info.json") == {"old": True}


def test_pending_is_successful_collection_and_single_case_still_works(tmp_path, documents, monkeypatch):
    source = tmp_path / "case"
    make_case(source, documents)
    monkeypatch.setattr(KwokValidator, "probe", fake_probe)
    assert main(["collect", "--case", str(source), "--kwok", "--output", str(tmp_path / "single")]) == 0
    assert main(["collect", "--dataset", str(source), "--kwok", "--output", str(tmp_path / "batch")]) == 0
    assert (tmp_path / "batch/dataset/case/case.json").exists()


def test_infrastructure_error_does_not_prevent_next_case(tmp_path, documents, monkeypatch):
    source = tmp_path / "source"
    for name in ("a", "b"):
        make_case(source / name, documents)
    calls = 0

    def probe(self, docs, case, *, phase):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "error", "reason": "cluster timeout"}
        return fake_probe(self, docs, case, phase=phase)

    monkeypatch.setattr(KwokValidator, "probe", probe)
    report = collect_dataset(source, tmp_path / "out", {}, kwok=True)
    assert [r["status"] for r in report["cases"]] == ["error", "collected"]
    assert "cluster timeout" in report["cases"][0]["reason"]


def test_interrupt_keeps_progress_and_does_not_publish_partial_case(tmp_path, documents, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "out"
    for name in ("a", "b"):
        make_case(source / name, documents)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(KwokValidator, "probe", interrupt)
    with pytest.raises(KeyboardInterrupt):
        collect_dataset(source, output, {}, kwok=True)
    report = read(output / "collection-batch.json")
    assert report["status"] == "interrupted"
    assert [r["status"] for r in report["cases"]] == ["interrupted", "pending"]
    assert not (output / "dataset/a").exists()


def test_partial_save_is_not_published(tmp_path, documents, monkeypatch):
    import confdr.collection.service as service

    source, output = tmp_path / "source", tmp_path / "out"
    make_case(source, documents)
    monkeypatch.setattr(KwokValidator, "probe", fake_probe)

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(service, "save_candidate", fail)
    with pytest.raises(OSError, match="write failed"):
        collect_case(source, output, {}, kwok=True)
    assert not output.exists()
    assert not list(tmp_path.glob(".confdr-collect-*"))


def test_output_safety_and_fixed_opt_in(tmp_path, documents, monkeypatch):
    source = tmp_path / "source"
    make_case(source / "a_fixed", documents)
    monkeypatch.setattr(KwokValidator, "probe", fake_probe)
    with pytest.raises(ValueError, match="contain each other"):
        collect_dataset(source, source / "out", {}, kwok=True, include_fixed=True)
    output = tmp_path / "out"
    report = collect_dataset(source, output, {}, kwok=True, include_fixed=True)
    assert report["counts"]["collected"] == 1
    with pytest.raises(ValueError, match="already exists"):
        collect_dataset(source, output, {}, kwok=True, include_fixed=True)


def test_case_and_dataset_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parser().parse_args(["collect", "--case", "a", "--dataset", "b", "--kwok", "--output", "out"])


def test_require_running_waits_for_healthy_phase_and_fails_batch_on_pending(
    tmp_path, documents, monkeypatch
):
    source = tmp_path / "source"
    make_case(source / "pending", documents)
    phases = []

    def probe(self, docs, case, *, phase):
        phases.append(phase)
        return fake_probe(self, docs, case, phase=phase)

    monkeypatch.setattr(KwokValidator, "probe", probe)
    output = tmp_path / "out"
    assert main([
        "collect", "--dataset", str(source), "--kwok", "--require-running",
        "--output", str(output),
    ]) == 2
    assert phases == ["candidate"]
    report = read(output / "collection-batch.json")
    assert report["require_running"] is True
    assert report["counts"]["deployment_failed"] == 1


def test_healthy_phase_retries_transient_pvc_scheduling_event(monkeypatch):
    pending = {
        "pod_status": {"default/app": {"phase": "Pending", "nodeName": None, "conditions": []}},
        "pod_events": {"default/app": [{"reason": "FailedScheduling", "message": "unbound PVC"}]},
        "pvc_status": {"default/claim": {"phase": "Pending"}},
    }
    running = {
        "pod_status": {"default/app": {
            "phase": "Running", "nodeName": "node-1",
            "conditions": [{"type": "Ready", "status": "True"}],
        }},
        "pod_events": {"default/app": pending["pod_events"]["default/app"]},
        "pvc_status": {"default/claim": {"phase": "Bound"}},
    }
    observations = iter([pending, running])
    validator = KwokValidator({"poll_interval_seconds": 1}, clock=lambda: 0,
                              sleeper=lambda _: None)
    monkeypatch.setattr(validator, "_collect", lambda *_: next(observations))
    result = validator._wait(None, "token", 1, 1, "candidate")
    assert result["status"] == "passed"
    assert result["observations"] is running


def test_original_collection_allows_startup_failure_to_resolve(monkeypatch):
    pending = {
        "pod_status": {"default/app": {"phase": "Pending", "nodeName": None, "conditions": []}},
        "pod_events": {"default/app": [{"reason": "FailedScheduling", "message": "no affinity peer yet"}]},
        "pvc_status": {},
    }
    running = copy.deepcopy(pending)
    running["pod_status"]["default/app"].update(
        phase="Running", nodeName="node-1", conditions=[{"type": "Ready", "status": "True"}],
    )
    observations = iter([pending, running])
    validator = KwokValidator(clock=lambda: 0, sleeper=lambda _: None)
    monkeypatch.setattr(validator, "_collect", lambda *_: next(observations))
    result = validator._wait(None, "token", 1, 0, "original")
    assert result["status"] == "passed"
    assert result["observations"] is running


def test_original_collection_waits_for_all_targets_and_stable_fault(monkeypatch):
    pending = {
        "pod_status": {"default/one": {"phase": "Pending", "nodeName": None, "conditions": []}},
        "pod_events": {"default/one": [{"reason": "FailedScheduling", "message": "hard affinity conflict"}]},
        "pvc_status": {},
    }
    complete = copy.deepcopy(pending)
    complete["pod_status"]["default/two"] = copy.deepcopy(pending["pod_status"]["default/one"])
    now, polls = [0.0], []
    validator = KwokValidator(
        {"poll_interval_seconds": 1, "original_failure_settle_seconds": 3},
        clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    def collect(*_):
        polls.append(now[0])
        return pending if len(polls) == 1 else complete

    monkeypatch.setattr(validator, "_collect", collect)
    result = validator._wait(None, "token", 2, 0, "original")
    assert result["status"] == "failed"
    assert len(result["observations"]["pod_status"]) == 2
    assert now[0] >= 4
    assert result["failure_settled_seconds"] >= 3


def test_environment_pvc_binds_before_workload(monkeypatch):
    import yaml

    now = [0.0]
    validator = KwokValidator(
        {"poll_interval_seconds": 1, "validation_timeout_seconds": 5},
        clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    applied = []
    claim_polls = iter(["Pending", "Bound"])

    def kubectl(_, args, *, stdin=None):
        if args[:2] == ["apply", "-f"]:
            applied.append([doc["kind"] for doc in yaml.safe_load_all(stdin)])
            return ""
        if args[:2] == ["get", "pvc"]:
            return json.dumps({"items": [{
                "metadata": {"namespace": "default", "name": "claim"},
                "status": {"phase": next(claim_polls)},
            }]})
        raise AssertionError(args)

    monkeypatch.setattr(validator, "_kubectl", kubectl)
    monkeypatch.setattr(validator, "_wait_for_nodes", lambda *_: None)
    documents = [
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "claim"}},
        {"kind": "PersistentVolume", "metadata": {"name": "volume"}},
        {"kind": "StorageClass", "metadata": {"name": "manual"}},
    ]
    validator._restore_environment(None, documents)
    assert applied == [["PersistentVolume", "StorageClass"], ["PersistentVolumeClaim"]]
    assert validator._wait_for_environment_claims(None, documents) is True
    assert now[0] == 1.0
