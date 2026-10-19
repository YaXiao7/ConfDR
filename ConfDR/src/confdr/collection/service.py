"""Single-case and sequential dataset collection, independent of model execution."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from confdr.collection.collector import Collector
from confdr.collection.files import (
    copy_case,
    discover,
    load_case,
    prepare_kwok_probe_case,
    save_candidate,
)
from confdr.shared.io import write


def _paths(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError("Input directory does not exist")
    if output.exists():
        raise ValueError("Output directory already exists; choose a new directory for this run")
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Input and output directories must not contain each other")
    return source, output


def collect_case(source, output, cfg, *, kwok=False, kubeconfig=None, logs=True,
                 require_running=False):
    source, output = _paths(source, output)
    if kwok == bool(kubeconfig):
        raise ValueError("Choose exactly one of KWOK or an explicit kubeconfig")
    if require_running and not kwok:
        raise ValueError("require_running requires KWOK")
    case, docs, _, locations = load_case(source, validate_targets=False)
    if not docs:
        raise ValueError("Case is missing apps/envs YAML")
    if kwok:
        from confdr.validation.kwok import KwokValidator

        probe_case, omitted_existing_pods = prepare_kwok_probe_case(case, docs, locations)
        result = KwokValidator(cfg.get("kwok")).probe(
            docs, probe_case, phase="candidate" if require_running else "original"
        )
        if require_running and result.get("status") == "passed":
            observed = result.get("observations") or {}
            statuses = observed.get("pod_status") or {}
            claims = observed.get("pvc_status") or {}
            if not statuses or any(item.get("phase") != "Running" for item in statuses.values()):
                result.update(status="failed", reason="target_pod_not_running", deployment_ready=False)
            elif any(item.get("phase") != "Bound" for item in claims.values()):
                result.update(status="failed", reason="target_pvc_not_bound", deployment_ready=False)
        if omitted_existing_pods:
            result["fixture_deduplication"] = {
                "omitted_existing_pods_already_in_envs": omitted_existing_pods
            }
        if result.get("case") is None or result.get("observations") is None:
            raise ValueError(
                "KWOK did not produce valid collection results: " + result.get("reason", "unknown")
            )
        snapshot, observations = result["case"], result["observations"]
    else:
        snapshot, observations = Collector(
            kubeconfig, kubectl=cfg.get("kwok", {}).get("kubectl", "kubectl")
        ).collect(docs, logs=logs, document_locations=locations)
        result = {"status": "collected", "scope": "live_cluster_read_only"}
    metadata = {k: v for k, v in result.items() if k not in {"case", "observations"}}
    # Publish only a complete case; interrupted writes must not leave stale copied observations.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".confdr-collect-", dir=output.parent) as temp:
        staged = Path(temp) / "case"
        copy_case(source, staged)
        save_candidate(staged, docs, locations, snapshot, observations)
        write(staged / "collection.json", metadata)
        staged.rename(output)
    return metadata


def collect_dataset(
    source, output, cfg, *, kwok=False, kubeconfig=None, logs=True, include_fixed=False,
    require_running=False, progress=print
):
    source, output = _paths(source, output)
    if kwok == bool(kubeconfig):
        raise ValueError("Choose exactly one of KWOK or an explicit kubeconfig")
    if require_running and not kwok:
        raise ValueError("require_running requires KWOK")
    cases = discover(source, include_fixed=include_fixed)
    records = [
        {"case_id": str(p.relative_to(source) if p != source else Path(p.name)), "status": "pending"}
        for p in cases
    ]
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "source": str(source),
        "mode": "kwok" if kwok else "existing_cluster_read_only",
        "require_running": require_running,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "cases": records,
    }

    def checkpoint():
        report["counts"] = {
            "total": len(records),
            **{
                status: sum(r["status"] == status for r in records)
                for status in ("pending", "running", "collected", "error", "interrupted")
            },
            "deployment_failed": sum(r.get("deployment_status") == "failed" for r in records),
        }
        write(output / "collection-batch.json", report)

    checkpoint()
    for index, (case_dir, record) in enumerate(zip(cases, records, strict=True), 1):
        record["status"] = "running"
        checkpoint()
        progress(f"[{index}/{len(cases)}] {record['case_id']}: collecting", flush=True)
        started = time.monotonic()
        try:
            relative = Path("dataset") / record["case_id"]
            result = collect_case(
                case_dir, output / relative, cfg, kwok=kwok, kubeconfig=kubeconfig, logs=logs,
                require_running=require_running,
            )
            record.update(
                status="error" if result.get("status") == "error" else "collected",
                output=str(relative),
                reason=result.get("reason"),
            )
            if kwok:
                record["deployment_status"] = result.get("status")
        except KeyboardInterrupt:
            record["status"] = "interrupted"
            report["status"] = "interrupted"
            raise
        except Exception as exc:
            # A malformed case, rejected manifest or cluster error must not skip later cases.
            record.update(status="error", reason=str(exc), error_type=type(exc).__name__)
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            checkpoint()
        progress(f"[{index}/{len(cases)}] {record['case_id']}: {record['status']}", flush=True)
    report["status"] = (
        "completed_with_errors" if any(r["status"] == "error" for r in records) else "completed"
    )
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return report
