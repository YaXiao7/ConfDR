"""Independent deployment evaluation of finalized method outputs. Observe, record and advise without rerunning repairs."""

from __future__ import annotations

import copy
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from confdr.collection.files import load_documents
from confdr.runtime.repair import advice_markdown, professional_advice
from confdr.shared.catalog import source_content
from confdr.shared.io import digest, read, write
from confdr.validation.repair_metrics import (
    observed_pending, postcheck, repair_entered, select_candidate, select_prefix_candidate,
)


def checked_probe(validator, documents, environment, phase):
    try:
        report = validator.probe(documents, environment, phase=phase)
        if report.get("status") not in {"passed", "failed", "error"}:
            raise ValueError("Deployer returned an unknown status")
        if report.get("observations") is not None and report["observations"].get("candidate_hash") != digest(
            documents
        ):
            raise ValueError("Deployment observation and evaluated manifest fingerprint mismatch")
        return report
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {"status": "error", "reason": str(exc)}


def compact_postcheck(report, source):
    """Keep the scoring fields of a formal recheck; full solver traces stay with the final evaluation."""
    if report is None:
        return None
    return {
        **{k: report.get(k) for k in ("status", "category_ids", "snapshot_hash", "reason")},
        "source": source,
    }


def evaluate_prefixes(
    result, documents, selected, final_probe, validator, progress, case_name,
    *, catalog=None, options=None, final_postcheck=None, original_postcheck=None, previous=None,
):
    """Evaluate each distinct fixed-budget candidate without changing method outputs.

    Each output also receives the formal recheck used by CVRR and introduced categories, with the
    same rules as the final output: an output without a candidate keeps the original configuration,
    and a candidate that violates the repair policy is not deployed or rechecked. A stored prefix
    probe for the same candidate is reused, so backfilling rechecks needs no new deployment.
    """
    entries = {}
    for budget in (1, 3, 5):
        prefix = select_prefix_candidate(result, documents, budget)
        entry = {
            "candidate": {k: v for k, v in prefix.items() if k != "documents"},
            "status": "not_evaluated",
            "pending": None,
            "api_accepted": None,
            "feedback_to_method": False,
        }
        old = (previous or {}).get(str(budget)) or {}
        if prefix["selection"] in {"no_candidate", "not_in_repair_cohort"}:
            entry["status"] = "no_candidate"
            entry["postcheck"] = compact_postcheck(original_postcheck, "original_postcheck")
        elif not prefix["policy_valid"]:
            entry["status"] = "blocked"
        else:
            if (
                final_probe is not None
                and prefix["documents_hash"] == selected.get("documents_hash")
                and prefix["selection"] == selected.get("selection")
            ):
                probe = final_probe
                entry["probe_source"] = "final_evaluation"
                entry["postcheck"] = compact_postcheck(final_postcheck, "final_postcheck")
            else:
                stored = old.get("probe")
                if (
                    isinstance(stored, dict)
                    and stored.get("status") in {"passed", "failed"}
                    and old.get("candidate", {}).get("documents_hash") == prefix["documents_hash"]
                    and old.get("candidate", {}).get("selection") == prefix["selection"]
                ):
                    probe = stored
                    entry["probe_source"] = "stored_prefix_evaluation"
                else:
                    progress(f"  KWOK repair prefix {budget}: {case_name}")
                    probe = checked_probe(
                        validator, prefix["documents"], result["source_snapshot"]["case"],
                        f"evaluation_prefix_{budget}",
                    )
                entry["probe"] = probe
                if catalog is not None:
                    entry["postcheck"] = compact_postcheck(
                        postcheck(probe, catalog, options=options), "prefix_probe"
                    )
            entry["status"] = probe["status"]
            entry["pending"] = observed_pending(probe)
            entry["api_accepted"] = probe.get("api_accepted")
        if entry.get("postcheck") is None:
            entry.pop("postcheck", None)
        entries[str(budget)] = entry
    return entries


def evaluate_deployment(run, validator, *, llm=None, progress=print, prefixes_only=False):
    started = time.monotonic()
    run = Path(run).resolve()
    method = read(run / "experiment.json")
    if not method.get("finished") or method.get("stage") != "method_completed":
        raise ValueError("Deployment evaluation requires the method stage to be complete for all cases")
    catalog = read(run / "rules-used.json")
    bundle_hash = digest(catalog)
    if (
        digest(source_content(catalog)) != method.get("rules_hash")
        or method.get("rules_bundle_hash", bundle_hash) != bundle_hash
    ):
        raise ValueError("Frozen rules or validation material have changed")
    records = method.get("records", [])
    # Verify all finalized results before any cluster operation; do not evaluate unfinished or modified method outputs.
    inputs, seen = [], set()
    for record in records:
        relative = Path(record["case"])
        target = run / "dataset" / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not target.resolve().is_relative_to(run / "dataset")
            or str(relative) in seen
        ):
            raise ValueError("Case path is invalid or duplicated")
        seen.add(str(relative))
        result = read(target / "result.json")
        if not result.get("method_finished") or digest(result) != record.get("method_result_hash"):
            raise ValueError("Method results are not finalized or have been modified")
        inputs.append((record, target, result))
    if prefixes_only:
        prepared = []
        summary_path = run / "deployment-summary.json"
        summary = read(summary_path)
        if not summary.get("finished") or summary.get("rules_bundle_hash") != bundle_hash:
            raise ValueError("Prefix backfill requires a completed deployment evaluation")
        for record, target, result in inputs:
            deployment = read(target / "deployment.json")
            if (
                deployment.get("method_result_hash") != record["method_result_hash"]
                or deployment.get("rules_bundle_hash") != bundle_hash
                or deployment.get("output_documents_hash") != result.get("output_documents_hash")
            ):
                raise ValueError(f"Existing deployment evidence is stale: {record['case']}")
            docs, _ = load_documents(target)
            if digest(docs) != result.get("output_documents_hash"):
                raise ValueError(f"Output manifests are missing or modified: {record['case']}")
            selected = select_candidate(result, docs)
            if (
                (deployment.get("paper_evaluation") or {}).get("candidate", {}).get("documents_hash")
                != selected["documents_hash"]
            ):
                raise ValueError(f"Existing candidate evaluation is stale: {record['case']}")
            final_probe = (
                deployment.get("candidate") if result.get("output_action") == "method_candidate"
                else deployment.get("evaluation_candidate")
            )
            prepared.append((record, target, result, deployment, docs, selected, final_probe))
        for record, target, result, deployment, docs, selected, final_probe in prepared:
            if not repair_entered(result):
                continue
            progress(f"Prefix deployment evaluation {record['case']}")
            paper = deployment.get("paper_evaluation") or {}
            deployment["retry_deployments"] = evaluate_prefixes(
                result, docs, selected, final_probe, validator, progress, record["case"],
                catalog=catalog, options=method.get("solve_options"),
                final_postcheck=paper.get("postcheck"), original_postcheck=paper.get("original_postcheck"),
                previous=deployment.get("retry_deployments"),
            )
            write(target / "deployment.json", deployment)
        from confdr.runtime.paper_metrics import evaluate_metrics

        evaluate_metrics([run])
        progress("Paper metrics updated: paper-metrics.txt")
        return summary
    summaries = []
    for record, target, result in inputs:
        progress(f"Deployment evaluation {record['case']}: record results without further repairs")
        case_started = time.monotonic()
        begin = len(llm.calls) if llm is not None else 0
        deployment = {
            "stage": "deployment_evaluation",
            "method_result_hash": record["method_result_hash"],
            "output_documents_hash": result.get("output_documents_hash"),
            "rules_bundle_hash": bundle_hash,
            "method_status": result["repair"]["status"],
            "feedback_to_method": False,
            "repair_correctness": "not_determined",
            "scope": "KWOK simulated deployment only",
            "original": None,
            "candidate": None,
            "snapshot_saved": False,
            "status": "not_evaluated",
        }
        original = result.get("source_snapshot", {})
        docs = []
        selection = None
        paper_probe = None
        try:
            docs, _ = load_documents(target)
            if not docs or digest(docs) != result.get("output_documents_hash"):
                raise ValueError(
                    "Output manifests are missing or modified; finalized results cannot be evaluated for deployment"
                )
            if not original.get("documents") or not isinstance(original.get("case"), dict):
                raise ValueError("The method stage did not obtain valid original inputs")
            deployment["original"] = checked_probe(
                validator, original["documents"], original["case"], "original"
            )
            selection = select_candidate(result, docs)
            if result["output_action"] == "method_candidate":
                final = checked_probe(validator, docs, original["case"], "evaluation")
                deployment["candidate"] = final
                deployment["status"] = final["status"]
                paper_probe = final
            else:
                final = deployment["original"]
                deployment["status"] = "no_candidate"
                if selection["selection"] == "final_proposal" and selection["policy_valid"]:
                    # Retain and evaluate an unaccepted final proposal independently.
                    # Do not promote it to method_candidate or change finalized YAML.
                    paper_probe = checked_probe(
                        validator, selection["documents"], original["case"], "evaluation_unaccepted"
                    )
                    deployment["evaluation_candidate"] = paper_probe
                elif selection["selection"] in {"no_candidate", "not_in_repair_cohort"}:
                    paper_probe = final
            # Do not overwrite case/pod_info in the method output directory; store newly collected snapshots in deployment.json.
            # This prevents failures, reevaluations and evaluations in different environments from changing method inputs or outputs.
            deployment["reason"] = final.get("reason", "")
            deployment["snapshot_saved"] = (
                final.get("case") is not None and final.get("observations") is not None
            )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            deployment.update(status="error", reason=str(exc))
        deployment["deployment_seconds"] = round(time.monotonic() - case_started, 3)
        selected = selection or {"selection": "unavailable", "parse_success": None}
        deployment["paper_evaluation"] = {
            "candidate": {k: v for k, v in selected.items() if k != "documents"},
            "deployment_status": (paper_probe or {}).get("status", "not_evaluated"),
            "pending": observed_pending(paper_probe),
            "api_accepted": (paper_probe or {}).get("api_accepted"),
            "original_postcheck": postcheck(
                deployment.get("original"), catalog, options=method.get("solve_options")
            ),
            "postcheck": postcheck(paper_probe, catalog, options=method.get("solve_options")),
            "feedback_to_method": False,
        }
        if repair_entered(result) and selection is not None and docs and original.get("documents"):
            deployment["retry_deployments"] = evaluate_prefixes(
                result, docs, selected, paper_probe, validator, progress, record["case"],
                catalog=catalog, options=method.get("solve_options"),
                final_postcheck=deployment["paper_evaluation"]["postcheck"],
                original_postcheck=deployment["paper_evaluation"]["original_postcheck"],
            )
        if deployment["status"] in {"failed", "error"} or (
            deployment["status"] == "no_candidate"
            and (deployment.get("original") or {}).get("status") != "passed"
        ):
            advice_context = {
                "reason": "Independent deployment evaluation: " + deployment.get("reason", ""),
                "attempts": [],
                "deployment": deployment,
            }
            # Generate advice only; do not modify method candidates or reports or trigger repair_fault.
            advice = professional_advice(
                original.get("case", {}), docs, result["detection"], advice_context, llm
            )
            deployment["manual_review"] = advice
            write(target / "deployment-advice.md", advice_markdown(advice, advice_context))
        else:
            (target / "deployment-advice.md").unlink(missing_ok=True)
        deployment["model_calls"] = copy.deepcopy(llm.calls[begin:]) if llm is not None else []
        deployment["seconds_including_advice"] = round(time.monotonic() - case_started, 3)
        deployment["observed_at"] = datetime.now(timezone.utc).isoformat()
        write(target / "deployment.json", deployment)
        summaries.append(
            {
                "case": record["case"],
                "method_status": result["repair"]["status"],
                "status": deployment["status"],
                "original_status": (deployment.get("original") or {}).get("status", "not_evaluated"),
                "candidate_status": (deployment.get("candidate") or {}).get("status", "not_evaluated"),
            }
        )
        count = Counter(r["candidate_status"] for r in summaries)
        assessed = count["passed"] + count["failed"]
        summary = {
            "stage": "deployment_evaluation",
            "rules_bundle_hash": bundle_hash,
            "seconds": round(time.monotonic() - started, 3),
            "scope": "simulation deployment rate, not repair correctness",
            "finished": len(summaries) == len(records),
            "total": len(records),
            "completed": len(summaries),
            "statuses": dict(Counter(r["status"] for r in summaries)),
            "records": summaries,
            "candidate_counts": dict(count),
            "candidate_deployment_rate": count["passed"] / assessed if assessed else None,
            "rate_denominator": "candidate passed + failed; infrastructure errors and absent candidates excluded",
            "feedback_to_method": False,
        }
        write(run / "deployment-summary.json", summary)
    if not summaries:
        raise ValueError("No cases are available for evaluation")
    from confdr.runtime.paper_metrics import evaluate_metrics

    evaluate_metrics([run])
    progress("Paper metrics updated: paper-metrics.txt")
    return summary
