import copy

import pytest
from test_experiment import DeploymentDouble, TransportDouble, sample

from confdr.runtime.experiment import run_method
from confdr.runtime.paper_metrics import (
    call_metrics,
    detected_quality,
    detection_metrics,
    evaluate_metrics,
    fixed_cohort_repair_metrics,
    group_metrics,
    group_repair_metrics,
    measurement,
    predictions,
    repair_metrics,
    retry_trajectory,
)
from confdr.runtime.solver import solve
from confdr.shared.io import digest, read, write, yaml_text
from confdr.validation.deployment import evaluate_deployment
from confdr.validation.repair_metrics import observed_pending, postcheck, select_candidate


def detection_row(actual, status, truth_labels=(), predicted_labels=(), *, recorded=True, annotated=True):
    return {
        "truth": {
            "is_pending": actual,
            "category_ids": list(truth_labels),
            "category_labelled": annotated,
            "status": "fault" if actual else "healthy",
        },
        "predictions": {
            "single_round": {"status": status, "category_ids": list(predicted_labels), "recorded": recorded}
        },
    }


def test_binary_pending_metrics_and_truth_positive_jaccard():
    cases = [
        detection_row(True, "fault", ["S1", "S2"], ["S1", "S1"]),
        detection_row(True, "healthy", ["S1"]),
        detection_row(False, "fault", [], ["R1"]),
        detection_row(False, "healthy"),
        detection_row(True, "unknown", ["P1"]),
        detection_row(False, "unknown"),
    ]
    m = detection_metrics(cases, "single_round")
    assert [m[k] for k in ("tp", "fp", "fn", "tn")] == [1, 1, 1, 1]
    assert m["precision"] == m["recall"] == m["f1"] == m["accuracy"] == 0.5
    assert m["coverage"] == pytest.approx(4 / 6)
    assert m["recall_including_abstentions"] == m["accuracy_including_abstentions"] == pytest.approx(1 / 3)
    assert m["avg_jaccard"] == pytest.approx(0.5 / 3)
    assert m["jaccard_scored_cases"] == 3


def test_missing_truth_or_result_not_an_empty_label_set():
    rows = [
        detection_row(True, "fault", ["S1"], ["S1"], annotated=False),
        detection_row(True, "unknown", ["S1"], recorded=False),
        detection_row(None, "fault", ["S1"], ["S1"]),
    ]
    m = detection_metrics(rows, "single_round")
    assert m["avg_jaccard"] is None and m["jaccard_missing_cases"] == 2
    assert m["unlabelled"] == 1


def test_groups_keep_multilabel_membership_and_count_controls_as_rest():
    rows = [
        detection_row(True, "fault", ["R1", "S2"], ["R1"]),
        detection_row(True, "unknown", ["S1"]),
        detection_row(False, "fault", [], ["S2"]),
    ]
    groups = group_metrics(rows, "single_round")
    assert groups["R"]["truth_positive"] == 1
    assert groups["R"]["tp"] == 1
    assert groups["S"]["truth_positive"] == 2
    assert groups["S"]["fn"] == 1
    assert groups["S"]["fp"] == 1
    assert groups["S"]["unknown_positive"] == 1
    assert groups["S"]["recall_including_abstentions"] == 0


def test_fixed_cohort_counts_missed_detection_and_requires_accepted_output():
    rows = [
        {"truth": {"is_pending": True}, "repair_entered": False,
         "candidate": {"selection": "not_in_repair_cohort"}, "deployment_status": "no_candidate", "pending": None},
        {"truth": {"is_pending": True}, "repair_entered": True,
         "candidate": {"selection": "accepted_output"}, "deployment_status": "passed", "pending": False},
        {"truth": {"is_pending": True}, "repair_entered": True,
         "candidate": {"selection": "final_proposal"}, "deployment_status": "passed", "pending": False},
        {"truth": {"is_pending": False}, "repair_entered": False,
         "candidate": {"selection": "not_in_repair_cohort"}, "deployment_status": "no_candidate", "pending": None},
    ]
    m = fixed_cohort_repair_metrics(rows)
    assert m["n"] == 3 and m["successes"] == 1 and m["success_rate"] == pytest.approx(1 / 3)


def test_formal_quality_requires_paired_snapshots():
    row = repair_row(["R1"], ["R2"])
    row.update(original_quality_status="scored", original_categories=["R1", "R2"])
    m = repair_metrics([row])
    assert m["configuration_violation_reduction_rate"] == 0.5
    assert m["formal_average_introduced_errors"] == 0
    row.pop("original_categories")
    row["original_quality_status"] = "missing_original_postcheck"
    assert repair_metrics([row])["configuration_violation_reduction_rate"] is None


def test_retry_prefix_does_not_borrow_later_accepted_candidate(documents):
    result = candidate_result(documents, "spec: [")
    result["repair"]["attempts"][0]["validation"] = {"status": "rejected"}
    result["repair"]["attempts"].append({
        "proposal": {"fixed_yaml": yaml_text(documents)},
        "validation": {"status": "accepted"},
    })
    result["output_action"] = "method_candidate"
    result["model_calls"] = [
        {"task": "repair", "seconds": 2, "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
        {"task": "repair", "seconds": 4, "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}},
    ]
    curve = retry_trajectory(result, documents)
    assert curve["1"]["accepted"] is False and curve["1"]["parse_success"] is False
    assert curve["1"]["repair_total_tokens"] == 13
    assert curve["3"]["accepted"] is True and curve["3"]["repair_total_tokens"] == 39


def test_original_unsat_not_overridden_by_filtered_sat_and_runtime_scope():
    core = [{"category_id": None}, {"category_id": "S1"}, {"category_id": "S1"}]
    d = {
        "status": "fault",
        "category_ids": ["S1"],
        "formal": {
            "status": "unsat",
            "solver_status": "unsat",
            "terminal_status": "sat",
            "formal_complete": False,
            "initial_core": core,
            "core": core + [{"category_id": "R1"}],
        },
    }
    views = predictions(d)
    assert views["single_round"]["category_ids"] == ["S1"]
    assert views["all_conflicts"]["status"] == "fault"
    assert views["all_conflicts"]["category_ids"] == ["R1", "S1"]
    d = {
        "status": "fault",
        "category_ids": [],
        "formal": {"status": "unknown"},
        "llm": {"findings": [{"verification_scope": "runtime", "category_ids": []}]},
    }
    assert predictions(d)["combined_detection"]["status"] == "unknown"


def test_repair_cost_includes_recheck_diagnosis_but_not_initial_detection_or_advice(documents):
    result = candidate_result(documents, "spec: [")
    result["repair"]["attempts"][0]["validation"] = {"status": "rejected"}
    result["repair"]["attempts"].append({
        "proposal": {"fixed_yaml": yaml_text(documents)},
        "validation": {"status": "accepted"},
    })
    result["output_action"] = "method_candidate"
    usage = lambda n: {"prompt_tokens": n, "completion_tokens": 1, "total_tokens": n + 1}  # noqa: E731
    result["model_calls"] = [
        {"task": "diagnose", "seconds": 100, "usage": usage(100)},
        {"task": "repair", "seconds": 2, "usage": usage(10)},
        {"task": "diagnose", "seconds": 3, "usage": usage(5)},
        {"task": "repair", "seconds": 4, "usage": usage(20)},
        {"task": "suggest_repair", "seconds": 50, "usage": usage(50)},
    ]
    calls = call_metrics(result)
    assert calls["calls"] == 2 and calls["recheck_calls"] == 1
    assert calls["seconds"] == 9 and calls["prompt_tokens"] == 35
    curve = retry_trajectory(result, documents)
    assert curve["1"]["repair_llm_seconds"] == 5 and curve["1"]["repair_prompt_tokens"] == 15
    assert curve["3"]["repair_llm_seconds"] == 9


def test_first_core_view_keeps_llm_assisted_detection_for_formal_unknown():
    d = {
        "status": "fault",
        "category_ids": ["S4"],
        "formal": {"status": "unknown", "solver_status": "sat", "formal_complete": False, "initial_core": [], "core": []},
        "llm": {"findings": [{"verification_scope": "control_plane", "category_ids": ["S4"]}]},
    }
    views = predictions(d)
    assert views["single_round"]["status"] == "fault"
    assert views["single_round"]["category_ids"] == ["S4"]
    assert views["all_conflicts"]["status"] == "unknown"


def test_detection_views_apply_category_attribution():
    claim = {"c": "ns/data"}
    path = {"constraint_id": "c1", "category_id": "S3", "requirement_ids": ["S3.path"], "entities": claim}
    dynamic = {"constraint_id": "c2", "category_id": "S2", "entities": claim}
    d = {
        "status": "fault",
        "category_ids": ["S2"],
        "formal": {
            "status": "unsat",
            "solver_status": "unsat",
            "formal_complete": True,
            "initial_core": [path, dynamic],
            "core": [path, dynamic, {"constraint_id": "c3", "category_id": "R1"}],
        },
    }
    views = predictions(d)
    # S3 path selection blocked by S2 on the same claim is a dependency, not a separate finding.
    assert views["single_round"]["category_ids"] == ["S2"]
    assert views["all_conflicts"]["category_ids"] == ["R1", "S2"]


def test_exact_match_scores_pending_category_sets():
    cases = [
        detection_row(True, "fault", ["R1", "R2"], ["R1", "R2"]),
        detection_row(True, "fault", ["R1", "R2"], ["R1"]),
        detection_row(True, "unknown", ["S4"], []),
        detection_row(False, "healthy"),
    ]
    m = detection_metrics(cases, "single_round")
    assert m["exact_match"] == pytest.approx(1 / 3)  # healthy cases are not scored


def test_incomplete_sat_never_counted_as_healthy():
    d = {
        "formal": {
            "status": "unknown",
            "solver_status": "sat",
            "formal_complete": False,
            "core": [],
            "initial_core": [],
        },
        "status": "unknown",
    }
    assert all(v["status"] == "unknown" for v in predictions(d).values())


def repair_row(g, f, *, status="passed", parsed=True, quality="scored"):
    return {
        "repair_entered": True,
        "truth": {"category_ids": g},
        "post_categories": f,
        "quality_status": quality,
        "candidate": {"parse_success": parsed},
        "deployment_status": status,
        "pending": False if status == "passed" else None,
    }


def test_repair_denominator_includes_failures_and_negative_security_improvement():
    rows = [repair_row(["A"], ["B", "C"]), repair_row(["A"], ["A"], status="no_candidate", parsed=False)]
    m = repair_metrics(rows)
    assert m["n"] == 2 and m["psr"] == m["pr"] == 0.5
    assert m["average_introduced_errors"] == 1.0
    assert m["average_introduced_errors_legacy_percent"] == 100
    assert m["security_improvement"] == -0.5


def test_missing_deployment_and_postcheck_not_optimistic_success():
    rows = [repair_row(["A"], []), repair_row(["A"], None, status="error", quality="incomplete")]
    m = repair_metrics(rows)
    assert m["pr"] is None and m["pr_lower_bound"] == 0.5
    assert m["average_introduced_errors"] is None and m["security_improvement"] is None
    assert m["quality_coverage"] == 0.5 and m["observed_quality"]["security_improvement"] == 1


def detected_row(detected, post, *, status="passed", api=True, quality="scored", truth=None):
    row = repair_row(truth or detected, post, status=status, quality=quality)
    row.update(
        predictions={"combined_detection": {"status": "fault", "category_ids": detected}},
        api_accepted=api,
    )
    return row


def test_resolved_and_introduced_cover_every_repair_case():
    rows = [
        detected_row(["R1", "R2"], []),                                   # repaired: resolves both
        detected_row(["R1"], None, status="blocked", api=None, quality="missing_postcheck"),
        detected_row(["A1"], None, status="failed", api=False, quality="unavailable"),
        detected_row(["S2"], ["S2"], status="no_candidate"),              # unchanged configuration
        detected_row(["R1"], ["P1"]),                                     # resolves R1, introduces P1
        detected_row(["S3"], None, status="failed", quality="incomplete"),
    ]
    m = repair_metrics(rows)
    assert m["detected_categories"] == 7 and m["resolved_categories"] == 3
    assert m["resolved"] == pytest.approx(3 / 7)
    assert m["introduced_cases"] == 3 and m["introduced"] == pytest.approx(3 / 6)


def test_resolved_and_introduced_require_known_outcomes():
    rows = [detected_row(["R1"], []), detected_row(["R1"], None, status="not_evaluated", quality="missing")]
    m = detected_quality([(r["predictions"]["combined_detection"]["category_ids"], r) for r in rows])
    assert m["resolved"] is None and m["introduced"] is None
    assert m["quality_evidence_missing_cases"] == 1
    assert repair_metrics([repair_row(["R1"], [])])["resolved"] is None  # no detected categories recorded


def test_group_repair_metrics_follow_truth_groups():
    rows = [detected_row(["R1"], [], truth=["R1", "S2"]), detected_row(["S2"], ["S2"], status="no_candidate")]
    groups = group_repair_metrics(rows)
    assert groups["R"]["n"] == 1 and groups["R"]["resolved"] == 1
    assert groups["S"]["n"] == 2 and groups["S"]["resolved"] == pytest.approx(1 / 2)
    assert groups["A"]["n"] == 0 and groups["A"]["resolved"] is None


def test_efficiency_counts_repair_calls_only_and_does_not_invent_usage():
    r = {
        "repair": {"attempts": [{}, {}]},
        "model_calls": [
            {"task": "diagnose", "seconds": 100, "usage": {"prompt_tokens": 900}},
            {
                "task": "repair",
                "seconds": 2,
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
            {
                "task": "repair",
                "seconds": 4,
                "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
            },
            {"task": "suggest_repair", "seconds": 100},
        ],
    }
    m = call_metrics(r)
    assert m["seconds"] == 6 and m["prompt_tokens"] == 30 and m["total_tokens"] == 39
    r["model_calls"][2].pop("usage")
    assert call_metrics(r)["total_tokens"] is None
    assert call_metrics(r)["reported_prompt_tokens"] == 10
    r["model_calls"][1]["http_attempts"] = 2
    assert call_metrics(r)["prompt_tokens"] is None
    assert measurement([1, 0, None])["total"] is None
    assert measurement([1, 0])["average"] == 0.5
    assert call_metrics({"model_calls": [], "repair": {"attempts": []}})["total_tokens"] == 0


def candidate_result(original, text):
    return {
        "repair_entered": True,
        "source_snapshot": {"documents": original},
        "repair": {"attempts": [{"proposal": {"fixed_yaml": text}}]},
        "output_action": "original_copy",
    }


def test_parse_success_separate_from_policy_and_last_proposal_only(documents):
    text = yaml_text(documents)  # Identical manifest is parseable but is not a repair.
    r = candidate_result(documents, text)
    c = select_candidate(r, documents)
    assert c["parse_success"] is True and c["policy_valid"] is False
    r["repair"]["attempts"].append({"proposal": {"fixed_yaml": "spec: ["}})
    assert select_candidate(r, documents)["parse_success"] is False
    r["repair"]["attempts"].append({"proposal": None, "validation": {"reason": "API error"}})
    assert select_candidate(r, documents)["selection"] == "no_candidate"


def test_resource_semantic_error_not_yaml_parse_failure(documents):
    changed = copy.deepcopy(documents)
    changed[0]["spec"]["containers"][0]["resources"]["limits"] = {"cpu": "1m"}
    c = select_candidate(candidate_result(documents, yaml_text(changed)), documents)
    assert c["parse_success"] is True and c["policy_valid"] is False


def test_postcheck_unknown_is_not_empty_errors(case, catalog, monkeypatch):
    import confdr.validation.repair_metrics as module

    def incomplete_solve(*args, **kwargs):
        return {**solve(*args, **kwargs), "core_search_complete": False}

    monkeypatch.setattr(module, "solve", incomplete_solve)
    result = postcheck({"status": "failed", "case": case, "observations": {}}, catalog)
    assert result["status"] == "incomplete" and result["category_ids"] is None
    monkeypatch.setattr(module, "solve", solve)
    complete = postcheck({"status": "failed", "case": case, "observations": {}}, catalog)
    assert complete["status"] == "scored" and complete["category_ids"] == ["test.constraint"]
    assert complete["exhaustive"] is False  # Completed skip union is not all MUSes.


def test_formal_timing_excludes_rule_validation(case, catalog):
    r = solve(case, catalog)
    t = r["timing"]
    assert t["modeling_and_solving_seconds"] == pytest.approx(
        t["modeling_seconds"] + t["solver_search_seconds"], abs=2e-6
    )
    assert 0 <= t["first_modeling_and_solving_seconds"] <= t["modeling_and_solving_seconds"] + 1e-6
    assert t["rule_validation_seconds"] >= 0


def pending_sample(tmp_path, documents, case):
    source = sample(tmp_path, documents, case)
    write(source / "truth.json", {"is_pending": True, "pending_labels": ["test.constraint"]})
    return source


def test_complete_workflow_fresh_postcheck_frozen_results(tmp_path, documents, case, catalog):
    source = pending_sample(tmp_path, documents, case)
    out = tmp_path / "run"
    model = TransportDouble()
    run_method(source, out, catalog, model, progress=lambda _: None)
    before = read(out / "dataset/source/result.json")
    initial = read(out / "paper-metrics.json")["overall"]
    assert initial["repair"]["pr"] is None and initial["repair"]["psr"] == 1
    assert initial["detection"]["single_round"]["f1"] == 1
    calls = model.rounds
    evaluate_deployment(out, DeploymentDouble(), progress=lambda _: None)
    assert model.rounds == calls
    assert read(out / "dataset/source/result.json") == before
    report = read(out / "paper-metrics.json")
    m = report["overall"]["repair"]
    assert m["pr"] == 1 and m["security_improvement"] == 1 and m["average_introduced_errors"] == 0
    assert m["configuration_violation_reduction_rate"] == 1
    assert report["overall"]["fixed_cohort_repair"]["success_rate"] == 1
    assert report["overall"]["fixed_cohort_repair"]["configuration_violation_reduction_rate"] == 1
    assert m["pending_false"] == 1
    assert report["overall"]["efficiency"]["repair_total_tokens"]["total"] is None
    dep = read(out / "dataset/source/deployment.json")
    assert dep["paper_evaluation"]["postcheck"]["snapshot_hash"]
    assert dep["paper_evaluation"]["original_postcheck"]["snapshot_hash"]
    assert dep["rules_bundle_hash"] == digest(read(out / "rules-used.json"))
    assert not any("truth" in p or "pending_labels" in p for p in model.payloads)


def test_retry_budgets_use_their_own_kwok_candidates(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    model = TransportDouble()
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, model,
        options={"max_rounds": 5}, progress=lambda _: None,
    )
    original = read(out / "dataset/source/result.json")
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, progress=lambda _: None)
    assert deploy.calls == ["original", "evaluation", "evaluation_prefix_1"]
    assert read(out / "dataset/source/result.json") == original
    evidence = read(out / "dataset/source/deployment.json")["retry_deployments"]
    assert evidence["1"]["status"] == "failed"
    assert evidence["1"]["probe"]["observations"]["candidate_hash"] == evidence["1"]["candidate"]["documents_hash"]
    assert evidence["3"]["probe_source"] == evidence["5"]["probe_source"] == "final_evaluation"
    curve = read(out / "paper-metrics.json")["overall"]["retry_budgets"]
    assert [curve[str(n)]["deployment_pr"] for n in (1, 3, 5)] == [0, 1, 1]


def test_prefix_backfill_preserves_existing_deployment(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(),
        progress=lambda _: None,
    )
    evaluate_deployment(out, DeploymentDouble(), progress=lambda _: None)
    before = read(out / "dataset/source/deployment.json")
    before.pop("retry_deployments")
    write(out / "dataset/source/deployment.json", before)
    assert evaluate_metrics([out])["overall"]["retry_budgets"]["1"]["deployment_pr"] is None
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, prefixes_only=True, progress=lambda _: None)
    assert deploy.calls == ["evaluation_prefix_1"]
    after = read(out / "dataset/source/deployment.json")
    assert {k: v for k, v in after.items() if k != "retry_deployments"} == before
    assert read(out / "paper-metrics.json")["overall"]["retry_budgets"]["1"]["deployment_pr"] == 0

    after["retry_deployments"]["1"]["candidate"]["documents_hash"] = "stale"
    write(out / "dataset/source/deployment.json", after)
    assert evaluate_metrics([out])["overall"]["retry_budgets"]["1"]["deployment_pr"] is None


def test_three_round_prefix_is_deployed_separately(tmp_path, documents, case, catalog):
    class StillUnresolved(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.rounds += 1
                changed = copy.deepcopy(payload["documents"])
                changed[0]["spec"]["nodeSelector"] = {"slot": f"missing-{self.rounds}"}
                self.calls.append({"task": "repair"})
                return {"fixed_yaml": yaml_text(changed), "summary": "Try another selector", "confidence": 0.9}
            return super().complete(task, system, payload, schema)

    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, StillUnresolved(),
        options={"max_rounds": 5}, progress=lambda _: None,
    )
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, progress=lambda _: None)
    assert deploy.calls == [
        "original", "evaluation_unaccepted", "evaluation_prefix_1", "evaluation_prefix_3",
    ]
    evidence = read(out / "dataset/source/deployment.json")["retry_deployments"]
    assert evidence["1"]["candidate"]["documents_hash"] != evidence["3"]["candidate"]["documents_hash"]
    assert evidence["3"]["candidate"]["documents_hash"] != evidence["5"]["candidate"]["documents_hash"]
    assert evidence["5"]["probe_source"] == "final_evaluation"


def test_unaccepted_final_proposal_is_evaluated_without_promotion(tmp_path, documents, case, catalog):
    source = pending_sample(tmp_path, documents, case)
    out = tmp_path / "run"
    run_method(source, out, catalog, TransportDouble(), options={"max_rounds": 1}, progress=lambda _: None)
    original = read(out / "dataset/source/result.json")
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, progress=lambda _: None)
    assert deploy.calls == ["original", "evaluation_unaccepted"]
    assert read(out / "dataset/source/result.json") == original
    assert original["output_action"] == "original_copy"
    report = read(out / "paper-metrics.json")["overall"]["repair"]
    assert report["n"] == 1 and report["psr"] == 1 and report["pr"] == 0
    assert report["security_improvement"] == 0


def test_new_snapshot_can_contradict_internal_sat(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(), progress=lambda _: None
    )

    class ChangedEnvironment(DeploymentDouble):
        def probe(self, *args, **kwargs):
            r = super().probe(*args, **kwargs)
            if kwargs["phase"] == "evaluation":
                r["case"]["ClusterState"]["Nodes"]["node"]["labels"]["slot"] = "changed"
                r["status"] = "failed"
            return r

    evaluate_deployment(out, ChangedEnvironment(), progress=lambda _: None)
    m = read(out / "paper-metrics.json")["overall"]["repair"]
    assert m["pr"] == 0 and m["security_improvement"] == 0
    assert m["observed_quality"]["final_error_categories"] == 1


def test_infrastructure_failure_and_stale_artifact_coverage(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(), progress=lambda _: None
    )
    deploy = DeploymentDouble()
    deploy.unavailable = True
    evaluate_deployment(out, deploy, progress=lambda _: None)
    m = read(out / "paper-metrics.json")["overall"]["repair"]
    assert m["pr"] is None and m["quality_missing_cases"] == 1
    path = out / "dataset/source/deployment.json"
    stale = read(path)
    stale["method_result_hash"] = "old result"
    stale["paper_evaluation"]["deployment_status"] = "passed"
    write(path, stale)
    assert evaluate_metrics([out])["overall"]["repair"]["pr"] is None


def test_offline_metrics_cli_and_case_weighted_aggregation(tmp_path, documents, case, catalog, monkeypatch):
    from confdr.cli import main
    from confdr.shared.llm import LLM

    source = pending_sample(tmp_path, documents, case)
    a, b = tmp_path / "a", tmp_path / "b"
    run_method(source, a, catalog, TransportDouble(), progress=lambda _: None)
    run_method(source, b, catalog, TransportDouble(), options={"max_rounds": 1}, progress=lambda _: None)
    evaluate_deployment(a, DeploymentDouble(), progress=lambda _: None)
    evaluate_deployment(b, DeploymentDouble(), progress=lambda _: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline metrics must not call LLM or deploy")

    monkeypatch.setattr(LLM, "complete", forbidden)
    monkeypatch.setattr(DeploymentDouble, "probe", forbidden)
    assert main(["evaluate-metrics", "--run", str(a), str(b), "--output", str(tmp_path / "metrics")]) == 0
    report = read(tmp_path / "metrics/paper-metrics.json")
    assert report["overall"]["case_count"] == 2 and report["overall"]["repair"]["pr"] == 0.5
    assert main(["evaluate-metrics", "--run", str(a), str(a)]) == 2


def test_rules_and_method_tampering_rejected_before_deployment(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(), progress=lambda _: None
    )
    bundle = read(out / "rules-used.json")
    bundle["verification_material"] = {}
    write(out / "rules-used.json", bundle)
    deploy = DeploymentDouble()
    with pytest.raises(ValueError, match="Frozen"):
        evaluate_deployment(out, deploy, progress=lambda _: None)
    with pytest.raises(ValueError, match="Frozen"):
        evaluate_metrics([out])
    assert not deploy.calls


def test_not_observed_is_not_nonpending(case):
    probe = {"status": "failed", "case": case, "observations": {"pod_status": {}}}
    assert observed_pending(probe) is None
    for name in case["WaitingDeployPods"]:
        probe["observations"]["pod_status"][name] = {"phase": "NotObserved"}
    assert observed_pending(probe) is None


def test_no_candidate_remains_in_denominator_and_rechecks_unchanged_input(tmp_path, documents, case, catalog):
    class NoRepair(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                self.calls.append({"task": task, "seconds": 1})
                return {"fixed_yaml": "", "summary": "No justified repair", "confidence": 0.9}
            return super().complete(task, system, payload, schema)

    out = tmp_path / "run"
    run_method(pending_sample(tmp_path, documents, case), out, catalog, NoRepair(), progress=lambda _: None)
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, progress=lambda _: None)
    m = read(out / "paper-metrics.json")["overall"]["repair"]
    assert deploy.calls == ["original"]
    assert m["n"] == 1 and m["psr"] == m["pr"] == 0
    assert m["quality_missing_cases"] == 0 and m["security_improvement"] == 0


def test_invalid_input_still_produces_missing_metrics(tmp_path, catalog):
    source, out = tmp_path / "source", tmp_path / "run"
    write(source / "case.json", {"WaitingDeployPods": {}})
    write(source / "apps/app.yaml", "spec: [")
    write(source / "truth.json", {"is_pending": True, "pending_labels": ["test.constraint"]})
    run_method(source, out, catalog, TransportDouble(), progress=lambda _: None)
    m = read(out / "paper-metrics.json")["overall"]
    assert m["detection"]["single_round"]["unknown_pending"] == 1
    assert m["repair"]["n"] == 0


def test_overall_counts_cases_not_dataset_averages(tmp_path, documents, case, catalog):
    one, two = tmp_path / "one", tmp_path / "two"
    source = pending_sample(tmp_path, documents, case)
    run_method(source, one, catalog, TransportDouble(), progress=lambda _: None)
    # Two failing cases in dataset two, vs one successful case in dataset one.
    batch = tmp_path / "batch"
    for name in ("first", "second"):
        write(batch / name / "apps/app.yaml", yaml_text(documents))
        write(batch / name / "case.json", case)
        write(batch / name / "truth.json", {"is_pending": True, "pending_labels": ["test.constraint"]})

    class AlwaysUnresolved(TransportDouble):
        def complete(self, task, system, payload, schema):
            if task == "repair":
                changed = copy.deepcopy(documents)
                changed[0]["spec"]["nodeSelector"] = {"slot": "still-absent"}
                self.calls.append({"task": "repair"})
                return {"fixed_yaml": yaml_text(changed), "summary": "Still unresolved", "confidence": 0.9}
            return super().complete(task, system, payload, schema)

    run_method(batch, two, catalog, AlwaysUnresolved(), options={"max_rounds": 1}, progress=lambda _: None)
    for run in (one, two):
        evaluate_deployment(run, DeploymentDouble(), progress=lambda _: None)
    m = evaluate_metrics([one, two], tmp_path / "metrics")["overall"]
    assert m["repair"]["n"] == 3 and m["repair"]["pr"] == pytest.approx(1 / 3)
    assert m["detection"]["single_round"]["tp"] == 3


def test_retry_budgets_report_paired_rechecks(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(),
        options={"max_rounds": 5}, progress=lambda _: None,
    )
    evaluate_deployment(out, DeploymentDouble(), progress=lambda _: None)
    evidence = read(out / "dataset/source/deployment.json")["retry_deployments"]
    assert evidence["1"]["postcheck"]["source"] == "prefix_probe"
    assert evidence["5"]["postcheck"]["source"] == "final_postcheck"
    report = read(out / "paper-metrics.json")["overall"]
    final = report["retry_budgets"]["5"]
    paired = report["repair"]["observed_paired_quality"]
    assert final["paired_quality_cases"] == paired["n"]
    assert final["configuration_violation_reduction_rate"] == paired["configuration_violation_reduction_rate"]
    assert final["average_introduced_errors"] == paired["average_introduced_errors"]
    assert final["resolved"] == report["repair"]["resolved"]
    assert final["introduced"] == report["repair"]["introduced"]


def test_prefix_recheck_backfill_reuses_stored_probes(tmp_path, documents, case, catalog):
    out = tmp_path / "run"
    run_method(
        pending_sample(tmp_path, documents, case), out, catalog, TransportDouble(),
        options={"max_rounds": 5}, progress=lambda _: None,
    )
    evaluate_deployment(out, DeploymentDouble(), progress=lambda _: None)
    deployment = read(out / "dataset/source/deployment.json")
    for entry in deployment["retry_deployments"].values():
        entry.pop("postcheck", None)  # Evidence written before prefix rechecks existed.
    write(out / "dataset/source/deployment.json", deployment)
    deploy = DeploymentDouble()
    evaluate_deployment(out, deploy, prefixes_only=True, progress=lambda _: None)
    assert deploy.calls == []
    evidence = read(out / "dataset/source/deployment.json")["retry_deployments"]
    assert evidence["1"]["probe_source"] == "stored_prefix_evaluation"
    assert evidence["1"]["postcheck"]["status"] in {"scored", "incomplete"}
    assert read(out / "paper-metrics.json")["overall"]["retry_budgets"]["1"]["paired_quality_cases"] >= 0
