import copy
import hashlib
import json

import pytest

from confdr.runtime.experiment import output_path, run_experiment, run_method, verify_dataset_manifest
from confdr.runtime.pipeline import diagnose
from confdr.runtime.repair import parse_candidate, repair_fault
from confdr.shared.io import digest, read, write, yaml_text
from confdr.shared.kubernetes import rebuild_case
from confdr.validation.deployment import evaluate_deployment


def test_frozen_dataset_manifest_detects_input_change(tmp_path):
    case = tmp_path / "dataset" / "case-1"
    case.mkdir(parents=True)
    payload = case / "truth.json"
    payload.write_text('{"is_pending": false}')
    manifest = {
        "schema_version": "d1-control-dataset-1",
        "cases": [{"case_id": "case-1", "files_sha256": {
            "truth.json": hashlib.sha256(payload.read_bytes()).hexdigest()
        }}],
    }
    verify_dataset_manifest(case.parent, [case], manifest)
    payload.write_text('{"is_pending": true}')
    with pytest.raises(ValueError, match="Frozen dataset files have changed"):
        verify_dataset_manifest(case.parent, [case], manifest)


class TransportDouble:
    """Stub only the model transport boundary; retain actual formal diagnosis and constraint solving."""

    def __init__(self):
        self.calls = []
        self.payloads = []
        self.rounds = 0
        self.reject_advice = False
        self.runtime_fault = False

    def complete(self, task, system, payload, schema):
        self.calls.append({"task": task})
        self.payloads.append(copy.deepcopy(payload))
        if task == "repair":
            self.rounds += 1
            if self.rounds > 1:
                assert payload["previous_validation"]["detection"]["formal"]["status"] == "unsat"
            docs = copy.deepcopy(payload["documents"])
            docs[0]["spec"]["nodeSelector"] = {"slot": "still-absent" if self.rounds == 1 else "present"}
            return {
                "summary": "Revise selector conditions",
                "confidence": 0.99,
                "fixed_yaml": yaml_text(docs),
            }
        if task == "suggest_repair":
            if self.reject_advice:
                raise RuntimeError("test unavailable")
            return {
                "summary": "Manual intervention required",
                "remaining_errors": ["Conditions remain unsatisfied"],
                "failure_analysis": payload["repair_report"]["reason"],
                "alternatives": [
                    {
                        "action": "Check node labels",
                        "rationale": "Current selector conditions do not match labels",
                        "prerequisites": ["Confirm the intended application nodes"],
                        "tradeoffs": "May change placement",
                        "validation": "Redeploy, collect observations and validate",
                        "certainty": "needs_confirmation",
                    }
                ],
                "missing_information": ["Application requirements"],
            }
        assert task == "diagnose"
        config = payload.get("assessment_scope") == "configuration_only"
        if config:
            passed = payload["documents"][0]["spec"].get("nodeSelector") == {"slot": "present"}
            return {
                "status": "healthy" if passed else "unknown",
                "summary": "Configuration recheck",
                "findings": [],
                "healthy_evidence": [
                    {"path": "/documents/0/spec/nodeSelector/slot", "value_json": '"present"'}
                ]
                if passed
                else [],
                "confidence": 0.99,
            }
        status = (
            "healthy" if payload.get("pod_info", payload.get("observations", {})).get("ready") else "unknown"
        )
        if self.runtime_fault and not payload.get("repair_context"):
            return {
                "status": "fault",
                "summary": "Runtime issue",
                "confidence": 0.99,
                "healthy_evidence": [],
                "findings": [
                    {
                        "category_ids": [],
                        "verification_scope": "runtime",
                        "summary": "Checks in a real environment are required",
                        "evidence": [
                            {
                                "path": "/case/WaitingDeployPods",
                                "value_json": json.dumps(payload["case"]["WaitingDeployPods"]),
                            }
                        ],
                    }
                ],
            }
        return {
            "status": status,
            "summary": "Test diagnosis",
            "findings": [],
            "healthy_evidence": [{"path": "/observations/ready", "value_json": "true"}]
            if status == "healthy"
            else [],
            "confidence": 0.99,
        }


class DeploymentDouble:
    def __init__(self):
        self.calls = []
        self.stale = False
        self.unavailable = False
        self.force_failed = False

    def probe(self, docs, environment, *, phase):
        self.calls.append(phase)
        if self.unavailable:
            return {"status": "error", "reason": "test infrastructure failure"}
        passed = not self.force_failed and docs[0]["spec"]["nodeSelector"] == {"slot": "present"}
        return {
            "status": "passed" if passed else "failed",
            "reason": "ready" if passed else "FailedScheduling",
            "case": rebuild_case(docs, environment),
            "observations": {
                "ready": passed,
                "candidate_hash": "stale" if self.stale else digest(docs),
                "collection": {"scope": "kwok_test"},
                "pod_status": {
                    name: {
                        "phase": "Running" if passed else "Pending",
                        "namespace": p.get("namespace", "default"),
                        "nodeName": "node" if passed else "",
                        "conditions": [{"type": "Ready", "status": "True" if passed else "False"}],
                    }
                    for name, p in rebuild_case(docs, environment)["WaitingDeployPods"].items()
                },
            },
        }


def sample(tmp_path, documents, case, name="source"):
    source = tmp_path / name
    write(source / "apps/app.yml", yaml_text(documents))
    write(source / "case.json", case)
    write(source / "truth.json", {"status": "fault", "category_ids": ["test.constraint"]})
    return source


@pytest.mark.parametrize("configuration_only", [False, True])
def test_unsat_skips_llm_and_preserves_formal_conflicts(case, documents, catalog, configuration_only):
    class ForbiddenDiagnosis:
        def complete(self, *args, **kwargs):
            pytest.fail("UNSAT must not call LLM-assisted detection")

    report = diagnose(
        case,
        catalog,
        ForbiddenDiagnosis(),
        documents=documents,
        configuration_only=configuration_only,
        repair_context={"original_documents": documents} if configuration_only else None,
    )
    assert report["formal"]["status"] == "unsat"
    assert report["status"] == "fault" and report["is_fault"] is True
    assert report["route"] == "formal" and report["llm"] is None
    assert report["error"] is None
    assert report["category_ids"] == ["test.constraint"]
    assert report["evidence"] == report["formal"]["core"] and report["evidence"]


@pytest.mark.parametrize("formal_status", ["sat", "unknown"])
@pytest.mark.parametrize("configuration_only", [False, True])
def test_only_unknown_calls_llm(case, documents, catalog, formal_status, configuration_only):
    documents[0]["spec"]["nodeSelector"] = {"slot": "present"}
    case = rebuild_case(documents, case)
    if formal_status == "unknown":
        # Remove independent validation material so real formal detection cannot verify the rules, without fabricating solver results.
        catalog.pop("verification_material")
    llm = TransportDouble()
    report = diagnose(
        case,
        catalog,
        llm,
        documents=documents,
        configuration_only=configuration_only,
        repair_context={"original_documents": documents} if configuration_only else None,
    )
    assert report["formal"]["status"] == formal_status
    assert [c["task"] for c in llm.calls] == (["diagnose"] if formal_status == "unknown" else [])
    if formal_status == "unknown":
        assert set(llm.payloads[0]) == {"case", "categories"}
        assert report["route"] == "formal+llm" and report["llm"] is not None
    assert report["status"] == ("healthy" if formal_status == "sat" else "unknown")


def test_each_candidate_calls_same_detector(case, documents, catalog):
    llm = TransportDouble()
    original = diagnose(case, catalog, llm, documents=documents)
    result = repair_fault(case, documents, original, catalog, llm, observations={}, max_rounds=2)
    assert result["status"] == "candidate_accepted"
    assert result["correctness_verified"] is False
    assert result["stop_reason"] == "detection_passed" and result["rounds_used"] == 2
    assert [c["task"] for c in llm.calls] == ["repair", "repair"]
    assert result["attempts"][0]["validation"]["detection"]["formal"]["status"] == "unsat"
    assert result["attempts"][1]["validation"]["detection"]["formal"]["status"] == "sat"
    assert all(
        a["validation"]["detection"]["assessment_scope"] == "formal_configuration" for a in result["attempts"]
    )
    assert all("kwok" not in json.dumps(p).lower() for p in llm.payloads)
    assert documents[0]["spec"]["nodeSelector"] == {"slot": "absent"}


def test_runtime_problem_not_blocked_by_absence_of_kwok(case, documents, catalog):
    class RuntimeRepair(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                candidate = copy.deepcopy(payload["documents"])
                candidate[0]["spec"]["containers"][0]["image"] = "example.invalid/app:2"
                return {"summary": "Revise image", "confidence": 0.99, "fixed_yaml": yaml_text(candidate)}
            return super().complete(task, system, payload, schema)

    documents[0]["spec"]["nodeSelector"] = {"slot": "present"}
    case = rebuild_case(documents, case)
    llm = RuntimeRepair()
    llm.runtime_fault = True
    evidence = catalog.pop("verification_material")
    original = diagnose(case, catalog, llm, documents=documents)
    assert original["formal"]["status"] == "unknown" and original["status"] == "fault"
    catalog["verification_material"] = evidence
    result = repair_fault(case, documents, original, catalog, llm, observations={}, max_rounds=2)
    assert result["status"] == "candidate_accepted"
    assert result["requires_runtime_evaluation"] is True
    assert result["correctness_verified"] is False


def test_failed_method_keeps_complete_original_dataset(tmp_path, documents, case, catalog):
    source = tmp_path / "source"
    for name in ("one", "nested/two"):
        sample(source, documents, case, name)
        write(source / name / "truth.json", {"is_pending": True, "pending_labels": ["private-human-label"]})
        write(source / name / "res.txt", "historical output")
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    llm = TransportDouble()

    def progress(_):
        llm.rounds = 0

    summary = run_method(source, tmp_path / "out", catalog, llm, options={"max_rounds": 1}, progress=progress)
    assert summary["finished"] and summary["statuses"] == {"not_accepted": 2}
    for name in ("one", "nested/two"):
        target = tmp_path / "out/dataset" / name
        for file in ("apps/app.yml", "case.json", "truth.json"):
            assert (target / file).read_bytes() == (source / name / file).read_bytes()
        assert (target / "advice.md").is_file()
        assert "Verification Result" in (target / "res.txt").read_text()
        assert not (target / "deployment.json").exists()
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert "private-human-label" not in json.dumps(llm.payloads)
    assert summary["evaluation"]["overall"]["category_exact_match"] is None


def test_method_only_outputs_candidate_without_invented_observations(tmp_path, documents, case, catalog):
    source = sample(tmp_path, documents, case)
    model = TransportDouble()
    summary = run_method(
        source,
        tmp_path / "out",
        catalog,
        model,
        options={"max_rounds": 2},
        progress=lambda _: None,
    )
    target = tmp_path / "out/dataset/source"
    assert summary["stage"] == "method_completed"
    assert summary["statuses"] == {"candidate_accepted": 1}
    # Human-readable files describe the original failure even when accepted manifests are saved.
    first = (target / "res_single_round.txt").read_text()
    aggregate = (target / "res.txt").read_text()
    assert "UNSAT:" in first and "Category labels: ['test.constraint']" in first
    assert "--- Round 1 ---" in aggregate and "--- Round 2 ---" in aggregate
    assert "Overall formal result: UNSAT" in aggregate
    assert "LLM auxiliary detection: skipped" in aggregate
    assert "present" in (target / "apps/app.yml").read_text()
    assert read(target / "case.json")["collection"]["observed"] is False
    assert read(target / "pod_info.json")["collection"]["scope"] == "not_collected"
    assert read(target / "truth.json")["status"] == "fault"
    assert not (target / "deployment.json").exists()
    assert all("observations" not in payload and "pod_info" not in payload for payload in model.payloads)
    assert "observations" not in read(target / "result.json")["source_snapshot"]


def test_deployment_failure_cannot_change_method_or_trigger_repair(tmp_path, documents, case, catalog):
    source = sample(tmp_path, documents, case)
    llm = TransportDouble()
    run_method(source, tmp_path / "out", catalog, llm, options={"max_rounds": 2}, progress=lambda _: None)
    # Evaluation summaries are refreshed; finalized method inputs/results must remain byte-identical.
    before = {
        p: p.read_bytes()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file() and p.name not in {"paper-metrics.json", "paper-metrics.txt"}
    }
    rounds = llm.rounds
    deploy = DeploymentDouble()
    deploy.force_failed = True
    summary = evaluate_deployment(tmp_path / "out", deploy, llm=llm, progress=lambda _: None)
    assert deploy.calls == ["original", "evaluation", "evaluation_prefix_1"]
    assert llm.rounds == rounds
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert summary["statuses"] == {"failed": 1}
    report = read(tmp_path / "out/dataset/source/deployment.json")
    assert report["method_status"] == "candidate_accepted"
    assert report["repair_correctness"] == "not_determined"
    assert report["feedback_to_method"] is False
    assert (tmp_path / "out/dataset/source/deployment-advice.md").is_file()
    assert report["candidate"]["observations"]["ready"] is False


def test_infrastructure_errors_only_affect_evaluation(tmp_path, documents, case, catalog):
    source = sample(tmp_path, documents, case)
    llm, deploy = TransportDouble(), DeploymentDouble()
    llm.reject_advice = deploy.unavailable = True
    summary = run_experiment(
        source, tmp_path / "out", catalog, llm, deploy, options={"max_rounds": 2}, progress=lambda _: None
    )
    assert summary["statuses"] == {"candidate_accepted": 1}
    assert summary["deployment_statuses"] == {"error": 1}
    assert llm.rounds == 2
    report = read(tmp_path / "out/dataset/source/deployment.json")
    assert report["manual_review"]["source"] == "local_fallback"
    assert read(tmp_path / "out/deployment-summary.json")["candidate_deployment_rate"] is None


def test_whole_method_batch_finishes_before_first_deployment(tmp_path, documents, case, catalog):
    source = tmp_path / "source"
    sample(source, documents, case, "one")
    sample(source, documents, case, "two")
    llm = TransportDouble()

    class GuardedDeployer(DeploymentDouble):
        def probe(self, docs, environment, *, phase):
            method = read(tmp_path / "out/experiment.json")
            assert method["stage"] == "method_completed" and method["completed"] == 2
            assert (tmp_path / "out/dataset/one/result.json").is_file()
            assert (tmp_path / "out/dataset/two/result.json").is_file()
            return super().probe(docs, environment, phase=phase)

    def progress(text):
        if text.startswith("["):
            llm.rounds = 0

    summary = run_experiment(
        source,
        tmp_path / "out",
        catalog,
        llm,
        GuardedDeployer(),
        options={"max_rounds": 2},
        progress=progress,
    )
    assert summary["statuses"] == {"candidate_accepted": 2}
    assert summary["deployment_statuses"] == {"passed": 2}


def test_stale_evaluation_observations_not_saved_as_valid(tmp_path, documents, case, catalog):
    source = sample(tmp_path, documents, case)
    run_method(
        source,
        tmp_path / "out",
        catalog,
        TransportDouble(),
        options={"max_rounds": 2},
        progress=lambda _: None,
    )
    deploy = DeploymentDouble()
    deploy.stale = True
    summary = evaluate_deployment(tmp_path / "out", deploy, progress=lambda _: None)
    assert summary["statuses"] == {"error": 1}
    assert "fingerprint mismatch" in read(tmp_path / "out/dataset/source/deployment.json")["reason"]
    assert read(tmp_path / "out/dataset/source/result.json")["repair"]["status"] == "candidate_accepted"


def test_evaluation_requires_unchanged_finished_method(tmp_path):
    write(tmp_path / "experiment.json", {"finished": False})
    deploy = DeploymentDouble()
    with pytest.raises(ValueError, match="method stage to be complete"):
        evaluate_deployment(tmp_path, deploy)
    assert not deploy.calls


def test_output_cannot_overwrite_input(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError):
        output_path(source, source / "fixed")
    with pytest.raises(ValueError):
        output_path(source, source)


def test_candidate_cannot_remove_workload_or_probe(documents):
    with pytest.raises(ValueError):
        parse_candidate("", documents)
    original = copy.deepcopy(documents)
    original[0]["spec"]["containers"][0]["readinessProbe"] = {"exec": {"command": ["true"]}}
    with pytest.raises(ValueError, match="probes"):
        parse_candidate(yaml_text(documents), original)


def test_complete_formal_sat_does_not_require_runtime_observations(case, documents, catalog):
    for p in case["WaitingDeployPods"].values():
        p["nodeSelector"] = {"slot": "present"}
    result = diagnose(case, catalog, TransportDouble(), observations={"ready": True}, documents=documents)
    assert result["formal"]["status"] == "sat" and result["status"] == "healthy"
    assert result["error"] is None and result["llm"] is None


def test_method_calls_public_detection_entry_each_round(monkeypatch, case, documents, catalog):
    import confdr.runtime.pipeline as pipeline

    real = pipeline.diagnose
    seen = []

    def wrapped(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "diagnose", wrapped)
    llm = TransportDouble()
    original = real(case, catalog, llm, documents=documents)
    result = repair_fault(case, documents, original, catalog, llm, observations={}, max_rounds=2)
    assert result["status"] == "candidate_accepted"
    assert len(seen) == 2
    assert all(call["configuration_only"] and call["observations"] == {} for call in seen)


def test_formal_sat_is_not_vetoed_by_model(case, documents, catalog):
    class MoreFaults(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                if self.rounds == 2:
                    assert payload["previous_validation"]["detection"]["formal"]["status"] == "sat"
                    assert payload["previous_validation"]["detection"]["status"] == "fault"
                candidate = copy.deepcopy(payload["documents"])
                candidate[0]["spec"]["nodeSelector"] = {"slot": "present"}
                candidate[0]["spec"]["containers"][0]["image"] = (
                    "wrong-image:1" if self.rounds == 1 else "fixed-image:1"
                )
                return {
                    "summary": "Revise in each round",
                    "confidence": 0.99,
                    "fixed_yaml": yaml_text(candidate),
                }
            if (
                task == "diagnose"
                and payload.get("assessment_scope") == "configuration_only"
                and self.rounds == 1
            ):
                return {
                    "status": "fault",
                    "summary": "An independent issue remains",
                    "confidence": 0.99,
                    "healthy_evidence": [],
                    "findings": [
                        {
                            "category_ids": [],
                            "verification_scope": "runtime",
                            "summary": "The new candidate image needs correction",
                            "evidence": [
                                {
                                    "path": "/documents/0/spec/containers/0/image",
                                    "value_json": '"wrong-image:1"',
                                }
                            ],
                        }
                    ],
                }
            return super().complete(task, system, payload, schema)

    llm = MoreFaults()
    original = diagnose(case, catalog, llm, documents=documents)
    result = repair_fault(case, documents, original, catalog, llm, observations={}, max_rounds=2)
    assert result["status"] == "candidate_accepted" and llm.rounds == 1
    assert result["attempts"][0]["validation"]["detection"]["status"] == "healthy"
    assert not any(c["task"] == "diagnose" for c in llm.calls)


def test_old_observations_cannot_prove_candidate_accepted(case, documents, catalog):
    for p in case["WaitingDeployPods"].values():
        p["nodeSelector"] = {"slot": "present"}

    class OldEvidence(TransportDouble):
        def complete(self, *args, **kwargs):
            return {
                "status": "healthy",
                "summary": "Old status cannot be used",
                "confidence": 0.99,
                "findings": [],
                "healthy_evidence": [
                    {"path": "/repair_context/original_observations/ready", "value_json": "true"}
                ],
            }

    report = diagnose(
        case,
        catalog,
        OldEvidence(),
        documents=documents,
        configuration_only=True,
        repair_context={"original_observations": {"ready": True}},
    )
    assert report["status"] == "healthy" and report["error"] is None and report["llm"] is None


def test_run_cli_does_not_require_deployment_commands(tmp_path, monkeypatch, documents, case, catalog):
    import shutil

    import confdr.cli
    import confdr.shared.catalog
    import confdr.shared.llm

    source = sample(tmp_path, documents, case)
    model = TransportDouble()
    model.preflight = lambda: None
    monkeypatch.setattr(confdr.shared.catalog, "read_bundle", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(confdr.shared.llm, "LLM", lambda *args, **kwargs: model)

    def forbidden(*args, **kwargs):
        raise AssertionError("The method must not check deployment commands")

    monkeypatch.setattr(shutil, "which", forbidden)
    assert (
        confdr.cli.main(
            [
                "run",
                "--dataset",
                str(source),
                "--rules",
                str(tmp_path / "rules.yaml"),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert read(tmp_path / "out/experiment.json")["statuses"] == {"candidate_accepted": 1}


@pytest.mark.parametrize("limit", [1, 3, 5])
def test_unresolved_candidates_stop_at_configured_limit(case, documents, catalog, limit):
    class Unresolved(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                candidate = copy.deepcopy(payload["documents"])
                candidate[0]["spec"]["nodeSelector"] = {"slot": f"missing-{self.rounds}"}
                return {"summary": "Still unresolved", "confidence": 0.99, "fixed_yaml": yaml_text(candidate)}
            return super().complete(task, system, payload, schema)

    llm = Unresolved()
    detection = diagnose(case, catalog, llm, documents=documents)
    result = repair_fault(case, documents, detection, catalog, llm, observations={}, max_rounds=limit)
    assert llm.rounds == result["rounds_used"] == result["max_rounds"] == limit
    assert len(result["attempts"]) == limit
    assert result["stop_reason"] == "max_rounds_reached"
    assert result["status"] == "not_accepted" and result["candidate"] is None
    assert "maximum repair attempts" in result["reason"] and result["last_failure_reason"]
    assert result["manual_review"]["advice"]["alternatives"]
    assert sum(c["task"] == "diagnose" for c in llm.calls) == 0


@pytest.mark.parametrize("mode", ["invalid_yaml", "model_error", "no_candidate"])
def test_invalid_and_failed_attempts_are_bounded(case, documents, catalog, mode):
    class Failing(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                if mode == "model_error":
                    raise RuntimeError("API failure")
                return {
                    "summary": "No candidate" if mode == "no_candidate" else "Invalid manifest",
                    "confidence": 0.99,
                    "fixed_yaml": "" if mode == "no_candidate" else "spec: [",
                }
            return super().complete(task, system, payload, schema)

    llm = Failing()
    detection = diagnose(case, catalog, llm, documents=documents)
    result = repair_fault(case, documents, detection, catalog, llm, observations={}, max_rounds=3)
    assert result["rounds_used"] == llm.rounds == (1 if mode == "no_candidate" else 3)
    assert result["stop_reason"] == ("no_candidate" if mode == "no_candidate" else "max_rounds_reached")


@pytest.mark.parametrize("invalid", [0, -1, 11, True, 2.5, "5", None])
def test_invalid_limits_rejected_before_any_model_call(case, documents, catalog, invalid):
    llm = TransportDouble()
    with pytest.raises(ValueError, match="an integer in 1–10"):
        repair_fault(case, documents, {}, catalog, llm, observations={}, max_rounds=invalid)
    assert llm.calls == [] and llm.rounds == 0
