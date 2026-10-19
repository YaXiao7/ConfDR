"""Independent collector: read the specified cluster and produce case and Pod observations for the target configuration."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from confdr.collection.files import WORKLOADS, manifest_scope, object_key
from confdr.shared.io import digest
from confdr.shared.kubernetes import pod_from_spec, quantity, rebuild_case
from confdr.shared.resources import extended_quantity


class CollectionError(RuntimeError):
    pass


class Collector:
    def __init__(
        self, kubeconfig: str | Path, *, kubectl: str = "kubectl", timeout: int = 30, runner=subprocess.run
    ):
        if not kubeconfig:
            raise ValueError("Collection requires an explicit kubeconfig")
        self.kubeconfig = str(kubeconfig)
        self.kubectl, self.timeout, self.runner = kubectl, timeout, runner

    def command(self, args: list[str]) -> str:
        try:
            p = self.runner(
                [self.kubectl, "--kubeconfig", self.kubeconfig, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectionError(str(exc)) from exc
        if p.returncode:
            raise CollectionError((p.stderr or p.stdout or "kubectl failed")[:2000])
        return p.stdout

    def collect(
        self,
        documents: list[dict],
        *,
        logs: bool = True,
        scope: str = "live_cluster",
        document_locations: dict | None = None,
    ) -> tuple[dict, dict]:
        resources, errors = {}, []
        names = {
            "nodes": "nodes",
            "pods": "pods",
            "pvcs": "pvc",
            "pvs": "pv",
            "storageclasses": "storageclasses",
            "csinodes": "csinodes",
            "namespaces": "namespaces",
            "limitranges": "limitranges",
            "events": "events",
        }
        kinds = {d.get("kind") for d in documents}
        if "Deployment" in kinds:
            names["replicasets"] = "replicasets"
        if "CronJob" in kinds:
            names["jobs"] = "jobs"
        for name, resource in names.items():
            try:
                value = json.loads(self.command(["get", resource, "-A", "-o", "json"]))
                if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                    raise ValueError("kubectl response is missing items")
                resources[name] = value["items"]
            except (CollectionError, ValueError) as exc:
                errors.append({"resource": name, "error": str(exc)})
        if any(name not in resources for name in ("nodes", "pods")):
            raise CollectionError("Cannot collect nodes or Pods: " + json.dumps(errors, ensure_ascii=False))
        case, observations = build_snapshot(
            documents, resources, errors=errors, scope=scope, document_locations=document_locations
        )
        if logs:
            for key, status in observations["pod_status"].items():
                if not status.get("actual_name"):
                    continue
                for container in [
                    *(status.get("containerStatuses") or []),
                    *(status.get("initContainerStatuses") or []),
                ]:
                    name = container.get("name")
                    if not name:
                        continue
                    entry = {}
                    for previous in (False, True) if container.get("restartCount", 0) else (False,):
                        label = "previous" if previous else "current"
                        args = [
                            "logs",
                            status["actual_name"],
                            "-n",
                            status["namespace"],
                            "-c",
                            name,
                            "--tail=200",
                            "--limit-bytes=16000",
                        ]
                        if previous:
                            args.append("--previous")
                        try:
                            entry[label] = self.command(args)
                        except CollectionError as exc:
                            entry[label + "_error"] = str(exc)
                    observations.setdefault("container_logs", {}).setdefault(key, {})[name] = entry
        return case, observations


def build_snapshot(
    documents: list[dict],
    resources: dict,
    *,
    errors: list | None = None,
    scope: str = "live_cluster",
    document_locations: dict | None = None,
) -> tuple[dict, dict]:
    """Exclude target Pods from background resource usage and count them once as requests to check."""
    errors = list(errors or [])
    observed_at = datetime.now(timezone.utc).isoformat()
    config_hash = digest(documents)
    cluster: dict = {"Nodes": {}, "ExistingPods": {}}
    defaults = resources.get("limitranges", [d for d in documents if d.get("kind") == "LimitRange"])
    cluster["LimitRanges"] = copy.deepcopy(defaults)
    desired = rebuild_case(
        documents,
        {
            "WaitingDeployPods": {},
            "ClusterState": {
                "Nodes": {
                    node["metadata"]["name"]: {
                        "labels": copy.deepcopy(node["metadata"].get("labels", {})),
                        "taints": copy.deepcopy(node.get("spec", {}).get("taints", [])),
                    }
                    for node in resources.get("nodes", [])
                },
                "LimitRanges": defaults,
            },
        },
        document_locations=document_locations,
    )
    if any(e.get("resource") == "limitranges" for e in errors):
        desired.setdefault("_unsupported_manifest_features", []).append(
            "Resource default inventory was not observed"
        )
    waiting = desired["WaitingDeployPods"]
    targets = {
        object_key(d): d
        for d in documents
        if d.get("kind") in WORKLOADS and manifest_scope(d, document_locations) == "application"
    }
    owners = {}
    for name in ("replicasets", "jobs"):
        for item in resources.get(name, []):
            owners[object_key(item)] = item

    def belongs(pod):
        meta = pod.get("metadata", {})
        namespace = meta.get("namespace") or "default"
        direct = ("Pod", namespace, meta.get("name"))
        if direct in targets:
            return direct
        refs = list(meta.get("ownerReferences") or [])
        seen = set()
        while refs:
            owner = refs.pop()
            key = owner.get("kind"), namespace, owner.get("name")
            if key in seen:
                continue
            seen.add(key)
            if key in targets:
                return key
            obj = owners.get(key, {})
            if owner.get("uid") and obj.get("metadata", {}).get("uid") != owner["uid"]:
                continue
            refs.extend(obj.get("metadata", {}).get("ownerReferences") or [])
        return None

    target_live, background = {}, []
    for pod in resources.get("pods", []):
        owner = belongs(pod)
        if owner:
            target_live.setdefault(owner, []).append(pod)
        else:
            background.append(pod)
    for pod in waiting.values():
        workload = pod["workload"]
        pod["namespace"] = pod.get("namespace") or "default"
        key = workload["kind"], pod["namespace"], workload["name"]
        # Associate actual Pod names with controllers below; do not treat random replica names as logical entity names.
        target_live.setdefault(key, [])

    for node in resources.get("nodes", []):
        meta, spec, status = node.get("metadata", {}), node.get("spec", {}), node.get("status", {})
        alloc = status.get("allocatable", {})
        cpu = quantity(alloc.get("cpu"), "cpu", legacy=False)
        memory = quantity(alloc.get("memory"), "memory", legacy=False)
        slots = int(alloc["pods"]) if alloc.get("pods") is not None else None
        ready = next(
            (
                str(c.get("status")).lower() == "true"
                for c in status.get("conditions", [])
                if c.get("type") == "Ready"
            ),
            None,
        )
        info = {
            "cpu": cpu,
            "mem": str(memory) if memory is not None else None,
            "remain_pods": slots,
            "ready": ready,
            "conditions": copy.deepcopy(status.get("conditions", [])),
            "unschedulable": bool(spec.get("unschedulable", False)),
            "labels": copy.deepcopy(meta.get("labels", {})),
            "taints": copy.deepcopy(spec.get("taints", [])),
            "used_ports": [],
            "allocatable": copy.deepcopy(alloc),
            "extended_free": {r: extended_quantity(v) for r, v in alloc.items() if "/" in r},
        }
        cluster["Nodes"][meta["name"]] = info
    for pod in background:
        spec, meta, status = pod.get("spec", {}), pod.get("metadata", {}), pod.get("status", {})
        node_name = spec.get("nodeName")
        if not node_name or status.get("phase") in {"Succeeded", "Failed"}:
            continue
        parsed = pod_from_spec(spec, meta)
        key = (meta.get("namespace") or "default") + "/" + meta["name"]
        cluster["ExistingPods"][key] = {**parsed, "nodeName": node_name, "uid": meta.get("uid")}
        node = cluster["Nodes"].get(node_name)
        if node is None:
            continue
        if node["cpu"] is not None:
            node["cpu"] -= parsed["cpu"]
        if node["mem"] is not None:
            node["mem"] = str(int(node["mem"]) - int(parsed["mem"]))
        if node["remain_pods"] is not None:
            node["remain_pods"] -= 1
        node["used_ports"].extend(parsed["host_ports"])
        for resource, amount in parsed["extended_requests"].items():
            node["extended_free"][resource] = node["extended_free"].get(resource, 0) - amount
    # Negative remaining capacity indicates overcommit; retain a nonnegative available budget and overcommit records without inventing capacity.
    for name, node in cluster["Nodes"].items():
        for resource, amount in node["extended_free"].items():
            if amount < 0:
                errors.append(
                    {
                        "resource": f"node/{name}/{resource}",
                        "error": "background_overcommitted",
                        "value": amount,
                    }
                )
                node["extended_free"][resource] = 0
        for field in ("cpu", "mem", "remain_pods"):
            if node[field] is not None and int(node[field]) < 0:
                errors.append(
                    {
                        "resource": f"node/{name}/{field}",
                        "error": "background_overcommitted",
                        "value": node[field],
                    }
                )
                node[field] = "0" if field == "mem" else 0
    if "pvcs" in resources:
        # Keep claims derived from StatefulSet templates even when OrderedReady
        # has not yet created later ordinals. These are desired claims, not
        # observations; the live API inventory below always takes precedence.
        derived = {
            ns: {
                name: {**copy.deepcopy(claim), "STATUS": "NotObserved"}
                for name, claim in claims.items() if claim.get("_template_source")
            }
            for ns, claims in desired.get("ClusterState", {}).get("PVCs", {}).items()
        }
        cluster["PVCs"] = derived
        for claim in resources["pvcs"]:
            meta, spec = claim["metadata"], copy.deepcopy(claim.get("spec", {}))
            namespace = meta.get("namespace") or "default"
            source = derived.get(namespace, {}).get(meta["name"], {}).get("_template_source")
            spec.update(
                metadata=copy.deepcopy(meta),
                uid=meta.get("uid"),
                requestsGi=spec.get("resources", {}).get("requests", {}).get("storage"),
                STATUS=claim.get("status", {}).get("phase"),
            )
            if source:
                spec["_template_source"] = source
            # Preserve volumeName whether explicitly configured or assigned by an actual binding.
            cluster["PVCs"].setdefault(namespace, {})[meta["name"]] = spec
    if "pvs" in resources:
        cluster["PVs"] = {
            x["metadata"]["name"]: {
                **copy.deepcopy(x.get("spec", {})),
                "metadata": copy.deepcopy(x["metadata"]),
                "labels": copy.deepcopy(x["metadata"].get("labels", {})),
                "capacityGi": x.get("spec", {}).get("capacity", {}).get("storage"),
                "phase": x.get("status", {}).get("phase"),
            }
            for x in resources["pvs"]
        }
    if "storageclasses" in resources:
        cluster["StorageClasses"] = {
            x["metadata"]["name"]: {
                "provisioner": x.get("provisioner"),
                "metadata": copy.deepcopy(x["metadata"]),
                "parameters": copy.deepcopy(x.get("parameters", {})),
                "volumeBindingMode": x.get("volumeBindingMode", "Immediate"),
                "allowedTopologies": copy.deepcopy(x.get("allowedTopologies", [])),
            }
            for x in resources["storageclasses"]
        }
    if "csinodes" in resources:
        cluster["CSINodes"] = {
            x["metadata"]["name"]: {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "CSINode",
                "metadata": {"name": x["metadata"]["name"]},
                "spec": copy.deepcopy(x.get("spec", {})),
            }
            for x in resources["csinodes"]
        }
    if "namespaces" in resources:
        cluster["NamespaceLabels"] = {
            x["metadata"]["name"]: x["metadata"].get("labels", {}) for x in resources["namespaces"]
        }
    info = {
        "scope": scope,
        "observed_at": observed_at,
        "candidate_hash": config_hash,
        "errors": errors,
        "resource_accounting": "allocatable_minus_background_only; targets_counted_once",
        "atomic_snapshot": False,
    }
    case = {
        "WaitingDeployPods": waiting,
        "_manifest_ownership": [
            {"identity": list(object_key(d)), "scope": manifest_scope(d, document_locations)}
            for d in documents
        ],
        "ClusterState": cluster,
        "collection": info,
        "_unsupported_manifest_features": desired.get("_unsupported_manifest_features", []),
    }
    observations = {
        "pod_status": {},
        "pod_events": {},
        "pvc_status": {},
        "container_logs": {},
        "observed_at": observed_at,
        "candidate_hash": config_hash,
        "collection": copy.deepcopy(info),
    }
    for key, pods in target_live.items():
        logical = sorted(
            name
            for name, p in waiting.items()
            if (p["workload"]["kind"], p["namespace"], p["workload"]["name"]) == key
        )
        pods = sorted(pods, key=lambda x: x.get("metadata", {}).get("name", ""))
        if len(pods) > len(logical):
            case["_unsupported_manifest_features"].append(
                f"Target replicas have not stabilized: {key}, actual {len(pods)}, declared {len(logical)}"
            )
        for index, name in enumerate(logical):
            if index >= len(pods):
                observations["pod_status"][name] = {
                    "namespace": key[1],
                    "phase": "NotObserved",
                    "actual_name": None,
                }
                observations["pod_events"][name] = []
                continue
            pod = pods[index]
            # Admission may default requests or inject containers. Use observed effective resources;
            # retain desired placement intent (scheduled nodeName must not bypass scheduler rules).
            effective = pod_from_spec(pod.get("spec", {}), pod["metadata"])
            if any(effective[field] != waiting[name][field] for field in ("cpu", "mem", "extended_requests")):
                note = (
                    f"Unmodelled admission resource mutation: {name}; recollect candidates before acceptance"
                )
                case.setdefault("_unmodelled_admission", []).append(note)
            for field in ("cpu", "mem", "extended_requests"):
                waiting[name][field] = copy.deepcopy(effective[field])
            waiting[name]["resource_source"] = "observed_admitted_pod"
            case["_unsupported_manifest_features"].extend(
                f"{name}: {feature}" for feature in effective.get("unsupported_features", [])
            )
            meta = pod["metadata"]
            status = copy.deepcopy(pod.get("status", {}))
            status.update(
                namespace=key[1],
                actual_name=meta["name"],
                uid=meta.get("uid"),
                nodeName=pod.get("spec", {}).get("nodeName"),
            )
            observations["pod_status"][name] = status
            events = []
            for event in resources.get("events", []):
                ref = event.get("involvedObject") or event.get("regarding") or {}
                if (
                    ref.get("kind") != "Pod"
                    or ref.get("name") != meta["name"]
                    or (ref.get("namespace") or "default") != key[1]
                ):
                    continue
                if ref.get("uid") and meta.get("uid") and ref["uid"] != meta["uid"]:
                    continue
                events.append(
                    {
                        "reason": event.get("reason"),
                        "message": event.get("message") or event.get("note"),
                        "type": event.get("type"),
                        "count": event.get("count", 1),
                        "lastTimestamp": event.get("lastTimestamp") or event.get("eventTime"),
                    }
                )
            observations["pod_events"][name] = events
    referenced = {
        (p.get("namespace") or "default", v["persistentVolumeClaim"]["claimName"])
        for p in waiting.values()
        for v in p.get("volumes") or []
        if (v.get("persistentVolumeClaim") or {}).get("claimName")
    }
    referenced |= {
        (object_key(d)[1], object_key(d)[2])
        for d in documents
        if d.get("kind") == "PersistentVolumeClaim" and manifest_scope(d, document_locations) == "application"
    }
    for namespace, name in sorted(referenced):
        c = cluster.get("PVCs", {}).get(namespace, {}).get(name)
        observations["pvc_status"][namespace + "/" + name] = {
            "phase": c.get("STATUS") if c else "NotObserved",
            "volumeName": c.get("volumeName") if c else None,
        }
    observations["is_pending"] = any(s.get("phase") == "Pending" for s in observations["pod_status"].values())
    return case, observations
