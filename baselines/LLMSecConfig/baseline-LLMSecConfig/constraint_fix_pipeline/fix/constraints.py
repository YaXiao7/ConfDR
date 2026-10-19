from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any, Dict, List, Optional, Tuple

from constraint_fix_pipeline.utils.io import (
    load_fix_examples_from_dir,
)


@dataclass
class K8sObject:
    kind: str
    name: str
    namespace: Optional[str]
    labels: Dict[str, str] = field(default_factory=dict)
    selectors: Dict[str, str] = field(default_factory=dict)
    ports: List[Dict[str, Any]] = field(default_factory=list)  # service or container ports
    container_ports: List[int] = field(default_factory=list)  # deployment/statefulset
    resources: Dict[str, Any] = field(default_factory=dict)  # per-container {requests:{}, limits:{}}
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Conflict:
    id: str
    type: str
    description: str
    involved: List[str]  # resource identifiers
    constraints: List[str]  # textual constraints participating in the conflict


@dataclass
class UnsatCore:
    core_id: str
    conflict_type: str
    constraints: List[str]
    involved: List[str]
    suggestion: str
    examples: List[Dict[str, Any]]


def _obj_id(obj: K8sObject) -> str:
    ns = obj.namespace or "default"
    return f"{obj.kind}/{ns}/{obj.name}"


def _extract_objects(docs: List[Dict[str, Any]]) -> List[K8sObject]:
    objs: List[K8sObject] = []
    for d in docs:
        try:
            kind = d.get("kind") or d.get("Kind")
            meta = d.get("metadata", {})
            spec = d.get("spec", {})
            name = meta.get("name") or meta.get("Name") or "unknown"
            ns = meta.get("namespace")

            labels = meta.get("labels", {}) or {}
            selectors = spec.get("selector", {}).get("matchLabels", {}) if spec.get("selector") else spec.get("selector", {}) or {}

            ports: List[Dict[str, Any]] = []
            container_ports: List[int] = []
            resources_agg: Dict[str, Any] = {}

            if kind == "Service":
                for p in spec.get("ports", []) or []:
                    ports.append({
                        "port": p.get("port"),
                        "targetPort": p.get("targetPort"),
                        "nodePort": p.get("nodePort"),
                        "protocol": p.get("protocol"),
                    })
            elif kind in {"Deployment", "StatefulSet", "DaemonSet"}:
                tpl = spec.get("template", {}).get("spec", {})
                for c in tpl.get("containers", []) or []:
                    for p in c.get("ports", []) or []:
                        if "containerPort" in p:
                            container_ports.append(int(p.get("containerPort")))
                    if c.get("resources"):
                        resources_agg.setdefault("containers", []).append(c.get("resources"))
            obj = K8sObject(
                kind=kind,
                name=name,
                namespace=ns,
                labels=labels,
                selectors=selectors,
                ports=ports,
                container_ports=container_ports,
                resources=resources_agg,
                raw=d,
            )
            objs.append(obj)
        except Exception:
            # Skip malformed docs
            continue
    return objs


def _detect_conflicts(objs: List[K8sObject]) -> List[Conflict]:
    conflicts: List[Conflict] = []

    # Build workload index by labels
    workload_objs = [o for o in objs if o.kind in {"Deployment", "StatefulSet", "DaemonSet"}]
    service_objs = [o for o in objs if o.kind == "Service"]

    # Map label sets for workloads
    wl_by_ns = {}
    for o in workload_objs:
        wl_by_ns.setdefault(o.namespace or "default", []).append(o)

    # 1) Service selector ↔ workload labels compatibility
    for svc in service_objs:
        ns = svc.namespace or "default"
        workloads = wl_by_ns.get(ns, [])
        # Determine if any workload matches selector
        selector = svc.selectors or {}
        if selector:
            any_match = False
            for wl in workloads:
                if all(wl.labels.get(k) == v for k, v in selector.items()):
                    any_match = True
                    break
            if not any_match:
                conflicts.append(Conflict(
                    id=f"selector_labels_{_obj_id(svc)}",
                    type="SelectorLabelMismatch",
                    description=f"Service {_obj_id(svc)} selector does not match any workload labels in namespace {ns}.",
                    involved=[_obj_id(svc)] + [_obj_id(w) for w in workloads],
                    constraints=[
                        "service.selector must match at least one workload.labels",
                        "workload.labels are fixed unless edited",
                    ],
                ))

    # 2) Service port ↔ containerPort alignment
    for svc in service_objs:
        ns = svc.namespace or "default"
        workloads = wl_by_ns.get(ns, [])
        for p in svc.ports:
            svc_ports = [p.get("port"), p.get("targetPort")]
            svc_ports = [int(x) for x in svc_ports if isinstance(x, (int, str)) and str(x).isdigit()]
            all_container_ports = []
            for wl in workloads:
                all_container_ports.extend(wl.container_ports)
            if svc_ports and all_container_ports:
                # If none of svc_ports appear in container_ports, flag mismatch
                if not any(sp in all_container_ports for sp in svc_ports):
                    conflicts.append(Conflict(
                        id=f"port_mismatch_{_obj_id(svc)}",
                        type="ServicePortMismatch",
                        description=f"Service {_obj_id(svc)} ports {svc_ports} do not align with any containerPort in namespace {ns}.",
                        involved=[_obj_id(svc)] + [_obj_id(w) for w in workloads],
                        constraints=[
                            "service.port or targetPort should map to container.containerPort",
                            "container.ports define exposed ports",
                        ],
                    ))

    # 3) NodePort conflicts and range
    for svc in service_objs:
        for p in svc.ports:
            node_port = p.get("nodePort")
            if node_port is not None:
                try:
                    np = int(node_port)
                except Exception:
                    continue
                if not (30000 <= np <= 32767):
                    conflicts.append(Conflict(
                        id=f"nodeport_range_{_obj_id(svc)}",
                        type="NodePortOutOfRange",
                        description=f"Service {_obj_id(svc)} nodePort {np} is out of the allowed range 30000-32767.",
                        involved=[_obj_id(svc)],
                        constraints=[
                            "nodePort must be within 30000-32767",
                        ],
                    ))

    # 4) Resources requests <= limits
    for wl in workload_objs:
        for res in wl.resources.get("containers", []) or []:
            req = res.get("requests", {}) or {}
            lim = res.get("limits", {}) or {}
            for k, v in req.items():
                if k in lim:
                    try:
                        if _compare_resource(v, lim[k]) > 0:
                            conflicts.append(Conflict(
                                id=f"resource_req_gt_lim_{_obj_id(wl)}_{k}",
                                type="ResourceRequestExceedsLimit",
                                description=f"Workload {_obj_id(wl)} request {k}={v} exceeds limit {lim[k]}.",
                                involved=[_obj_id(wl)],
                                constraints=[
                                    "container.resources.requests[k] must be <= container.resources.limits[k]",
                                    "resource units must be normalized",
                                ],
                            ))
                    except Exception:
                        # If unparsable, flag advisory conflict
                        conflicts.append(Conflict(
                            id=f"resource_unit_unparsable_{_obj_id(wl)}_{k}",
                            type="ResourceUnitUnparsable",
                            description=f"Workload {_obj_id(wl)} resource key {k} has unparsable values (requests={v}, limits={lim.get(k)}).",
                            involved=[_obj_id(wl)],
                            constraints=[
                                "resource units must be comparable (e.g., 100m vs 0.1)",
                            ],
                        ))
    return conflicts


def _compare_resource(a: str, b: str) -> int:
    """Compare K8s CPU/memory strings. Returns 1 if a>b, 0 if equal, -1 if a<b.
    CPU examples: 500m vs 0.5; Memory examples: 256Mi vs 0.25Gi.
    """
    def to_num(x: str) -> float:
        s = str(x)
        if s.endswith("m"):
            return float(s[:-1]) / 1000.0  # CPU milli to cores
        if s.endswith("Ki"):
            return float(s[:-2]) * 1024.0
        if s.endswith("Mi"):
            return float(s[:-2]) * 1024.0 * 1024.0
        if s.endswith("Gi"):
            return float(s[:-2]) * 1024.0 * 1024.0 * 1024.0
        return float(s)
    aa = to_num(a)
    bb = to_num(b)
    return 1 if aa > bb else (-1 if aa < bb else 0)


def get_minimal_unsat_cores(
    docs: List[Dict[str, Any]],
    external_conflicts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[UnsatCore], List[Conflict]]:
    """Compute minimal unsatisfiable cores (MUS) from YAML-based constraints and third-party conflict signals.

    - Build internal conflicts from YAML semantics (selector/labels, ports, nodePort range, resources).
    - Merge external conflicts provided by third-party detectors.
    - For each conflict, its MUS is the minimal set of textual constraints that cannot hold simultaneously.

    Returns (unsat_cores, all_conflicts)
    """
    objs = _extract_objects(docs)
    internal_conflicts = _detect_conflicts(objs)

    merged_conflicts: List[Conflict] = list(internal_conflicts)
    if external_conflicts:
        for i, ec in enumerate(external_conflicts):
            merged_conflicts.append(Conflict(
                id=ec.get("id", f"external_{i}"),
                type=ec.get("type", "ExternalRuleViolation"),
                description=ec.get("description", "External detector reported violation"),
                involved=ec.get("involved", []),
                constraints=ec.get("constraints", ["external rule: violated"]),
            ))

    cores: List[UnsatCore] = []
    for idx, c in enumerate(merged_conflicts):
        # Minimal core assumption: constraints list already represents the minimal set
        core_constraints = _minimize_constraints(c.constraints)
        suggestion = _suggest_fix_for_conflict(c)
        cores.append(UnsatCore(
            core_id=f"core_{idx}_{c.id}",
            conflict_type=c.type,
            constraints=core_constraints,
            involved=c.involved,
            suggestion=suggestion,
            examples=[],  # to be filled by RAG retrieval
        ))
    return cores, merged_conflicts


def _minimize_constraints(constraints: List[str]) -> List[str]:
    """Heuristic minimalization: remove duplicates, trim whitespace, keep short canonical statements."""
    seen = set()
    out = []
    for s in constraints:
        k = " ".join(str(s).strip().split())
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _suggest_fix_for_conflict(c: Conflict) -> str:
    t = c.type
    if t == "SelectorLabelMismatch":
        return "Align service.selector with workload.labels or adjust labels to include selector keys."
    if t == "ServicePortMismatch":
        return "Map service.port/targetPort to a valid containerPort; update container ports if necessary."
    if t == "NodePortOutOfRange":
        return "Choose a nodePort within 30000-32767 or switch to ClusterIP/LoadBalancer as appropriate."
    if t == "ResourceRequestExceedsLimit":
        return "Reduce requests or increase limits so that requests <= limits with consistent units."
    if t == "ResourceUnitUnparsable":
        return "Normalize resource units (e.g., 500m vs 0.5, 256Mi vs 0.25Gi) so they are comparable."
    # External or unknown
    return "Review constraint definitions and adjust configuration to satisfy all constraints with minimal change."


def retrieve_rag_examples_for_cores(
    cores: List[UnsatCore],
    examples_dir: str,
    top_k: int = 3,
) -> List[UnsatCore]:
    """Attach RAG examples to each core using simple lexical similarity against historical fix examples.

    Ranks over raw examples loaded from the directory (JSON or before/after YAML pairs).
    """
    examples = load_fix_examples_from_dir(examples_dir)

    def score_example(core: UnsatCore, ex: Dict[str, Any]) -> float:
        keys = [core.conflict_type] + core.constraints
        text = " ".join(keys).lower()
        before = (ex.get("before") or "").lower()
        notes = (ex.get("notes") or "").lower()
        err = (ex.get("error") or "").lower()
        score = 0.0
        for token in {"selector", "label", "port", "nodeport", "resource", "limit", "request"}:
            if token in text and (token in before or token in notes or token in err):
                score += 1.0
        # crude overlap measure
        for w in set(text.split()):
            if w and (w in before or w in err):
                score += 0.2
        return score

    for i, core in enumerate(cores):
        ranked = sorted(examples, key=lambda ex: score_example(core, ex), reverse=True)
        core.examples = ranked[:top_k]
    return cores


def analyze_constraints(
    docs: List[Dict[str, Any]],
    external_conflicts: Optional[List[Dict[str, Any]]] = None,
    examples_dir: Optional[str] = None,
    top_k: int = 3,
) -> Dict[str, Any]:
    """Primary entry for the runner:
    - Compute internal + external conflicts
    - Produce MUS cores and suggestions
    - Optionally attach RAG examples from examples_dir
    - Provide lightweight relationships/conflicts list for downstream prompt assembly
    """
    cores, conflicts = get_minimal_unsat_cores(docs, external_conflicts)
    if examples_dir:
        cores = retrieve_rag_examples_for_cores(cores, examples_dir, top_k=top_k)

    relationships: List[str] = []
    # Minimal relationship hints (namespaces and kinds)
    objs = _extract_objects(docs)
    for o in objs:
        relationships.append(f"{_obj_id(o)} present with labels={o.labels} selectors={o.selectors}")

    suggestions = [core.suggestion for core in cores]

    conflicts_text = [f"[{c.type}] {c.description}" for c in conflicts]

    summary = {
        "objects": len(objs),
        "conflicts": len(conflicts),
        "cores": len(cores),
    }

    return {
        "relationships": relationships,
        "conflicts": conflicts_text,
        "suggestions": suggestions,
        "unsat_cores": [
            {
                "core_id": core.core_id,
                "conflict_type": core.conflict_type,
                "constraints": core.constraints,
                "involved": core.involved,
                "suggestion": core.suggestion,
                "examples": core.examples,
            }
            for core in cores
        ],
        "summary": summary,
    }