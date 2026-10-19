"""Case directory protocol: manifest ownership, snapshot loading and complete outputs; exclude ground truth from diagnosis."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from confdr.shared.io import manifests, read, write, yaml_text

WORKLOADS = {"Pod", "Deployment", "StatefulSet", "ReplicaSet", "Job", "DaemonSet", "CronJob"}
GLOBAL = {
    "Node",
    "Namespace",
    "PersistentVolume",
    "StorageClass",
    "ClusterRole",
    "ClusterRoleBinding",
    "CSINode",
}


def object_key(doc: dict) -> tuple[str, str, str]:
    meta = doc.get("metadata") or {}
    return (
        doc.get("kind", ""),
        "" if doc.get("kind") in GLOBAL else meta.get("namespace") or "default",
        meta.get("name", ""),
    )


APP_DEPENDENCIES = {
    "PersistentVolumeClaim",
    "ConfigMap",
    "Secret",
    "Service",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "Ingress",
    "NetworkPolicy",
}


def manifest_scope(doc: dict, locations: dict | None = None) -> str:
    """Ownership comes from apps/envs, with cluster infrastructure always environment-owned."""
    if doc.get("kind") in GLOBAL:
        return "environment"
    # Normalise separators: callers pass either a relative path from the filesystem or an
    # ownership location, and only the POSIX form is meaningful for an apps/envs prefix test.
    path = (locations or {}).get(object_key(doc), "").replace("\\", "/")
    if path.startswith("envs/"):
        return "environment"
    if path.startswith("apps/"):
        return "application"
    return "application" if doc.get("kind") in WORKLOADS | APP_DEPENDENCIES else "environment"


def load_documents(folder: str | Path) -> tuple[list[dict], dict[tuple, str]]:
    folder = Path(folder)
    paths = sorted(
        (
            p
            for part in ("envs", "apps")
            for p in (folder / part).rglob("*")
            if p.suffix in {".yaml", ".yml"} and not any(x.startswith(".") for x in p.relative_to(folder).parts)
        ),
        key=lambda p: p.as_posix(),
    )
    if not paths and (folder / "manifest.yaml").is_file():
        paths = [folder / "manifest.yaml"]
    docs, locations = [], {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError(f"Manifest path is outside the case directory: {path}")
        for doc in manifests(path):
            key = object_key(doc)
            if not key[0] or not key[2] or key in locations:
                raise ValueError(f"Resource identity is missing or duplicated: {key}")
            # Store POSIX-relative paths so ownership scope does not depend on the host separator.
            locations[key] = path.relative_to(folder).as_posix()
            docs.append(doc)
    return docs, locations


def associate_targets(case: dict, documents: list[dict]) -> dict:
    """Associate legacy app-0 names with controllers to avoid stale replicas after candidate reconstruction."""
    case = copy.deepcopy(case)
    waiting = case.get("WaitingDeployPods", {})
    for name, pod in waiting.items():
        if pod.get("workload"):
            continue
        matches = []
        ns = pod.get("namespace") or "default"
        for doc in documents:
            kind, namespace, resource = object_key(doc)
            if kind not in WORKLOADS or namespace != ns:
                continue
            canonical = name.startswith(f"{ns}/{kind}/{resource}/")
            old_name = name in {resource, f"{ns}/{resource}"} or (
                name.startswith(resource + "-") and name[len(resource) + 1 :].isdigit()
            )
            if canonical or old_name or (kind == "CronJob" and name == resource + "-cronpod"):
                matches.append({"kind": kind, "name": resource})
        if len(matches) == 1:
            pod["workload"] = matches[0]
    return case


def ownership_locations(case: dict) -> dict:
    """Serializable ownership survives collection and independent deployment evaluation."""
    return {
        tuple(item["identity"]): ("apps/" if item["scope"] == "application" else "envs/") + "snapshot.yaml"
        for item in case.get("_manifest_ownership", [])
    }


def prepare_kwok_probe_case(
    case: dict,
    documents: list[dict],
    locations: dict[tuple, str] | None = None,
) -> tuple[dict, list[str]]:
    """Prepare a case for KWOK without materializing the same background Pod twice.

    ``ClusterState.ExistingPods`` represents the observed background snapshot used by
    ConfDR's formal model.  KWOK may materialize those snapshot entries as synthetic
    ``confdr-existing-*`` Pods.  If an ExistingPod is already represented by a Pod
    manifest under ``envs/``, materializing it again would make the scheduler count
    the same logical background Pod twice.

    This helper is intentionally KWOK-only: normal diagnosis keeps the complete
    ExistingPods snapshot unchanged.  Only exact Pod identities that are already
    environment-owned manifests are removed; unrelated snapshot-only background Pods
    remain available for synthetic materialization.
    """
    prepared = copy.deepcopy(case)
    environment_pods = {
        (object_key(doc)[1], object_key(doc)[2])
        for doc in documents
        if doc.get("kind") == "Pod" and manifest_scope(doc, locations) == "environment"
    }
    if not environment_pods:
        return prepared, []

    removed: list[str] = []

    def prune(existing) -> None:
        if not isinstance(existing, dict):
            return
        for raw_key, pod in list(existing.items()):
            pod = pod if isinstance(pod, dict) else {}
            namespace = pod.get("namespace") or "default"
            name = raw_key
            if isinstance(raw_key, str) and "/" in raw_key:
                key_namespace, name = raw_key.split("/", 1)
                namespace = pod.get("namespace") or key_namespace or "default"
            if (namespace, name) in environment_pods:
                del existing[raw_key]
                removed.append(f"{namespace}/{name}")

    cluster = prepared.get("ClusterState")
    if isinstance(cluster, dict):
        prune(cluster.get("ExistingPods"))
    # Keep compatibility with legacy cases that stored ExistingPods at the top level.
    prune(prepared.get("ExistingPods"))

    return prepared, sorted(set(removed))


def load_case(
    folder: str | Path, *, validate_targets: bool = True, include_observations: bool = True
) -> tuple[dict, list[dict], dict, dict]:
    folder = Path(folder)
    docs, locations = load_documents(folder)
    raw = read(folder / "case.json") if (folder / "case.json").is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError("case.json must be an object")
    case = {
        k: raw[k]
        for k in (
            "WaitingDeployPods",
            "ClusterState",
            "ExistingPods",
            "ExtraFacts",
            "ProvisionerContracts",
            "gpu_resource_names",
            "_unsupported_manifest_features",
            "_unmodelled_admission",
            "collection",
        )
        if k in raw
    }
    app_docs = [d for d in docs if manifest_scope(d, locations) == "application"]
    case = associate_targets(case, app_docs)
    environment_workloads = {
        object_key(d)
        for d in docs
        if d.get("kind") in WORKLOADS and manifest_scope(d, locations) == "environment"
    }
    case["_manifest_ownership"] = [
        {"identity": list(object_key(d)), "scope": manifest_scope(d, locations)} for d in docs
    ]
    for name, target in (case.get("WaitingDeployPods", {}) if validate_targets else {}).items():
        owner = target.get("workload") or {}
        ns = target.get("namespace") or "default"
        if (owner.get("kind"), ns, owner.get("name")) in environment_workloads or any(
            namespace == ns
            and (name in {resource, f"{ns}/{resource}"} or name.startswith(f"{ns}/{kind}/{resource}/"))
            for kind, namespace, resource in environment_workloads
        ):
            raise ValueError(
                "case.json includes an envs workload as a deployment target; recollect the apps/envs dataset"
            )
    case["_input_yaml"] = yaml_text(docs)
    if not include_observations:
        return case, docs, {}, locations
    obs_path = next(
        (folder / n for n in ("pod_info.json", "observations.json") if (folder / n).is_file()), None
    )
    raw_obs = read(obs_path) if obs_path else {}
    if not isinstance(raw_obs, dict):
        raise ValueError("pod_info.json must be an object")
    obs = {
        k: raw_obs[k]
        for k in (
            "pod_status",
            "pod_events",
            "container_logs",
            "raw_logs",
            "observed_at",
            "candidate_hash",
            "collection",
            "pvc_status",
        )
        if k in raw_obs
    }
    for name in ("log.txt", "logs.txt"):
        if (folder / name).is_file():
            obs.setdefault("raw_logs", {})[name] = (folder / name).read_text(encoding="utf-8")
    return case, docs, obs, locations


def discover(source: str | Path, *, include_fixed: bool = False) -> list[Path]:
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError("Dataset directory does not exist")
    folders = {
        p.parent
        for p in source.rglob("case.json")
        if not any(x.startswith(".") for x in p.relative_to(source).parts)
    }
    folders |= {
        p.parent
        for p in source.rglob("apps")
        if p.is_dir() and not any(x.startswith(".") for x in p.relative_to(source).parts)
    }
    if (source / "manifest.yaml").is_file():
        folders.add(source)
    ordered = sorted(
        p
        for p in folders
        if include_fixed or not any(part.endswith("_fixed") for part in p.relative_to(source).parts)
    )
    if not ordered:
        raise ValueError("Dataset contains no cases with case.json or apps/")
    if any(p.is_symlink() or not p.resolve().is_relative_to(source) for p in ordered):
        raise ValueError("Case path is outside the dataset")
    if any(a in b.parents for a in ordered for b in ordered if a != b):
        raise ValueError("Case directories must not be nested")
    return ordered


def copy_case(source: Path, target: Path) -> None:
    """Copy only inputs and reference labels, excluding old experiment results; retain a complete official copy even for failed cases."""
    target.mkdir(parents=True, exist_ok=False)
    for part in ("apps", "envs"):
        src = source / part
        if src.is_symlink():
            raise ValueError(f"Symlinks inside cases are not supported: {src}")
        if src.is_dir():
            for p in src.rglob("*"):
                if p.is_symlink():
                    raise ValueError(f"Symlinks inside cases are not supported: {p}")
            shutil.copytree(
                src, target / part, ignore=shutil.ignore_patterns("._*", "__pycache__", ".DS_Store")
            )
    for name in (
        "case.json",
        "pod_info.json",
        "observations.json",
        "manifest.yaml",
        "truth.json",
        "truth_v2.json",
        "log.txt",
        "logs.txt",
    ):
        p = source / name
        if p.is_file():
            if p.is_symlink():
                raise ValueError(f"Symlinks inside cases are not supported: {p}")
            shutil.copy2(p, target / name)


def save_candidate(
    folder: Path, documents: list[dict], locations: dict[tuple, str], case: dict, observations: dict
) -> None:
    groups: dict[str, list] = {name: [] for name in set(locations.values())}
    for doc in documents:
        key = object_key(doc)
        path = locations.get(key)
        if path is None:
            # Store new dependencies in a separate file without overwriting inputs or constructing paths from user resource names.
            from confdr.shared.io import digest

            path = (
                ("apps/" if manifest_scope(doc) == "application" else "envs/")
                + "added-"
                + digest(key)[:16]
                + ".yaml"
            )
            if path in locations.values():
                raise ValueError("New resource output file conflicts with an existing manifest")
        groups.setdefault(path, []).append(doc)
    for name, docs in groups.items():
        if not docs:
            raise ValueError("Original manifest objects must not be lost")
        write(folder / name, yaml_text(docs))
    clean_case = {k: v for k, v in case.items() if k != "_input_yaml"}
    write(folder / "case.json", clean_case)
    write(folder / "pod_info.json", observations)
    # Exclude original runtime logs from successful copies; retain original observations in each case result.
    for name in ("observations.json", "log.txt", "logs.txt"):
        (folder / name).unlink(missing_ok=True)
