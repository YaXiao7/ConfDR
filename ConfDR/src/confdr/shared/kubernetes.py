"""Convert Kubernetes manifests or legacy case.json to finite-domain facts; keep missing critical evidence unknown."""

from __future__ import annotations

import copy
import re
from decimal import ROUND_CEILING, Decimal
from itertools import product

import z3

from confdr.shared.io import yaml_text
from confdr.shared.logic import Entity, World
from confdr.shared.resources import extended_requests, populate_resources
from confdr.shared.storage import lookup_claim


def quantity(value, resource: str, *, legacy: bool = True) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Resource quantities must not be booleans")
    if type(value) is int and legacy:
        return value if resource == "cpu" else value * 1024**2
    text = str(value)
    if resource == "cpu":
        factors = {"": 1000, "m": 1, "u": 0.001, "n": 0.000001}
    else:
        factors = {
            "": 1,
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "Pi": 1024**5,
            "K": 1000,
            "k": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", text)
    if not match or match[2] not in factors:
        raise ValueError(f"Invalid resource quantity: {text}")
    return int(
        (Decimal(match[1]) * Decimal(str(factors[match[2]]))).to_integral_value(rounding=ROUND_CEILING)
    )


def selector_matches(selector: dict, labels: dict) -> bool:
    if not all(str(labels.get(k)) == str(v) for k, v in selector.get("matchLabels", {}).items()):
        return False
    for item in selector.get("matchExpressions", []):
        key, op, values = item.get("key"), item.get("operator"), item.get("values", [])
        present, actual = key in labels, str(labels.get(key, ""))
        if op == "In" and not (present and actual in values):
            return False
        if op == "NotIn" and present and actual in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
        if op in {"Gt", "Lt"}:
            try:
                good = (
                    present
                    and len(values) == 1
                    and (int(actual) > int(values[0]) if op == "Gt" else int(actual) < int(values[0]))
                )
            except (TypeError, ValueError, IndexError):
                good = False
            if not good:
                return False
        if op not in {"In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"}:
            return False
    return True


def node_terms(terms: list, name: str, labels: dict) -> bool:
    for term in terms:
        expressions, fields = term.get("matchExpressions", []), term.get("matchFields", [])
        if (
            (expressions or fields)
            and selector_matches({"matchExpressions": expressions}, labels)
            and selector_matches({"matchExpressions": fields}, {"metadata.name": name})
        ):
            return True
    return False


def tolerates(pod: dict, node: dict) -> bool:
    def matches(t, x):
        if x.get("effect") not in (None, "", t.get("effect")):
            return False
        if x.get("operator", "Equal") == "Exists":
            return x.get("key") in (None, "", t.get("key"))
        return x.get("key") == t.get("key") and str(x.get("value", "")) == str(t.get("value", ""))

    return all(
        any(matches(t, x) for x in pod.get("tolerations") or [])
        for t in node.get("taints") or []
        if t.get("effect") == "NoExecute" or (t.get("effect") == "NoSchedule" and not pod.get("nodeName"))
    )


def ports(values: list) -> list[tuple]:
    return [
        (
            str(x.get("protocol") or "TCP").upper(),
            int(x.get("hostPort", x.get("port", 0))),
            x.get("hostIP", ""),
        )
        if isinstance(x, dict)
        else ("TCP", int(x), "")
        for x in values
    ]


def disjoint(first: list, second: list) -> bool:
    return not any(
        a[0:2] == b[0:2] and (a[2] in {"", "0.0.0.0"} or b[2] in {"", "0.0.0.0"} or a[2] == b[2])
        for a in ports(first)
        for b in ports(second)
    )


def make_world(case: dict, *, profile: str | None = None) -> World:
    if not isinstance(case, dict) or not isinstance(case.get("WaitingDeployPods"), dict):
        raise ValueError("Missing WaitingDeployPods snapshot")
    cluster = case.get("ClusterState", {})
    if not isinstance(cluster.get("Nodes"), dict):
        raise ValueError("Missing Nodes snapshot")
    pods, nodes = case["WaitingDeployPods"], cluster["Nodes"]
    volumes, classes = cluster.get("PVs", {}), cluster.get("StorageClasses", {})
    pvcs = cluster.get("PVCs", {})
    claims, uses = {}, set()
    for p, pod in pods.items():
        namespace = pod.get("namespace") or "default"
        for volume in pod.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                key = f"{namespace}/{claim}"
                claims[key] = lookup_claim(pvcs, namespace, claim)
                uses.add((p, key))
    world = World({"Pod": list(pods), "Node": list(nodes), "Claim": list(claims), "Volume": list(volumes)})
    values = world.values
    for p, pod in pods.items():
        values["uses_scheduler", p] = not bool(pod.get("nodeName"))
        values["cpu_request", p] = quantity(pod.get("cpu"), "cpu")
        values["memory_request", p] = quantity(pod.get("mem"), "memory")
        if any(
            values[symbol, p] is not None and values[symbol, p] < 0
            for symbol in ("cpu_request", "memory_request")
        ):
            raise ValueError("Pod resource requests must not be negative")

    for n, node in nodes.items():
        values["free_cpu", n] = quantity(node.get("cpu"), "cpu")
        values["free_memory", n] = quantity(node.get("mem"), "memory")
        values["pod_slots", n] = node.get("remain_pods")
        ready = node.get("ready")
        if ready is None:
            conditions = node.get("conditions", node.get("status", {}).get("conditions", []))
            ready = next(
                (str(c.get("status")).lower() == "true" for c in conditions if c.get("type") == "Ready"), None
            )
        values["node_ready", n] = ready
        unschedulable = node.get("unschedulable", node.get("spec", {}).get("unschedulable"))
        values["node_schedulable", n] = None if unschedulable is None else not unschedulable

    for p, n in product(pods, nodes):
        pod, node = pods[p], nodes[n]
        labels = node.get("labels") or {}
        target_node = pod.get("daemonset_target_node") or pod.get("nodeName")
        values["node_name_ok", p, n] = not target_node or target_node == n
        values["selector_ok", p, n] = (
            None
            if pod.get("nodeSelector") and "labels" not in node
            else selector_matches({"matchLabels": pod.get("nodeSelector") or {}}, labels)
        )
        values["tolerates", p, n] = tolerates(pod, node) if "taints" in node else None
        required = ((pod.get("affinity") or {}).get("nodeAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution"
        )
        values["node_affinity_ok", p, n] = required is None or node_terms(
            required.get("nodeSelectorTerms", []), n, labels
        )
        if required is not None and "labels" not in node:
            values["node_affinity_ok", p, n] = None
        values["host_ports_free", p, n] = (
            None
            if pod.get("host_ports") and "used_ports" not in node
            else disjoint(pod.get("host_ports") or [], node.get("used_ports") or [])
        )
        for mode, symbol in [("podAffinity", "pod_affinity_ok"), ("podAntiAffinity", "pod_anti_affinity_ok")]:
            values[symbol, p, n] = pod_affinity(world, case, p, n, mode)
    for p, q in product(pods, repeat=2):
        values["ports_disjoint", p, q] = disjoint(
            pods[p].get("host_ports") or [], pods[q].get("host_ports") or []
        )
    for p, c in product(pods, claims):
        values["uses", p, c] = (p, c) in uses
    for c, claim in claims.items():
        values["claim_exists", c] = claim is not None
        claim = claim or {}
        fixed = claim.get("boundPV") or claim.get("volumeName")
        values["has_fixed_volume", c] = bool(fixed)
        sc = classes.get(claim.get("storageClassName"), {})
        provisioner = sc.get("provisioner")
        values["dynamic_available", c] = bool(
            provisioner and provisioner != "kubernetes.io/no-provisioner" and not fixed
        )
        for n, node in nodes.items():
            topology = sc.get("allowedTopologies")
            values["dynamic_node_ok", c, n] = (
                True
                if not values["dynamic_available", c]
                else None
                if topology is None and "volumeBindingMode" not in sc
                else not topology
                or any(
                    all(
                        str((node.get("labels") or {}).get(e.get("key"), "")) in e.get("values", [])
                        for e in term.get("matchLabelExpressions", [])
                    )
                    for term in topology
                )
            )
        for v, volume in volumes.items():
            values["fixed_to", c, v] = fixed == v
            values["class_ok", c, v] = (claim.get("storageClassName") or "") == (
                volume.get("storageClassName") or ""
            )
            requested = claim.get("accessModes", claim.get("accessMode"))
            offered = volume.get("accessModes", volume.get("accessMode"))
            values["access_ok", c, v] = (
                None if requested is None or offered is None else (set(requested).issubset(offered))
            )
            values["mode_ok", c, v] = (claim.get("volumeMode") or "Filesystem") == (
                volume.get("volumeMode") or "Filesystem"
            )
            request = storage_quantity(claim, "requestsGi", "request")
            capacity = storage_quantity(volume, "capacityGi", "capacity")
            values["capacity_ok", c, v] = None if request is None or capacity is None else capacity >= request
            phase = volume.get("STATUS", volume.get("phase"))
            ref = volume.get("claimRef") or {}
            same_claim = not ref or f"{ref.get('namespace', 'default')}/{ref.get('name')}" == c
            values["volume_available", c, v] = (
                None if phase is None else same_claim and (phase == "Available" or fixed == v)
            )
    for v, n in product(volumes, nodes):
        required = (volumes[v].get("nodeAffinity") or {}).get("required")
        values["volume_node_ok", v, n] = required is None or node_terms(
            required.get("nodeSelectorTerms", []), n, nodes[n].get("labels") or {}
        )
    populate_resources(world, case)
    from confdr.shared.scheduling import ordered_affinity, populate_spread
    from confdr.shared.storage import populate_storage

    populate_storage(world, case, claims, profile=profile)
    populate_spread(world, case)
    from confdr.shared.volume_limits import populate_volume_limits

    populate_volume_limits(world, case)
    for p, n in product(pods, nodes):
        world.values["scheduling_tolerates", p, n] = (
            tolerates({**pods[p], "nodeName": None}, nodes[n]) if "taints" in nodes[n] else None
        )
        if profile in {"scheduling-v1", "scheduling-v2"}:
            for mode, symbol in [
                ("podAffinity", "pod_affinity_ok"),
                ("podAntiAffinity", "pod_anti_affinity_ok"),
            ]:
                world.values[symbol, p, n] = ordered_affinity(world, case, p, n, mode)
    return world


def storage_quantity(item: dict, gi_field: str, raw_field: str) -> int | None:
    value = item.get(gi_field, item.get(raw_field))
    if gi_field in item and type(value) in {int, float}:
        value = f"{value}Gi"
    return quantity(value, "memory", legacy=False)


def pod_affinity(world: World, case: dict, pod_name: str, node_name: str, mode: str):
    pods, nodes = case["WaitingDeployPods"], case["ClusterState"]["Nodes"]
    pod = pods[pod_name]
    terms = ((pod.get("affinity") or {}).get(mode) or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    if terms and (
        "labels" not in nodes[node_name]
        or "ExistingPods" not in case
        and "ExistingPods" not in case["ClusterState"]
    ):
        return None
    existing = case.get("ExistingPods", case["ClusterState"].get("ExistingPods", {}))
    namespace_labels = case["ClusterState"].get("NamespaceLabels")
    conditions = []
    for term in terms or []:
        key = term.get("topologyKey")
        target = (nodes[node_name].get("labels") or {}).get(key)
        if target is None:
            conditions.append(z3.BoolVal(False))
            continue
        namespaces = set(term.get("namespaces") or [])
        ns_selector = term.get("namespaceSelector")
        if ns_selector is not None:
            if namespace_labels is None:
                return None
            namespaces |= {
                n for n, labels in namespace_labels.items() if selector_matches(ns_selector, labels)
            }
        elif not namespaces:
            namespaces = {pod.get("namespace") or "default"}
        selector = term.get("labelSelector") or {}

        def matches(item, namespaces=namespaces, selector=selector):
            return (item.get("namespace") or "default") in namespaces and selector_matches(
                selector, item.get("labels") or {}
            )

        candidates = []
        matching_anywhere = False
        for item in existing.values():
            if matches(item):
                matching_anywhere = True
                node = nodes.get(item.get("nodeName"), {})
                if (node.get("labels") or {}).get(key) == target:
                    candidates.append(z3.BoolVal(True))
        for other, item in pods.items():
            if other != pod_name and matches(item):
                matching_anywhere = True
                for n, node in nodes.items():
                    if (node.get("labels") or {}).get(key) == target:
                        candidates.append(world.call("assign", [Entity("Pod", other), Entity("Node", n)]))
        if mode == "podAffinity" and not matching_anywhere and matches(pod):
            candidates.append(z3.BoolVal(True))
        found = z3.Or(candidates)
        conditions.append(z3.Not(found) if mode == "podAntiAffinity" else found)
    return z3.And(conditions)


def pod_from_spec(spec: dict, metadata: dict) -> dict:
    """Manifest requests use Kubernetes units; include peak init-container requirements and Pod overhead."""
    requests = []
    for resource in ("cpu", "memory"):
        running, peak = 0, 0
        for container in spec.get("containers", []):
            resources = container.get("resources") or {}
            value = resources.get("requests", {}).get(
                resource, resources.get("limits", {}).get(resource, "0")
            )
            running += quantity(value, resource, legacy=False) or 0
        sidecars = 0
        for container in spec.get("initContainers", []):
            resources = container.get("resources") or {}
            value = resources.get("requests", {}).get(
                resource, resources.get("limits", {}).get(resource, "0")
            )
            amount = quantity(value, resource, legacy=False) or 0
            if container.get("restartPolicy") == "Always":
                sidecars += amount
            peak = max(peak, sidecars + (0 if container.get("restartPolicy") == "Always" else amount))
        total = max(running + sidecars, peak)
        total += quantity(spec.get("overhead", {}).get(resource, "0"), resource, legacy=False) or 0
        requests.append(total)
    return {
        "cpu": requests[0],
        "mem": str(requests[1]),
        "extended_requests": extended_requests(spec),
        "namespace": metadata.get("namespace") or "default",
        "labels": metadata.get("labels") or {},
        "host_ports": [
            port
            for container in spec.get("containers", [])
            for port in container.get("ports", [])
            if port.get("hostPort")
        ],
        **{
            key: spec.get(key)
            for key in (
                "nodeName",
                "nodeSelector",
                "tolerations",
                "affinity",
                "volumes",
                "topologySpreadConstraints",
            )
        },
        "unsupported_features": [
            key for key in ("resourceClaims", "resources", "schedulingGates") if spec.get(key)
        ]
        + (
            ["initContainers.hostPort"]
            if any(
                port.get("hostPort")
                for container in spec.get("initContainers", [])
                for port in container.get("ports", [])
            )
            else []
        ),
    }


def apply_resource_defaults(spec: dict, namespace: str, limit_ranges: list[dict]) -> tuple[dict, list[str]]:
    """Apply observed Container default/defaultRequest values without altering explicit requests."""
    result, errors = copy.deepcopy(spec), []
    defaults = {"limits": {}, "requests": {}}
    for obj in limit_ranges:
        if (obj.get("metadata", {}).get("namespace") or "default") != namespace:
            continue
        for limit in obj.get("spec", {}).get("limits", []):
            if limit.get("type") != "Container":
                continue
            for source, target in (("default", "limits"), ("defaultRequest", "requests")):
                for resource, value in limit.get(source, {}).items():
                    if resource in defaults[target] and defaults[target][resource] != value:
                        errors.append(f"Ambiguous LimitRange {target}/{resource} in namespace {namespace}")
                    defaults[target][resource] = value
    for container in [*result.get("containers", []), *(result.get("initContainers") or [])]:
        resources = container["resources"] = container.get("resources") or {}
        for field in ("requests", "limits"):
            if resources.get(field) is None and field in resources:
                resources[field] = {}
        for resource, value in resources.get("limits", {}).items():
            resources.setdefault("requests", {}).setdefault(resource, value)
        for field, values in defaults.items():
            for resource, value in values.items():
                container.setdefault("resources", {}).setdefault(field, {}).setdefault(resource, value)
    return result, errors


def rebuild_case(documents: list[dict], original: dict, *, document_locations: dict | None = None) -> dict:
    """Reconstruct pending workloads from candidate manifests; disclose environment snapshot reuse in the validation report."""
    from confdr.collection.files import WORKLOADS, manifest_scope, ownership_locations

    if document_locations is None:
        document_locations = ownership_locations(original)
    case = copy.deepcopy(original)
    # New observations cannot be inferred from old ones. Recollect additional predicates after configuration changes.
    case.pop("ExtraFacts", None)
    case["_input_yaml"] = yaml_text(documents)
    case.setdefault("WaitingDeployPods", {})
    cluster = case.setdefault("ClusterState", {})
    unsupported = list(original.get("_unmodelled_admission", []))
    if any(e.get("resource") == "limitranges" for e in original.get("collection", {}).get("errors", [])):
        unsupported.append("Resource default inventory was not observed")
    # Candidate LimitRange manifests supersede snapshots of the same identity.
    limits = {
        (d.get("metadata", {}).get("namespace", "default"), d.get("metadata", {}).get("name")): d
        for d in cluster.get("LimitRanges", [])
    }
    limits.update(
        {
            (d.get("metadata", {}).get("namespace", "default"), d.get("metadata", {}).get("name")): d
            for d in documents
            if d.get("kind") == "LimitRange"
        }
    )
    cluster["LimitRanges"] = list(limits.values())
    # DaemonSet eligibility depends on the final node labels, independent of manifest order.
    for doc in documents:
        if doc.get("kind") != "Node":
            continue
        meta, spec = doc.get("metadata", {}), doc.get("spec", {})
        node = cluster.setdefault("Nodes", {}).setdefault(meta.get("name", ""), {})
        if "labels" in meta:
            node["labels"] = meta["labels"]
        if "taints" in spec:
            node["taints"] = spec["taints"]
        if "unschedulable" in spec:
            node["unschedulable"] = spec["unschedulable"]
    for doc in documents:
        # Environment workloads remain background observations, never new target replicas.
        if doc.get("kind") in WORKLOADS and manifest_scope(doc, document_locations) == "environment":
            continue
        kind, meta, spec = doc.get("kind"), doc.get("metadata", {}), doc.get("spec", {})
        name, namespace = meta.get("name", ""), meta.get("namespace") or "default"
        pod_spec, pod_meta, replicas = None, None, 1
        claim_templates = []
        daemon_nodes = []
        if kind == "Pod":
            pod_spec, pod_meta = spec, meta
        elif kind in {"Deployment", "StatefulSet", "ReplicaSet", "Job"}:
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            pod_meta = {**template.get("metadata", {}), "name": name, "namespace": namespace}
            replicas = spec.get("parallelism", 1) if kind == "Job" else spec.get("replicas", 1)
            if kind == "StatefulSet":
                claim_templates = spec.get("volumeClaimTemplates") or []
                if not isinstance(claim_templates, list):
                    unsupported.append(f"StatefulSet claim templates are invalid: {namespace}/{name}")
                    claim_templates = []
                elif any(not isinstance(t, dict) for t in claim_templates):
                    unsupported.append(f"StatefulSet claim templates are invalid: {namespace}/{name}")
                    claim_templates = [t for t in claim_templates if isinstance(t, dict)]
        elif kind == "CronJob":
            replicas = spec.get("jobTemplate", {}).get("spec", {}).get("parallelism", 1)
            template = spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
            pod_spec = template.get("spec", {})
            pod_meta = {**template.get("metadata", {}), "name": name, "namespace": namespace}
        elif kind == "DaemonSet":
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            pod_meta = {**template.get("metadata", {}), "name": name, "namespace": namespace}
            if pod_spec.get("nodeName"):
                unsupported.append(f"DaemonSet template cannot specify nodeName: {namespace}/{name}")
            if not cluster.get("Nodes"):
                unsupported.append(f"DaemonSet node inventory is missing: {namespace}/{name}")
            required = ((pod_spec.get("affinity") or {}).get("nodeAffinity") or {}).get(
                "requiredDuringSchedulingIgnoredDuringExecution"
            )
            for node_name, node in sorted(cluster.get("Nodes", {}).items()):
                labels = node.get("labels")
                if labels is None:
                    unsupported.append(f"DaemonSet node labels are missing: {node_name}")
                    continue
                if not selector_matches({"matchLabels": pod_spec.get("nodeSelector") or {}}, labels):
                    continue
                if required is not None and not node_terms(required.get("nodeSelectorTerms", []), node_name, labels):
                    continue
                daemon_nodes.append(node_name)
            replicas = len(daemon_nodes)
        elif kind == "PersistentVolumeClaim":
            previous = cluster.get("PVCs", {}).get(namespace, {}).get(name, {})
            observed = {key: previous[key] for key in ("STATUS", "uid", "boundPV") if key in previous}
            if previous.get("STATUS") == "Bound" and previous.get("volumeName"):
                observed["boundPV"] = previous["volumeName"]
                if spec.get("volumeName") not in (None, previous["volumeName"]):
                    unsupported.append(f"Bound PVC reassignment requires API validation: {namespace}/{name}")
            cluster.setdefault("PVCs", {}).setdefault(namespace, {})[name] = {
                **observed,
                **spec,
                "metadata": meta,
                "requestsGi": spec.get("resources", {}).get("requests", {}).get("storage"),
            }
        elif kind == "PersistentVolume":
            previous = cluster.get("PVs", {}).get(name, {})
            cluster.setdefault("PVs", {})[name] = {
                **spec,
                "metadata": meta,
                "labels": meta.get("labels", {}),
                "capacityGi": spec.get("capacity", {}).get("storage"),
                "phase": previous.get(
                    "phase", previous.get("STATUS", doc.get("status", {}).get("phase", "Available"))
                ),
            }
            if previous.get("phase", previous.get("STATUS")) == "Bound" and previous.get("claimRef"):
                cluster["PVs"][name]["claimRef"] = copy.deepcopy(previous["claimRef"])
        elif kind == "CSINode":
            # A manifest is explicit scenario evidence for this node, not a complete API inventory.
            cluster.setdefault("CSINodeOverrides", {})[name] = copy.deepcopy(doc)
        elif kind == "StorageClass":
            cluster.setdefault("StorageClasses", {})[name] = {
                **doc,
                "allowedTopologies": doc.get("allowedTopologies", []),
                "volumeBindingMode": doc.get("volumeBindingMode", "Immediate"),
            }
        if pod_spec is not None:
            pod_spec, default_errors = apply_resource_defaults(pod_spec, namespace, cluster["LimitRanges"])
            unsupported.extend(default_errors)
            if type(replicas) is not int or not 0 <= replicas <= 1000:
                raise ValueError("Replica count exceeds the local validation range")
            # Replace only explicitly associated targets; other pending Pods still compete for resources.
            matched = sorted(
                p
                for p, old in original.get("WaitingDeployPods", {}).items()
                if (old.get("namespace") or "default") == namespace
                and (
                    p.startswith(f"{namespace}/{kind}/{name}/")
                    or old.get("workload") == {"kind": kind, "name": name}
                    or kind == "Pod"
                    and p in {name, f"{namespace}/{name}"}
                )
            )
            if kind != "DaemonSet" and not matched and original.get("WaitingDeployPods"):
                unsupported.append(
                    f"Cannot associate manifest with original target: {namespace}/{kind}/{name}"
                )
            if kind == "Pod" and len(matched) > 1:
                unsupported.append(f"Pod Target association is not unique: {namespace}/{name}")
            for p in matched:
                case["WaitingDeployPods"].pop(p, None)
            for template in claim_templates:
                tmeta, tspec = template.get("metadata") or {}, template.get("spec") or {}
                if not isinstance(tmeta, dict):
                    unsupported.append(f"StatefulSet claim template metadata is invalid: {namespace}/{name}")
                    continue
                tname = tmeta.get("name")
                if not tname or not isinstance(tspec, dict):
                    unsupported.append(f"StatefulSet claim template is incomplete: {namespace}/{name}")
                    continue
                if set(tspec) - {"accessModes", "resources", "storageClassName", "volumeMode", "selector"}:
                    unsupported.append(f"StatefulSet claim template has unmodelled fields: {namespace}/{name}/{tname}")
                if any(v.get("name") == tname for v in pod_spec.get("volumes") or []):
                    unsupported.append(f"StatefulSet claim volume conflicts with Pod template: {namespace}/{name}/{tname}")
                for i in range(replicas):
                    claim_name = f"{tname}-{name}-{i}"
                    previous = cluster.get("PVCs", {}).get(namespace, {}).get(claim_name, {})
                    observed = {k: previous[k] for k in ("STATUS", "uid", "boundPV", "volumeName") if k in previous}
                    cluster.setdefault("PVCs", {}).setdefault(namespace, {})[claim_name] = {
                        **copy.deepcopy(tspec),
                        **observed,
                        "metadata": {**copy.deepcopy(tmeta), "name": claim_name, "namespace": namespace},
                        "requestsGi": tspec.get("resources", {}).get("requests", {}).get("storage"),
                        "_template_source": f"{namespace}/{name}",
                    }
            for i in range(replicas):
                key = (
                    f"{namespace}/{kind}/{name}/{daemon_nodes[i]}"
                    if kind == "DaemonSet"
                    else matched[i] if i < len(matched) else f"{namespace}/{kind}/{name}/{i}"
                )
                ordinal_spec = copy.deepcopy(pod_spec)
                for template in claim_templates:
                    tmeta = template.get("metadata") or {}
                    tname = tmeta.get("name") if isinstance(tmeta, dict) else None
                    if tname and not any(v.get("name") == tname for v in ordinal_spec.get("volumes") or []):
                        ordinal_spec.setdefault("volumes", []).append({
                            "name": tname,
                            "persistentVolumeClaim": {"claimName": f"{tname}-{name}-{i}"},
                        })
                case["WaitingDeployPods"][key] = pod_from_spec(ordinal_spec, pod_meta)
                case["WaitingDeployPods"][key]["workload"] = {"kind": kind, "name": name}
                if kind == "DaemonSet":
                    case["WaitingDeployPods"][key]["daemonset_target_node"] = daemon_nodes[i]
                unsupported.extend(
                    f"{key}: {feature}" for feature in case["WaitingDeployPods"][key]["unsupported_features"]
                )
    case["_unsupported_manifest_features"] = unsupported
    return case
