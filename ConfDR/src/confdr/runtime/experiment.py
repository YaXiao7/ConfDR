"""Batch method execution: detection and static repair only; evaluate deployments after all method outputs are complete."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from confdr.collection.files import copy_case, discover, load_case, save_candidate
from confdr.runtime.evaluation import evaluate_partition, truth
from confdr.runtime.pipeline import diagnose
from confdr.runtime.repair import advice_markdown, professional_advice, repair_fault, validate_max_rounds
from confdr.runtime.reporting import (
    compare_detection,
    conflict_views,
    evaluate_detection_views,
    format_comparison,
    format_multiple_rounds,
    format_single_round,
)
from confdr.shared.catalog import source_content
from confdr.shared.io import digest, read, write
from confdr.shared.kubernetes import rebuild_case


def output_path(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Output directory already exists; choose a new directory for this run")
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Input and output directories must not contain each other")
    return source, output


def verify_dataset_manifest(source, cases, manifest):
    """Reject changes to the frozen D1/control inputs before making model calls."""
    if manifest.get("schema_version") != "d1-control-dataset-1":
        return
    rows = manifest.get("cases") or []
    indexed = {row.get("case_id"): row for row in rows}
    if len(indexed) != len(rows) or set(indexed) != {p.name for p in cases}:
        raise ValueError("Frozen dataset case inventory has changed")
    for case in cases:
        expected = indexed[case.name].get("files_sha256")
        if not isinstance(expected, dict):
            raise ValueError(f"Missing frozen file hashes: {case.name}")
        actual = {
            str(path.relative_to(case)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in case.rglob("*")
            if path.is_file() and not path.name.startswith("._") and path.name != ".DS_Store"
        }
        if actual != expected:
            raise ValueError(f"Frozen dataset files have changed: {case.name}")


def failure_report(reason: str, case, docs, detection, llm):
    report = {
        "status": "not_accepted",
        "reason": reason,
        "correctness_verified": False,
        "attempts": [],
        "candidate": None,
    }
    report["manual_review"] = professional_advice(case, docs, detection, report, llm)
    return report


def run_method(
    source,
    output,
    catalog,
    llm,
    *,
    options=None,
    label_mapping=None,
    include_fixed=False,
    progress=print,
):
    source, output = output_path(source, output)
    cases = discover(source, include_fixed=include_fixed)
    dataset_manifest = source.parent / "dataset-manifest.json"
    manifest = read(dataset_manifest) if dataset_manifest.is_file() else None
    if manifest is not None:
        verify_dataset_manifest(source, cases, manifest)
    dataset_manifest_hash = digest(manifest) if manifest is not None else None
    options = options or {}
    # Detection options only: these are forwarded to diagnose(), which has no no_lg parameter.
    solve_options = {
        k: options[k]
        for k in (
            "timeout_ms",
            "search_timeout_s",
            "min_confidence",
            "conflict_method",
        )
        if k in options
    }
    # Ablation flag for repair only; recorded in the run summary for provenance.
    no_lg = bool(options.get("no_lg", False))
    validate_max_rounds(options.get("max_rounds", 5))
    known = {c["id"] for c in catalog["taxonomy"]["categories"]}
    if label_mapping is not None:
        if not isinstance(label_mapping, dict) or any(
            not isinstance(v, list) or not v or any(x not in known for x in v) for v in label_mapping.values()
        ):
            raise ValueError("Label mapping must map legacy label strings to lists of current category IDs")
    output.mkdir(parents=True)
    data = output / "dataset"
    data.mkdir()
    write(
        output / "rules-used.json",
        {**source_content(catalog), "verification_material": catalog["verification_material"]},
    )
    started = time.monotonic()
    records = []
    paths = []
    # Copy every case first, preserving the directory hierarchy so an individual failure does not skip later cases.
    for case_dir in cases:
        relative = case_dir.relative_to(source) if case_dir != source else Path(case_dir.name)
        target = data / relative
        copy_case(case_dir, target)
        paths.append((case_dir, target, str(relative)))
    for index, (case_dir, target, case_id) in enumerate(paths, 1):
        progress(f"[{index}/{len(paths)}] {case_id}: ConfDR detection and static repair")
        begin = time.monotonic()
        call_begin = len(llm.calls)
        case, docs, obs = {}, [], {}
        result = {"detection": {"status": "unknown"}, "repair": None, "repair_entered": False}
        repair_started = None
        try:
            case, docs, obs, locations = load_case(case_dir, include_observations=False)
            if not docs:
                raise ValueError("Case is missing deployable apps/envs YAML")
            # Fill missing static inputs from manifests; retain existing resource snapshots and custom observations.
            if not case.get("WaitingDeployPods"):
                original_extra = case.get("ExtraFacts")
                case = rebuild_case(docs, case, document_locations=locations)
                if original_extra is not None:
                    case["ExtraFacts"] = original_extra
            consistency = rebuild_case(docs, case, document_locations=locations).get(
                "_unsupported_manifest_features", []
            )
            if any(
                "Cannot associate manifest with original target" in item
                or "Target association is not unique" in item
                for item in consistency
            ):
                raise ValueError(
                    "Targets in case.json and YAML differ; recollect with collect first: "
                    + "; ".join(consistency)
                )
            result["source_snapshot"] = {"case": case, "documents": docs}
            original = diagnose(
                case,
                catalog,
                llm,
                observations=obs,
                documents=docs,
                progress=progress,
                case_id=case_id,
                **solve_options,
            )
            result["detection"] = original
            if original["status"] == "fault":
                result["repair_entered"] = True
                repair_started = time.monotonic()
                repair = repair_fault(
                    case,
                    docs,
                    original,
                    catalog,
                    llm,
                    observations=obs,
                    document_locations=locations,
                    progress=progress,
                    case_id=case_id,
                    **options,
                )
            elif original["status"] == "healthy":
                repair = {
                    "status": "no_change",
                    "reason": "Initial detection found no issues requiring changes",
                    "attempts": [],
                    "candidate": None,
                    "correctness_verified": False,
                }
            else:
                repair = failure_report(
                    "Initial detection has insufficient evidence; unsupported changes are not allowed",
                    case,
                    docs,
                    original,
                    llm,
                )
            result["repair"] = repair
            if repair["status"] == "candidate_accepted":
                # Complete all writes in a full temporary copy first; preserve the official original copy if a write fails.
                with tempfile.TemporaryDirectory(prefix=".confdr-output-", dir=output) as temp:
                    staged = Path(temp) / "case"
                    copy_case(case_dir, staged)
                    save_candidate(
                        staged,
                        repair["candidate"],
                        locations,
                        repair["candidate_case"],
                        {
                            "candidate_hash": digest(repair["candidate"]),
                            "collection": {"scope": "not_collected", "observed": False},
                            "pod_status": {},
                            "pod_events": {},
                        },
                    )
                    backup = Path(temp) / "original"
                    target.rename(backup)
                    try:
                        staged.rename(target)
                    except OSError:
                        backup.rename(target)
                        raise
                # Candidate and derived inputs are saved; runtime observations come only from subsequent independent evaluation.
                repair["accepted_candidate_hash"] = digest(repair.pop("candidate"))
                repair.pop("candidate_case", None)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
            result["error"] = str(exc)
            if not result["detection"].get("formal"):
                result["detection"]["error"] = str(exc)
                progress(format_multiple_rounds(case_id, result["detection"]))
            result["repair"] = failure_report(
                "Case processing failed: " + str(exc), case, docs, result["detection"], llm
            )
        result["repair"].setdefault("max_rounds", options.get("max_rounds", 5))
        result["repair"].setdefault("rounds_used", len(result["repair"].get("attempts", [])))
        result["repair"].setdefault(
            "stop_reason", "no_repair_needed" if result["repair"]["status"] == "no_change" else "not_started"
        )
        result["repair_seconds"] = (
            round(time.monotonic() - repair_started, 6) if repair_started is not None else 0.0
        )
        result["seconds"] = round(time.monotonic() - begin, 3)
        result["model_calls"] = copy.deepcopy(llm.calls[call_begin:])
        result["output_action"] = (
            "method_candidate" if result["repair"]["status"] == "candidate_accepted" else "original_copy"
        )
        result["method_finished"] = True
        result["validation_scope"] = "method internal static checks; deployment has not been evaluated"
        try:
            from confdr.collection.files import load_documents

            result["output_documents_hash"] = digest(load_documents(target)[0])
        except (OSError, ValueError, TypeError, KeyError):
            result["output_documents_hash"] = None
        # Read ground truth after detection and repair; never include it in model requests.
        try:
            expected = truth(case_dir, label_mapping, known_categories=known)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            expected = {
                "status": "unlabelled",
                "category_ids": [],
                "category_labelled": False,
                "error": str(exc),
            }
        write(target / "res_single_round.txt", format_single_round(case_id, result["detection"]) + "\n")
        write(target / "res.txt", format_multiple_rounds(case_id, result["detection"]) + "\n")
        result["conflict_report"] = conflict_views(result["detection"])
        result["label_evaluation"] = compare_detection(expected, result["detection"])
        progress("  Evaluation: " + format_comparison(result["label_evaluation"]["combined_detection"]))
        write(target / "result.json", result)
        if result["repair"].get("manual_review"):
            write(target / "advice.md", advice_markdown(result["repair"]["manual_review"], result["repair"]))
        records.append(
            {
                "case": case_id,
                "truth": expected,
                "result": {
                    "detection": {
                        "status": result["detection"].get("status", "unknown"),
                        "category_ids": result["detection"].get("category_ids", []),
                        "formal": {
                            key: value
                            for key, value in (result["detection"].get("formal") or {}).items()
                            if key
                            in {
                                "status",
                                "solver_status",
                                "terminal_status",
                                "conflict_method",
                                "formal_complete",
                                "core_search_complete",
                                "all_conflicts_enumerated",
                                "minimality_proved",
                                "minimality_scope",
                                "reason",
                                "initial_core",
                                "core",
                            }
                        },
                    }
                },
                "repair_status": result["repair"]["status"],
                "output_action": result["output_action"],
                "method_result_hash": digest(result),
            }
        )
        summary = {
            "started_from": str(source),
            "output_dataset": str(data),
            "rules_hash": digest(source_content(catalog)),
            "rules_bundle_hash": digest(
                {**source_content(catalog), "verification_material": catalog["verification_material"]}
            ),
            "solve_options": solve_options,
            "no_lg": no_lg,
            "max_rounds": options.get("max_rounds", 5),
            "model": getattr(llm, "model", None),
            "model_source": getattr(llm, "model_source", None),
            "task_models": getattr(llm, "task_models", None),
            "dataset_manifest_hash": dataset_manifest_hash,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "seconds": round(time.monotonic() - started, 3),
            "completed": len(records),
            "total": len(cases),
            "finished": len(records) == len(cases),
            "stage": "method_completed" if len(records) == len(cases) else "method_running",
            "statuses": dict(Counter(r["repair_status"] for r in records)),
            "records": records,
            "evaluation_scope": "original input detection only; copied truth is never relabelled",
            "evaluation": evaluate_partition(records, [], known_categories=sorted(known)),
            "detection_comparison": evaluate_detection_views(records),
            "detection_comparison_matched": evaluate_detection_views(
                [
                    r
                    for r in records
                    if "initial_core" in (r.get("result", {}).get("detection", {}).get("formal") or {})
                ]
            ),
        }
        write(output / "experiment.json", summary)
        progress(f"  → {result['repair']['status']}: {result['repair']['reason']}")
    for view, metrics in summary["detection_comparison"].items():
        micro = metrics["micro"]
        rates = ", ".join(
            f"{key}={micro[key]:.4f}" if micro[key] is not None else f"{key}=N/A"
            for key in ("precision", "recall", "f1")
        )
        exact = f"{metrics['exact_match']:.4f}" if metrics["exact_match"] is not None else "N/A"
        progress(
            f"{('Z3 aggregate' if view == 'all_conflicts' else view)}: {metrics['scored_cases']} scored, {metrics['unscored_cases']} unscored, "
            f"{metrics['unrecorded_cases']} unrecorded; {rates}; exact_match={exact}"
        )
    from confdr.runtime.paper_metrics import evaluate_metrics

    evaluate_metrics([output])
    progress(
        "Paper metrics: paper-metrics.txt (deployment metrics remain pending until independent evaluation)"
    )
    return summary


def run_experiment(source, output, catalog, llm, validator, **kwargs):
    """Run two independent stages sequentially; do not feed evaluation results back into the method."""
    summary = run_method(source, output, catalog, llm, **kwargs)
    from confdr.validation.deployment import evaluate_deployment

    deployment = evaluate_deployment(output, validator, llm=llm, progress=kwargs.get("progress", print))
    return {**summary, "deployment_statuses": deployment["statuses"]}
