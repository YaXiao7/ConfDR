"""Independent evidence for paper repair metrics; never guides method acceptance."""

from __future__ import annotations

import time

import yaml

from confdr.collection.files import WORKLOADS
from confdr.runtime.repair import parse_candidate
from confdr.runtime.solver import solve
from confdr.shared.io import digest


def repair_entered(result):
    # Legacy outputs did not record entry explicitly. Use the original diagnosis,
    # never ground truth or candidate acceptance, to reconstruct the cohort.
    return result.get("repair_entered", result.get("detection", {}).get("status") == "fault") is True


def _structure(documents):
    """Syntax/basic structure only. Semantic resource limits and repair policy are separate."""
    if not documents or len(documents) > 1000:
        raise ValueError("Expected 1–1000 Kubernetes objects")
    for doc in documents:
        if not isinstance(doc, dict) or not isinstance(doc.get("metadata"), dict):
            raise ValueError("Each manifest must be an object with metadata")
        if any(
            not isinstance(x, str) or not x.strip()
            for x in (doc.get("apiVersion"), doc.get("kind"), doc["metadata"].get("name"))
        ):
            raise ValueError("apiVersion, kind and metadata.name must be nonempty strings")
        spec = doc.get("spec", {})
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        if doc["kind"] not in WORKLOADS:
            continue
        if doc["kind"] == "CronJob":
            spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
        elif doc["kind"] != "Pod":
            spec = spec.get("template", {}).get("spec", {})
        containers = spec.get("containers")
        if not isinstance(containers, list) or not containers:
            raise ValueError("Workload containers must be a nonempty list")
        for container in [*containers, *(spec.get("initContainers") or [])]:
            if not isinstance(container, dict) or any(
                not isinstance(container.get(k), str) or not container[k].strip() for k in ("name", "image")
            ):
                raise ValueError("Container name and image must be nonempty strings")
            for field in ("command", "args"):
                if field in container and (
                    not isinstance(container[field], list)
                    or any(not isinstance(x, str) for x in container[field])
                ):
                    raise ValueError(f"{field} must be a list of strings")


def select_candidate(result, output_documents):
    """Accepted output, otherwise the final attempt only; never cherry-pick an earlier success."""
    report = {
        "selection": "no_candidate",
        "parse_success": False,
        "policy_valid": False,
        "documents": None,
        "documents_hash": None,
        "reason": "No final repair proposal",
    }
    if not repair_entered(result):
        report.update(selection="not_in_repair_cohort", parse_success=None)
        return report
    repair = result.get("repair") or {}
    attempts = repair.get("attempts") or []
    accepted = result.get("output_action") == "method_candidate"
    text = (attempts[-1].get("proposal") or {}).get("fixed_yaml") if attempts else None
    if not accepted and (not isinstance(text, str) or not text.strip()):
        return report
    report["selection"] = "accepted_output" if accepted else "final_proposal"
    try:
        if accepted:
            documents = output_documents
        else:
            if len(text) > 2_000_000:
                raise ValueError("Proposal exceeds the size limit")
            documents = [x for x in yaml.safe_load_all(text) if x is not None]
        _structure(documents)
        report.update(parse_success=True, documents=documents, documents_hash=digest(documents), reason="")
    except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        report.update(selection="invalid_proposal", reason=str(exc))
        return report
    try:
        original = result.get("source_snapshot", {}).get("documents")
        if not original:
            raise ValueError("Original manifests unavailable for repair-policy checks")
        parse_candidate(yaml.safe_dump_all(documents, sort_keys=False), original)
        report["policy_valid"] = True
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        report["reason"] = str(exc)
    return report


def select_prefix_candidate(result, output_documents, budget):
    """Select the candidate visible after a fixed number of repair attempts."""
    attempts = ((result.get("repair") or {}).get("attempts") or [])[:budget]
    accepted = any((a.get("validation") or {}).get("status") == "accepted" for a in attempts)
    prefix = {
        "repair_entered": repair_entered(result),
        "source_snapshot": result.get("source_snapshot"),
        "repair": {"attempts": attempts},
        "output_action": "method_candidate" if accepted else "original_copy",
    }
    return select_candidate(prefix, output_documents)


def postcheck(probe, catalog, *, options=None):
    """Re-detect the fresh deployment snapshot with frozen rules and no LLM."""
    started = time.monotonic()
    report = {
        "status": "unavailable",
        "category_ids": None,
        "feedback_to_method": False,
        "scope": "fresh_snapshot_formal_detection",
        "formal": None,
    }
    if not isinstance(probe, dict) or probe.get("status") not in {"passed", "failed"}:
        report["reason"] = "No completed deployment observation"
        return report
    case, observations = probe.get("case"), probe.get("observations")
    if not isinstance(case, dict) or not isinstance(observations, dict):
        report["reason"] = "Fresh case/observations unavailable (e.g. API rejection)"
        return report
    report["snapshot_hash"] = digest({"case": case, "observations": observations})
    allowed = {"timeout_ms", "search_timeout_s", "conflict_method"}
    formal = solve(case, catalog, **{k: v for k, v in (options or {}).items() if k in allowed})
    report["formal"] = formal
    # A completed skip traversal is usable for the legacy detected-label metric,
    # but it is NOT exhaustive MUS enumeration. UNSAT formal_complete is normally
    # false in skipping mode; check coverage and traversal separately.
    coverage_missing = any(
        formal.get(k)
        for k in (
            "skipped_rules",
            "missing_evidence",
            "unverified_categories",
            "unsupported_manifest_features",
        )
    )
    usable = (
        formal.get("core_search_complete") is True
        and not coverage_missing
        and (
            (formal.get("status") == "sat" and formal.get("formal_complete") is True)
            or (
                formal.get("status") == "unsat"
                and formal.get("reason") != "protected_constraints_inconsistent"
            )
        )
    )
    report.update(
        status="scored" if usable else "incomplete",
        category_ids=sorted(set(formal.get("category_ids", []))) if usable else None,
        reason=formal.get("reason"),
        conflict_method=formal.get("conflict_method"),
        exhaustive=formal.get("all_conflicts_enumerated", False),
        seconds=round(time.monotonic() - started, 6),
    )
    return report


def observed_pending(probe):
    """Pending is distinct from readiness and from absent/NotObserved targets."""
    if not isinstance(probe, dict) or probe.get("status") not in {"passed", "failed"}:
        return None
    targets = (probe.get("case") or {}).get("WaitingDeployPods") or {}
    statuses = (probe.get("observations") or {}).get("pod_status") or {}
    if not targets:
        return None
    phases = []
    for name, pod in targets.items():
        status = statuses.get(name) or {}
        if status.get("namespace", "default") != pod.get("namespace", "default"):
            return None
        phases.append(status.get("phase"))
    if any(p == "Pending" for p in phases):
        return True
    if all(p in {"Running", "Succeeded", "Failed"} for p in phases):
        return False
    return None
