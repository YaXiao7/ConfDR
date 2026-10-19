"""Temporary KWOK clusters for independent deployment evaluation and data collection; do not determine repair correctness."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from confdr.collection.collector import CollectionError, Collector
from confdr.shared.io import digest, yaml_text
from confdr.shared.kubernetes import quantity

MARKER = "confdr.io/validation-run"
ENVIRONMENT = "confdr.io/environment"
WORKLOAD_KINDS = {"Pod", "Deployment", "StatefulSet", "ReplicaSet", "Job", "DaemonSet"}
CLUSTER_SCOPED = {"Namespace", "Node", "PersistentVolume", "StorageClass", "CSINode"}


class CandidateDeploymentError(RuntimeError):
    """Candidate manifests were rejected by the API; this rejection should inform the next repair round."""


class KwokInfrastructureError(RuntimeError):
    """KWOK or kubectl is unavailable; this is not repair feedback on the candidate YAML."""


def _object_key(doc: dict) -> tuple[str, str, str]:
    metadata = doc.get("metadata") or {}
    kind = doc.get("kind", "")
    namespace = "" if kind in CLUSTER_SCOPED else metadata.get("namespace") or "default"
    return kind, namespace, metadata.get("name", "")


def _safe_name(value: str, *, limit: int = 63) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    value = re.sub(r"-+", "-", value) or "confdr"
    return value[:limit].rstrip("-")


def _resource(value, unit: str, default: int) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid KWOK node resource quantity: {value!r}")
    return f"{max(0, value):g}{unit}"


def _namespace_names(documents: list[dict], case: dict) -> set[str]:
    names = {
        (doc.get("metadata") or {}).get("namespace") or "default"
        for doc in documents
        if doc.get("kind") not in {"Namespace", "Node", "PersistentVolume", "StorageClass", "CSINode"}
    }
    cluster = case.get("ClusterState", {})
    names.update(_claims_by_namespace(cluster.get("PVCs") or {}).keys())
    for pod in (case.get("ExistingPods") or cluster.get("ExistingPods") or {}).values():
        names.add(pod.get("namespace") or "default")
    return {name for name in names if isinstance(name, str) and name}


def _namespaces(documents: list[dict], case: dict) -> list[dict]:
    existing = {doc.get("metadata", {}).get("name") for doc in documents if doc.get("kind") == "Namespace"}
    names = _namespace_names(documents, case)
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": copy.deepcopy(
                    case.get("ClusterState", {}).get("NamespaceLabels", {}).get(name, {})
                ),
            },
        }
        for name in sorted(names - existing - {"default", "kube-system", "kube-public", "kube-node-lease"})
        if isinstance(name, str) and name
    ]


def _claims_by_namespace(pvcs: dict) -> dict:
    """Support legacy default/name nesting and some flat PVC snapshots."""
    claim_fields = {"accessModes", "accessMode", "storageClassName", "requestsGi", "volumeName"}
    if pvcs and all(isinstance(value, dict) and claim_fields.intersection(value) for value in pvcs.values()):
        return {"default": pvcs}
    return pvcs


def environment_documents(case: dict, candidate: list[dict]) -> list[dict]:
    """Convert ConfDR snapshots into minimal environment manifests accepted by KWOK."""
    cluster = case.get("ClusterState") or {}
    candidate_keys = {_object_key(doc) for doc in candidate}
    documents: list[dict] = _namespaces(candidate, case)
    for namespace in sorted(_namespace_names(candidate, case)):
        if ("ServiceAccount", namespace, "default") not in candidate_keys:
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "default", "namespace": namespace},
                }
            )
    nodes = cluster.get("Nodes") or {}
    existing = case.get("ExistingPods") or cluster.get("ExistingPods") or {}
    existing_per_node: dict[str, int] = {}
    for pod in existing.values():
        if pod.get("nodeName"):
            existing_per_node[pod["nodeName"]] = existing_per_node.get(pod["nodeName"], 0) + 1

    for name, node in nodes.items():
        if ("Node", "", name) in candidate_keys:
            continue
        cpu = node.get("cpu", node.get("alloc_cpu_m"))
        memory = node.get("mem", node.get("alloc_mem_mi"))
        remaining = node.get("remain_pods", node.get("alloc_pods"))
        if remaining is None or cpu is None or memory is None:
            raise ValueError("KWOK node restoration requires explicit CPU, memory and Pod capacities")
        background = [p for p in existing.values() if p.get("nodeName") == name]
        cpu = f"{quantity(cpu, 'cpu') + sum(quantity(p.get('cpu', 0), 'cpu') or 0 for p in background)}m"
        memory = str(
            quantity(memory, "memory") + sum(quantity(p.get("mem", 0), "memory") or 0 for p in background)
        )
        pod_capacity = (
            max(0, int(remaining))
            + existing_per_node.get(name, 0)
            + (bool(node.get("used_ports")) and not existing_per_node.get(name))
        )
        ready = node.get("ready")
        status = {
            "capacity": {
                "cpu": _resource(cpu, "m", 1000),
                "memory": _resource(memory, "Mi", 1024),
                "pods": str(pod_capacity),
            },
            "allocatable": {
                "cpu": _resource(cpu, "m", 1000),
                "memory": _resource(memory, "Mi", 1024),
                "pods": str(pod_capacity),
            },
        }
        if ready is not None:
            status["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]
        labels = copy.deepcopy(node.get("labels") or {})
        labels.setdefault("kubernetes.io/hostname", name)
        annotations = {}
        # Do not let KWOK stages maintain heartbeats for Ready=false nodes; otherwise the stage would reset the snapshot state to Ready.
        if ready is not False:
            annotations["kwok.x-k8s.io/node"] = "fake"
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Node",
                "metadata": {
                    "name": name,
                    "labels": labels,
                    **({"annotations": annotations} if annotations else {}),
                },
                "spec": {
                    "taints": copy.deepcopy(node.get("taints") or []),
                    "unschedulable": bool(node.get("unschedulable", False)),
                },
                "status": status,
            }
        )

    for name, item in {**cluster.get("CSINodeOverrides", {}), **cluster.get("CSINodes", {})}.items():
        if ("CSINode", "", name) not in candidate_keys:
            documents.append(
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "CSINode",
                    "metadata": {"name": name},
                    "spec": copy.deepcopy(item.get("spec", {})),
                }
            )

    for name, storage_class in (cluster.get("StorageClasses") or {}).items():
        if ("StorageClass", "", name) in candidate_keys:
            continue
        documents.append(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": name},
                "provisioner": storage_class.get("provisioner") or "kubernetes.io/no-provisioner",
                "volumeBindingMode": storage_class.get("volumeBindingMode", "Immediate"),
                **(
                    {"allowedTopologies": copy.deepcopy(storage_class["allowedTopologies"])}
                    if storage_class.get("allowedTopologies")
                    else {}
                ),
            }
        )

    for name, volume in (cluster.get("PVs") or {}).items():
        if ("PersistentVolume", "", name) in candidate_keys:
            continue
        capacity = volume.get("capacityGi", (volume.get("capacity") or {}).get("storage", "1Gi"))
        spec = {
            "capacity": {"storage": _resource(capacity, "Gi", 1)},
            "accessModes": copy.deepcopy(
                volume.get("accessModes", volume.get("accessMode") or ["ReadWriteOnce"])
            ),
            "persistentVolumeReclaimPolicy": volume.get("persistentVolumeReclaimPolicy", "Retain"),
            "hostPath": {"path": f"/tmp/confdr/{_safe_name(name)}"},
        }
        if volume.get("csi"):
            spec.pop("hostPath")
            spec["csi"] = copy.deepcopy(volume["csi"])
        for field in ("storageClassName", "volumeMode", "nodeAffinity", "claimRef"):
            if volume.get(field) is not None:
                spec[field] = copy.deepcopy(volume[field])
        documents.append(
            {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name}, "spec": spec}
        )

    pvcs = _claims_by_namespace(cluster.get("PVCs") or {})
    for namespace, claims in pvcs.items():
        if not isinstance(claims, dict):
            continue
        for name, claim in claims.items():
            if not isinstance(claim, dict) or ("PersistentVolumeClaim", namespace, name) in candidate_keys:
                continue
            if claim.get("_template_source"):
                # The StatefulSet controller, not environment restoration,
                # owns these PVCs (including ordinals not created yet).
                continue
            request = claim.get(
                "requestsGi",
                ((claim.get("resources") or {}).get("requests") or {}).get("storage", "1Gi"),
            )
            spec = {
                "accessModes": copy.deepcopy(
                    claim.get("accessModes", claim.get("accessMode") or ["ReadWriteOnce"])
                ),
                "resources": {"requests": {"storage": _resource(request, "Gi", 1)}},
            }
            for field in ("storageClassName", "volumeMode", "volumeName", "selector"):
                if claim.get(field) is not None:
                    spec[field] = copy.deepcopy(claim[field])
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": spec,
                }
            )

    for original_name, pod in existing.items():
        node_name = pod.get("nodeName")
        if not node_name:
            continue

        # ``ExistingPods`` serves two distinct purposes in KWOK restoration:
        #   1. above, its requests are added back to the recorded free budget to
        #      reconstruct the original Node allocatable capacity; and
        #   2. here, background Pods that are not otherwise represented are
        #      materialized as synthetic ``confdr-existing-*`` Pods so the
        #      scheduler sees their resource consumption.
        #
        # If the same logical Pod is already present in the supplied environment
        # manifests (normally under envs/), purpose (2) must be skipped. Otherwise
        # KWOK would deploy both the real environment Pod and a synthetic copy,
        # double-counting its requests while the Node capacity was restored only
        # once from the snapshot. Keep the ExistingPods entry itself untouched so
        # purpose (1) and all formal/snapshot semantics remain intact.
        namespace = pod.get("namespace") or "default"
        original_pod_name = original_name
        if isinstance(original_name, str) and "/" in original_name:
            key_namespace, original_pod_name = original_name.split("/", 1)
            namespace = pod.get("namespace") or key_namespace or "default"

        if ("Pod", namespace, original_pod_name) in candidate_keys:
            continue

        name = _safe_name(f"confdr-existing-{original_name}-{digest([namespace, original_name])[:8]}")
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {**copy.deepcopy(pod.get("labels") or {}), ENVIRONMENT: "true"},
                },
                "spec": {
                    "nodeName": node_name,
                    "volumes": copy.deepcopy(pod.get("volumes") or []),
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {
                            "name": "placeholder",
                            "image": "registry.k8s.io/pause:3.9",
                            "resources": {
                                "requests": {
                                    "cpu": f"{quantity(pod.get('cpu', 0), 'cpu') or 0}m",
                                    "memory": str(quantity(pod.get("mem", 0), "memory") or 0),
                                }
                            },
                        }
                    ],
                },
            }
        )

    for node_name, node in nodes.items():
        used_ports = node.get("used_ports") or []
        if not used_ports:
            continue
        ports = []
        for index, value in enumerate(used_ports):
            item = value if isinstance(value, dict) else {"hostPort": value}
            ports.append(
                {
                    "name": f"p{index}",
                    "containerPort": int(item.get("containerPort", item.get("hostPort"))),
                    "hostPort": int(item.get("hostPort", item.get("port"))),
                    "protocol": str(item.get("protocol") or "TCP").upper(),
                    **({"hostIP": item["hostIP"]} if item.get("hostIP") else {}),
                }
            )
        holder = next(
            (
                d
                for d in documents
                if d.get("kind") == "Pod"
                and d.get("spec", {}).get("nodeName") == node_name
                and d.get("metadata", {}).get("labels", {}).get(ENVIRONMENT) == "true"
            ),
            None,
        )
        if holder is not None:
            holder["spec"]["containers"][0]["ports"] = ports
            continue
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": _safe_name(f"confdr-ports-{node_name}-{digest(used_ports)[:8]}"),
                    "namespace": "default",
                    "labels": {ENVIRONMENT: "true"},
                },
                "spec": {
                    "nodeName": node_name,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {"name": "port-holder", "image": "registry.k8s.io/pause:3.9", "ports": ports}
                    ],
                },
            }
        )
    return documents


def instrument(documents: list[dict], token: str) -> list[dict]:
    """Label Pod templates of objects to validate solely for precise result collection in the isolated cluster."""
    result = copy.deepcopy(documents)
    for doc in result:
        kind = doc.get("kind")
        if kind == "Pod":
            metadata = doc.setdefault("metadata", {})
        elif kind in WORKLOAD_KINDS:
            metadata = doc.setdefault("spec", {}).setdefault("template", {}).setdefault("metadata", {})
        elif kind == "CronJob":
            metadata = (
                doc.setdefault("spec", {})
                .setdefault("jobTemplate", {})
                .setdefault("spec", {})
                .setdefault("template", {})
                .setdefault("metadata", {})
            )
        elif kind == "PersistentVolumeClaim":
            metadata = doc.setdefault("metadata", {})
        else:
            continue
        metadata.setdefault("labels", {})[MARKER] = token
    return result


def expected_targets(documents: list[dict], node_count: int) -> tuple[int, int]:
    pods = claims = 0
    for doc in documents:
        kind, spec = doc.get("kind"), doc.get("spec") or {}
        if kind == "Pod":
            pods += 1
        elif kind in {"Deployment", "StatefulSet", "ReplicaSet"}:
            pods += int(spec.get("replicas", 1))
        elif kind == "Job":
            pods += int(spec.get("parallelism", 1))
        elif kind == "CronJob":
            pods += int(spec.get("jobTemplate", {}).get("spec", {}).get("parallelism", 1))
        elif kind == "DaemonSet":
            pods += node_count
        elif kind == "PersistentVolumeClaim":
            claims += 1
    return pods, claims


class KwokValidator:
    """Create an explicitly named temporary cluster, pass all manifests via stdin and clean up afterward."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        runner: Callable = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        cfg = config or {}
        self.kwokctl = str(cfg.get("kwokctl", "kwokctl"))
        self.kubectl = str(cfg.get("kubectl", "kubectl"))
        self.prefix = _safe_name(str(cfg.get("cluster_prefix", "confdr")), limit=36)
        self.create_args = cfg.get("create_args", [])
        if not isinstance(self.create_args, list) or not all(isinstance(x, str) for x in self.create_args):
            raise ValueError("kwok.create_args must be a list of strings")
        if any(
            x.split("=", 1)[0] in {"--name", "--kubeconfig", "--all", "--force"} for x in self.create_args
        ):
            raise ValueError(
                "kwok.create_args must not override the cluster name, kubeconfig or cleanup scope"
            )
        self.create_timeout = int(cfg.get("create_timeout_seconds", 120))
        self.command_timeout = int(cfg.get("command_timeout_seconds", 30))
        self.validation_timeout = int(cfg.get("validation_timeout_seconds", 45))
        self.poll_interval = float(cfg.get("poll_interval_seconds", 2))
        self.original_failure_settle = float(cfg.get("original_failure_settle_seconds", 10))
        if min(self.create_timeout, self.command_timeout, self.validation_timeout, self.poll_interval) <= 0:
            raise ValueError("KWOK timeouts and polling intervals must be positive")
        if self.original_failure_settle < 0:
            raise ValueError("original_failure_settle_seconds must be nonnegative")
        self.runner, self.clock, self.sleeper = runner, clock, sleeper

    def probe(self, documents: list[dict], case: dict, *, phase: str = "candidate") -> dict:
        """Create an independent temporary cluster each time without calling the generator or overwriting original data."""
        if self.runner is subprocess.run:
            missing = [c for c in (self.kwokctl, self.kubectl) if shutil.which(c) is None]
            if missing:
                return {
                    "status": "error",
                    "scope": "simulated_control_plane",
                    "reason": "missing_commands: " + ", ".join(missing),
                }
        result = self._phase(documents, case, phase=phase)
        result["candidate_hash"] = digest(documents)
        return result

    def _phase(self, documents: list[dict], case: dict, *, phase: str) -> dict:
        from confdr.collection.files import manifest_scope, ownership_locations

        locations = ownership_locations(case)
        token = digest([phase, documents])[:12]
        name = _safe_name(f"{self.prefix}-{uuid.uuid4().hex[:12]}")
        attempted = False
        api_accepted = None
        cleanup_error = None
        result: dict
        with tempfile.TemporaryDirectory(prefix="confdr-kwok-") as directory:
            kubeconfig = Path(directory) / "kubeconfig"
            try:
                attempted = True
                self._command(
                    [
                        self.kwokctl,
                        "create",
                        "cluster",
                        "--name",
                        name,
                        "--kubeconfig",
                        str(kubeconfig),
                        "--wait",
                        f"{self.create_timeout}s",
                        *self.create_args,
                    ],
                    timeout=self.create_timeout + 15,
                )
                # Node manifests are explicit experimental environment inputs. Restore the status subresource and let KWOK manage simulated heartbeats.
                workload_docs = [d for d in documents if manifest_scope(d, locations) == "application"]
                environment = environment_documents(case, documents)
                environment += [
                    copy.deepcopy(d)
                    for d in documents
                    if d.get("kind") != "Node" and manifest_scope(d, locations) == "environment"
                ]
                for node in (copy.deepcopy(d) for d in documents if d.get("kind") == "Node"):
                    name_in_case = node.get("metadata", {}).get("name")
                    fallback = next(
                        (
                            d
                            for d in environment_documents(case, workload_docs)
                            if d.get("kind") == "Node" and d.get("metadata", {}).get("name") == name_in_case
                        ),
                        {},
                    )
                    node.setdefault("status", copy.deepcopy(fallback.get("status", {})))
                    conditions = node["status"].get("conditions", [])
                    ready = next(
                        (
                            str(c.get("status")).lower() == "true"
                            for c in conditions
                            if c.get("type") == "Ready"
                        ),
                        None,
                    )
                    if ready is None:
                        ready = (case.get("ClusterState", {}).get("Nodes", {}).get(name_in_case, {})).get(
                            "ready"
                        )
                    if ready is False:
                        node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
                    else:
                        node.setdefault("metadata", {}).setdefault("annotations", {})[
                            "kwok.x-k8s.io/node"
                        ] = "fake"
                    snapshot_node = case.get("ClusterState", {}).get("Nodes", {}).get(name_in_case, {})
                    existing_pods = (
                        case.get("ExistingPods") or case.get("ClusterState", {}).get("ExistingPods") or {}
                    )
                    if (
                        node.get("status")
                        and snapshot_node.get("used_ports")
                        and not any(p.get("nodeName") == name_in_case for p in existing_pods.values())
                        and not fallback.get("status") == node["status"]
                    ):
                        for field in ("capacity", "allocatable"):
                            if node["status"].get(field, {}).get("pods") is not None:
                                node["status"][field]["pods"] = str(int(node["status"][field]["pods"]) + 1)
                    environment.append(node)
                environment_claims_bound = True
                if environment:
                    self._restore_environment(kubeconfig, environment)
                    environment_claims_bound = self._wait_for_environment_claims(
                        kubeconfig, environment
                    )
                marked = instrument(workload_docs, token)
                manifest = yaml_text(marked)
                try:
                    self._kubectl(kubeconfig, ["apply", "--dry-run=server", "-f", "-"], stdin=manifest)
                    self._kubectl(kubeconfig, ["apply", "-f", "-"], stdin=manifest)
                    api_accepted = True
                except KwokInfrastructureError as exc:
                    api_accepted = False
                    raise CandidateDeploymentError(str(exc)) from exc
                node_names = set((case.get("ClusterState") or {}).get("Nodes") or {})
                node_names.update(
                    d.get("metadata", {}).get("name")
                    for d in documents
                    if d.get("kind") == "Node" and d.get("metadata", {}).get("name")
                )
                node_count = len(node_names)
                expected_pods, expected_claims = expected_targets(workload_docs, node_count)
                result = self._wait(kubeconfig, token, expected_pods, expected_claims, phase)
                snapshot, observations = Collector(
                    kubeconfig, kubectl=self.kubectl, timeout=self.command_timeout, runner=self.runner
                ).collect(
                    documents, logs=False, scope="kwok_simulated_control_plane", document_locations=locations
                )
                # Finally, use the same independent collector to check all targets and referenced PVCs and save fresh snapshots.
                ok, reason = self._deployment_ready(
                    observations, len(snapshot["WaitingDeployPods"]), len(observations["pvc_status"])
                )
                if not snapshot["WaitingDeployPods"]:
                    ok, reason = False, "no_target_pods"
                if snapshot.get("_unsupported_manifest_features"):
                    ok, reason = False, "unsupported_or_unstable_workload"
                required_errors = [
                    e
                    for e in observations["collection"]["errors"]
                    if e.get("resource") in {"events", "replicasets", "jobs", "pvcs", "pvs", "storageclasses"}
                ]
                if required_errors:
                    ok, reason = False, "incomplete_collection"
                result.update(
                    status="passed" if ok else "failed",
                    reason=reason,
                    case=snapshot,
                    observations=observations,
                )
                result.update(
                    environment="kwok",
                    scope="simulated_control_plane",
                    cluster_name=name,
                    phase=phase,
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    environment_claims_bound_before_workload=environment_claims_bound,
                )
            except CandidateDeploymentError as exc:
                result = {
                    "status": "failed",
                    "scope": "simulated_control_plane",
                    "phase": phase,
                    "reason": "candidate_apply_rejected: " + str(exc),
                }
            except (KwokInfrastructureError, CollectionError, ValueError, TypeError, KeyError) as exc:
                result = {
                    "status": "error",
                    "environment": "kwok",
                    "scope": "simulated_control_plane",
                    "cluster_name": name,
                    "phase": phase,
                    "reason": str(exc),
                }
            finally:
                if attempted:
                    try:
                        self._command(
                            [
                                self.kwokctl,
                                "delete",
                                "cluster",
                                "--name",
                                name,
                                "--kubeconfig",
                                str(kubeconfig),
                                "--force",
                            ],
                            timeout=self.command_timeout,
                        )
                    except KwokInfrastructureError as exc:
                        cleanup_error = str(exc)
        result["api_accepted"] = api_accepted
        result["deployment_ready"] = result.get("status") == "passed" if api_accepted else None
        result["repair_correctness"] = "not_determined"
        if cleanup_error:
            result["validation_reason"] = result.get("reason")
            result.update(status="error", reason="kwok_cleanup_failed", cleanup_error=cleanup_error)
        return result

    def _restore_environment(self, kubeconfig: Path, documents: list[dict]) -> None:
        """Create environment objects and restore the node state read by the scheduler through the status subresource."""
        storage = [d for d in documents if d.get("kind") in {"StorageClass", "PersistentVolume"}]
        remaining = [d for d in documents if d.get("kind") not in {"StorageClass", "PersistentVolume"}]
        for batch in (storage, remaining):
            if batch:
                self._kubectl(kubeconfig, ["apply", "-f", "-"], stdin=yaml_text(batch))
        for node in (doc for doc in documents if doc.get("kind") == "Node"):
            name = (node.get("metadata") or {}).get("name")
            status = node.get("status")
            if not name or not isinstance(status, dict):
                continue
            self._kubectl(
                kubeconfig,
                [
                    "patch",
                    "node",
                    name,
                    "--type=merge",
                    "--subresource=status",
                    "-p",
                    json.dumps({"status": status}, ensure_ascii=False, separators=(",", ":")),
                ],
            )
        self._wait_for_nodes(kubeconfig, documents)

    def _wait_for_environment_claims(self, kubeconfig: Path, documents: list[dict]) -> bool:
        """Let static environment PVCs bind before creating Pods that consume them."""
        expected = {
            ((doc.get("metadata") or {}).get("namespace") or "default", (doc.get("metadata") or {}).get("name"))
            for doc in documents
            if doc.get("kind") == "PersistentVolumeClaim"
        }
        if not expected:
            return True
        deadline = self.clock() + self.validation_timeout
        while True:
            claims = self._json(self._kubectl(kubeconfig, ["get", "pvc", "-A", "-o", "json"]))
            bound = {
                ((item.get("metadata") or {}).get("namespace") or "default", (item.get("metadata") or {}).get("name"))
                for item in claims.get("items", [])
                if (item.get("status") or {}).get("phase") == "Bound"
            }
            if expected <= bound:
                return True
            if self.clock() >= deadline:
                return False
            self.sleeper(min(self.poll_interval, max(0, deadline - self.clock())))

    def _wait_for_nodes(self, kubeconfig: Path, documents: list[dict]) -> None:
        expected = {}
        for node in (doc for doc in documents if doc.get("kind") == "Node"):
            name = (node.get("metadata") or {}).get("name")
            if not name:
                continue
            conditions = (node.get("status") or {}).get("conditions") or []
            ready = next((item.get("status") for item in conditions if item.get("type") == "Ready"), None)
            expected[name] = str(ready).lower() != "false"
        if not expected:
            return
        deadline = self.clock() + self.validation_timeout
        reason = "nodes_not_observed"
        while True:
            nodes = self._json(self._kubectl(kubeconfig, ["get", "nodes", "-o", "json"]))
            current = {}
            for node in nodes["items"]:
                metadata = node.get("metadata") or {}
                spec = node.get("spec") or {}
                conditions = (node.get("status") or {}).get("conditions") or []
                ready = next(
                    (
                        str(item.get("status")).lower() == "true"
                        for item in conditions
                        if item.get("type") == "Ready"
                    ),
                    False,
                )
                transient_taint = any(
                    item.get("key") in {"node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"}
                    for item in spec.get("taints") or []
                )
                current[metadata.get("name")] = (ready, transient_taint)
            unstable = [
                name
                for name, should_be_ready in expected.items()
                if name not in current
                or (should_be_ready and (not current[name][0] or current[name][1]))
                or (not should_be_ready and current[name][0])
            ]
            if not unstable:
                return
            reason = "nodes_not_stable: " + ", ".join(sorted(unstable))
            if self.clock() >= deadline:
                raise KwokInfrastructureError(reason)
            self.sleeper(min(self.poll_interval, max(0, deadline - self.clock())))

    def _wait(
        self,
        kubeconfig: Path,
        token: str,
        expected_pods: int,
        expected_claims: int,
        phase: str,
    ) -> dict:
        if expected_pods == 0 and expected_claims == 0:
            return {"status": "error", "reason": "no_verifiable_pod_or_pvc"}
        deadline = self.clock() + self.validation_timeout
        observations = {}
        failure_since, failure_state = None, None
        while True:
            observations = self._collect(kubeconfig, token)
            ok, reason = self._deployment_ready(observations, expected_pods, expected_claims)
            if ok:
                return {"status": "passed", "reason": "targets_healthy", "observations": observations}
            # Startup failures can disappear as other workloads become ready. Collect
            # every declared target, then require a stable failure before ending early.
            complete = (
                len(observations["pod_status"]) >= expected_pods
                and len(observations["pvc_status"]) >= expected_claims
            )
            if phase == "original" and complete and reason.startswith("event:"):
                state = (
                    tuple(sorted(
                        (key, item.get("phase"), item.get("nodeName"), any(
                            c.get("type") == "Ready" and str(c.get("status")).lower() == "true"
                            for c in item.get("conditions") or []
                        ))
                        for key, item in observations["pod_status"].items()
                    )),
                    tuple(sorted((key, item.get("phase")) for key, item in observations["pvc_status"].items())),
                    tuple(sorted({
                        (key, event.get("reason") or "", event.get("message") or "")
                        for key, events in observations["pod_events"].items()
                        if observations["pod_status"].get(key, {}).get("phase") == "Pending"
                        for event in events
                    })),
                )
                now = self.clock()
                if state != failure_state:
                    failure_state, failure_since = state, now
                if now - failure_since >= self.original_failure_settle:
                    return {"status": "failed", "reason": reason, "observations": observations,
                            "failure_settled_seconds": now - failure_since}
            else:
                failure_since, failure_state = None, None
            if self.clock() >= deadline:
                return {"status": "failed", "reason": reason, "observations": observations}
            self.sleeper(min(self.poll_interval, max(0, deadline - self.clock())))

    def _collect(self, kubeconfig: Path, token: str) -> dict:
        selector = f"{MARKER}={token}"
        pods = self._json(self._kubectl(kubeconfig, ["get", "pods", "-A", "-l", selector, "-o", "json"]))
        claims = self._json(self._kubectl(kubeconfig, ["get", "pvc", "-A", "-l", selector, "-o", "json"]))
        events = self._json(self._kubectl(kubeconfig, ["get", "events", "-A", "-o", "json"]))
        pod_status, pod_events, assignments = {}, {}, {}
        target_names = set()
        for item in pods.get("items", []):
            metadata = item.get("metadata") or {}
            status = item.get("status") or {}
            spec = item.get("spec") or {}
            key = f"{metadata.get('namespace', 'default')}/{metadata.get('name', '')}"
            target_names.add(metadata.get("name"))
            pod_status[key] = {
                "namespace": metadata.get("namespace", "default"),
                "phase": status.get("phase", "Unknown"),
                "conditions": copy.deepcopy(status.get("conditions") or []),
                "containerStatuses": copy.deepcopy(status.get("containerStatuses") or []),
                "nodeName": spec.get("nodeName"),
            }
            assignments[key] = spec.get("nodeName")
            pod_events[key] = []
        for event in events.get("items", []):
            involved = event.get("involvedObject") or event.get("regarding") or {}
            if involved.get("kind") != "Pod" or involved.get("name") not in target_names:
                continue
            key = f"{involved.get('namespace', 'default')}/{involved.get('name', '')}"
            pod_events.setdefault(key, []).append(
                {
                    "type": event.get("type"),
                    "reason": event.get("reason"),
                    "message": event.get("message") or event.get("note"),
                    "count": event.get("count", 1),
                }
            )
        pvc_status = {}
        for item in claims.get("items", []):
            metadata = item.get("metadata") or {}
            status = item.get("status") or {}
            spec = item.get("spec") or {}
            key = f"{metadata.get('namespace', 'default')}/{metadata.get('name', '')}"
            pvc_status[key] = {"phase": status.get("phase", "Unknown"), "volumeName": spec.get("volumeName")}
        return {
            "pod_status": pod_status,
            "pod_events": pod_events,
            "pvc_status": pvc_status,
            "assignments": assignments,
            "is_pending": any(x["phase"] == "Pending" for x in pod_status.values()),
        }

    @staticmethod
    def _deployment_ready(observations: dict, expected_pods: int, expected_claims: int) -> tuple[bool, str]:
        statuses = observations["pod_status"]
        claims = observations["pvc_status"]
        failure_reasons = {"FailedScheduling", "FailedMount", "FailedAttachVolume", "FailedBinding"}
        for key, events in observations["pod_events"].items():
            current = statuses.get(key, {})
            scheduled_and_ready = (
                bool(current.get("nodeName"))
                and current.get("phase") == "Running"
                and any(
                    item.get("type") == "Ready" and str(item.get("status")).lower() == "true"
                    for item in current.get("conditions") or []
                )
            )
            for event in events:
                # Once the same Pod is scheduled and ready, a startup scheduling failure no longer indicates a current blocker.
                # Handle other mount/binding failures conservatively; still check all PVCs and target counts afterward.
                if event.get("reason") == "FailedScheduling" and scheduled_and_ready:
                    continue
                if event.get("reason") in failure_reasons:
                    return False, f"event:{event['reason']}: {event.get('message') or ''}".strip()
        if len(statuses) < expected_pods:
            return False, f"expected_{expected_pods}_pods_found_{len(statuses)}"
        if len(claims) < expected_claims:
            return False, f"expected_{expected_claims}_pvcs_found_{len(claims)}"
        if any(item.get("phase") != "Bound" for item in claims.values()):
            return False, "pvc_not_bound"
        for key, status in statuses.items():
            if not status.get("nodeName"):
                return False, f"pod_not_scheduled:{key}"
            if status.get("phase") == "Succeeded":
                continue
            ready = any(
                item.get("type") == "Ready" and str(item.get("status")).lower() == "true"
                for item in status.get("conditions") or []
            )
            if status.get("phase") != "Running" or not ready:
                return False, f"pod_not_ready:{key}:{status.get('phase')}"
        return True, "targets_healthy"

    def _kubectl(self, kubeconfig: Path, arguments: list[str], *, stdin: str | None = None) -> str:
        return self._command(
            [self.kubectl, "--kubeconfig", str(kubeconfig), *arguments],
            stdin=stdin,
            timeout=self.command_timeout,
        )

    def _command(self, arguments: list[str], *, stdin: str | None = None, timeout: int) -> str:
        try:
            completed = self.runner(
                arguments,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise KwokInfrastructureError(f"command_timeout: {arguments[0]}") from None
        except OSError as exc:
            raise KwokInfrastructureError(f"command_unavailable: {arguments[0]}: {exc}") from None
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "unknown command error").strip()[:4000]
            raise KwokInfrastructureError(f"command_failed: {' '.join(arguments[:3])}: {message}")
        return completed.stdout or ""

    @staticmethod
    def _json(text: str) -> dict:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise KwokInfrastructureError("kubectl returned invalid JSON") from None
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise KwokInfrastructureError("kubectl JSON missing items")
        return value
