"""CSI volume-count facts for the Kubernetes 1.30 scheduling profile.

Counts are symbolic integers over placement, scheduling order and storage choices.
The generated category, not this interpreter, compares counts with driver limits.
"""

from collections import defaultdict

import z3

from confdr.shared.logic import Entity
from confdr.shared.storage import effective_class, lookup_claim

UNKNOWN_DRIVER = "<unresolved-volume-driver>"
NON_CSI = {"nfs", "hostPath", "local", "emptyDir", "secret", "configMap", "projected", "downwardAPI"}
MIGRATED = {
    "awsElasticBlockStore",
    "azureDisk",
    "gcePersistentDisk",
    "vsphereVolume",
    "cinder",
    "portworxVolume",
}


def populate_volume_limits(world, case):
    cluster = case["ClusterState"]
    pods, nodes = case["WaitingDeployPods"], cluster["Nodes"]
    pvs, pvcs, classes = (cluster.get(k, {}) for k in ("PVs", "PVCs", "StorageClasses"))
    csi_nodes = cluster.get("CSINodes")
    csi_overrides = cluster.get("CSINodeOverrides", {})
    if any(e.get("resource") == "csinodes" for e in case.get("collection", {}).get("errors", [])):
        csi_overrides = {}
    observed_nodes = csi_nodes if isinstance(csi_nodes, dict) else csi_overrides
    drivers = set()
    if isinstance(observed_nodes, dict):
        for item in observed_nodes.values():
            for driver in item.get("spec", {}).get("drivers", []):
                if isinstance(driver.get("name"), str):
                    drivers.add(driver["name"])

    def pv_identity(pv):
        csi = pv.get("csi")
        if isinstance(csi, dict) and csi.get("driver") and csi.get("volumeHandle"):
            return csi["driver"], "pv:" + csi["volumeHandle"]
        if any(k in pv for k in NON_CSI):
            return None
        # In-tree migration and incomplete source records need explicit support/evidence.
        raise ValueError("unresolved PV driver/volumeHandle (CSI migration is outside this profile)")

    def claim_paths(namespace, name, target):
        claim = lookup_claim(pvcs, namespace, name)
        if claim is None:
            raise ValueError(f"PVC inventory/reference missing: {namespace}/{name}")
        fixed = claim.get("boundPV") or claim.get("volumeName")
        if fixed:
            if fixed not in pvs:
                raise ValueError(f"PV inventory/reference missing: {fixed}")
            identity = pv_identity(pvs[fixed])
            return [(identity, z3.BoolVal(True))] if identity else []
        key = f"{namespace}/{name}"
        paths = []
        if target:
            if "PVs" not in cluster:
                raise ValueError("PVs inventory missing")
            # The storage rules select and validate the binding; count its actual chosen identity.
            for volume, pv in pvs.items():
                if world.values.get(("class_ok", key, volume)) is False:
                    continue
                identity = pv_identity(pv)
                if identity:
                    paths.append(
                        (identity, world.call("bind", [Entity("Claim", key), Entity("Volume", volume)]))
                    )
        class_name = effective_class(claim, cluster.get("StorageClasses"))
        if class_name is None:
            raise ValueError(f"StorageClass resolution unknown: {key}")
        if not class_name:
            return paths
        sc = classes.get(class_name)
        if not sc:
            # A complete inventory proves there is no dynamic CSI path for this
            # class. Keep any known static CSI-PV alternatives, but do not add
            # an unknown driver that makes an otherwise static repair undecidable.
            # A failed/missing class inventory is still genuinely unknown.
            if "StorageClasses" in cluster and not any(
                e.get("resource") == "storageclasses"
                for e in case.get("collection", {}).get("errors", [])
            ):
                return paths
            raise ValueError(f"StorageClass inventory/reference missing: {class_name}")
        driver = sc.get("provisioner")
        if driver == "kubernetes.io/no-provisioner":
            return paths
        if not driver or driver.startswith("kubernetes.io/"):
            raise ValueError(f"CSI provisioner unresolved: {class_name}")
        decision = world.call("dynamic", [Entity("Claim", key)]) if target else z3.BoolVal(True)
        paths.append(((driver, "pvc:" + key), decision))
        return paths

    def volumes(pod, target):
        result, errors = defaultdict(list), []
        if "volumes" not in pod:
            return {}, ["Pod volumes were not collected"]
        namespace = pod.get("namespace") or "default"
        for volume in pod.get("volumes") or []:
            try:
                pvc = volume.get("persistentVolumeClaim")
                if pvc:
                    paths = claim_paths(namespace, pvc["claimName"], target)
                elif "ephemeral" in volume or any(k in volume for k in MIGRATED):
                    raise ValueError("generic ephemeral or migrated inline volume is not modeled")
                elif "csi" in volume:
                    # Inline CSI ephemeral volumes are not PVC-backed persistent volumes.
                    paths = []
                elif any(k in volume for k in NON_CSI):
                    paths = []
                else:
                    raise ValueError("unrecognized volume source")
                for identity, active in paths:
                    drivers.add(identity[0])
                    result[identity].append(active)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
        return {identity: z3.Or(active) for identity, active in result.items()}, errors

    target_paths, target_errors = {}, {}
    for p, pod in pods.items():
        target_paths[p], target_errors[p] = volumes(pod, True)
    background = defaultdict(set)
    background_errors = defaultdict(list)
    existing = case.get("ExistingPods", cluster.get("ExistingPods"))
    if existing is not None:
        for pod in existing.values():
            node = pod.get("nodeName")
            if node not in nodes or pod.get("phase") in {"Succeeded", "Failed"}:
                continue
            paths, errors = volumes(pod, False)
            background[node].update(paths)
            background_errors[node].extend(errors)
    if any(target_errors.values()):
        drivers.add(UNKNOWN_DRIVER)
    world.domains["Driver"] = sorted(drivers)

    def limit(n, d):
        if not isinstance(csi_nodes, dict) and n not in csi_overrides:
            return None, None
        entries = [
            x for x in observed_nodes.get(n, {}).get("spec", {}).get("drivers", []) if x.get("name") == d
        ]
        if len(entries) > 1:
            return None, None
        count = (entries[0].get("allocatable") or {}).get("count") if entries else None
        if count is None:
            # A complete API inventory without a reported limit differs from failed collection.
            # Legacy Node attach limits require migration handling; never silently ignore them.
            if any(k.startswith("attachable-volumes-") for k in nodes[n].get("allocatable", {})):
                return None, None
            return False, 0
        if type(count) is not int or count < 0:
            return None, None
        return True, count

    for n in nodes:
        for d in drivers:
            applies, maximum = limit(n, d) if d != UNKNOWN_DRIVER else (None, None)
            world.values["volume_limit_applies", n, d] = applies
            world.values["volume_limit", n, d] = maximum
            for p in pods:
                own = {i: active for i, active in target_paths[p].items() if i[0] == d}
                prefix = (p, n, d)
                if not own and not target_errors[p] or applies is False and not target_errors[p]:
                    world.values[("volume_count_before", *prefix)] = 0
                    world.values[("volume_count_new", *prefix)] = 0
                    continue
                if (
                    target_errors[p]
                    or existing is None
                    or background_errors[n]
                    or any(target_errors.values())
                ):
                    world.values[("volume_count_before", *prefix)] = None
                    world.values[("volume_count_new", *prefix)] = None
                    continue
                identities = {i for i in background[n] if i[0] == d} | set(own)
                identities.update(i for paths in target_paths.values() for i in paths if i[0] == d)
                before, new = [], []
                for identity in sorted(identities):
                    witnesses = [z3.BoolVal(identity in background[n])]
                    for q in pods:
                        if q == p or identity not in target_paths[q]:
                            continue
                        witnesses.append(
                            z3.And(
                                world.call("assign", [Entity("Pod", q), Entity("Node", n)]),
                                world.call("schedule_rank", [Entity("Pod", q)])
                                < world.call("schedule_rank", [Entity("Pod", p)]),
                                target_paths[q][identity],
                            )
                        )
                    present = z3.Or(witnesses)
                    before.append(z3.If(present, 1, 0))
                    new.append(z3.If(z3.And(own.get(identity, z3.BoolVal(False)), z3.Not(present)), 1, 0))
                world.values[("volume_count_before", *prefix)] = z3.Sum(before) if before else 0
                world.values[("volume_count_new", *prefix)] = z3.Sum(new) if new else 0
