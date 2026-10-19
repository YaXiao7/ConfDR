"""Storage reference, binding and provisioning configuration facts."""

import re
from functools import lru_cache
from pathlib import Path

ACCESS_MODES = {"ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod"}
CONTRACTS_FILE = Path(__file__).resolve().parents[3] / "config" / "provisioner_contracts.yaml"


def lookup_claim(inventory, namespace, name):
    if namespace in inventory:
        return inventory[namespace].get(name)
    qualified = f"{namespace}/{name}"
    if qualified in inventory:
        return inventory[qualified]
    candidate = inventory.get(name)
    if not isinstance(candidate, dict):
        return None
    fields = {
        "metadata",
        "namespace",
        "storageClassName",
        "accessModes",
        "accessMode",
        "volumeMode",
        "requestsGi",
        "request",
        "resources",
        "volumeName",
        "boundPV",
        "STATUS",
        "selector",
        "uid",
    }
    if candidate and not fields.intersection(candidate):
        return None  # Do not mistake a namespace dictionary for a PVC with the same name.
    actual_namespace = (
        candidate.get("namespace") or candidate.get("metadata", {}).get("namespace") or "default"
    )
    return candidate if actual_namespace == namespace else None


def effective_class(claim, classes):
    value = claim.get("storageClassName")
    if value is not None:
        return value
    if classes is None:
        return None
    defaults = []
    for name, sc in classes.items():
        meta = sc.get("metadata", {})
        annotations = meta.get("annotations", sc.get("annotations", {}))
        if str(annotations.get("storageclass.kubernetes.io/is-default-class", "")).lower() == "true":
            defaults.append((meta.get("creationTimestamp"), name))
    if not defaults:
        return ""
    if len(defaults) == 1:
        return defaults[0][1]
    if any(stamp is None for stamp, _ in defaults):
        return None
    defaults.sort(reverse=True)
    return defaults[0][1] if defaults[0][0] != defaults[1][0] else None


@lru_cache(maxsize=1)
def _registry_contracts():
    """Versioned driver contracts shipped with ConfDR; a missing file means no contracts."""
    import yaml

    if not CONTRACTS_FILE.is_file():
        return {}
    data = yaml.safe_load(CONTRACTS_FILE.read_text()) or {}
    contracts = data.get("contracts") or {}
    if not isinstance(contracts, dict):
        raise ValueError("provisioner_contracts.yaml must map provisioners to contracts")
    return contracts


def provisioner_contracts(case):
    """Merge the shipped registry with case-level contracts; case entries replace registry entries."""
    case_contracts = case.get("ProvisionerContracts", {})
    if not isinstance(case_contracts, dict):
        raise ValueError("ProvisionerContracts must map provisioners to contracts")
    return {**_registry_contracts(), **case_contracts}


def access_modes_supported(claim, sc, contracts):
    """Three-valued capability check: False if a requested mode is documented as rejected,
    True if all requested modes are documented as supported, otherwise None."""
    contract = contracts.get(sc.get("provisioner"))
    if contract is None or "access_modes" not in contract:
        return None
    modes = contract["access_modes"]
    if not isinstance(modes, dict):
        raise ValueError("Provisioner access_modes must map volume modes to mode lists")
    requested = claim.get("accessModes", claim.get("accessMode"))
    if not isinstance(requested, list) or not requested:
        return None
    entry = modes.get(claim.get("volumeMode") or "Filesystem")
    if entry is None:
        return None
    supported, unsupported = (set(entry.get(key) or []) for key in ("supported", "unsupported"))
    if not supported | unsupported <= ACCESS_MODES or supported & unsupported:
        raise ValueError("Provisioner access mode lists must be disjoint known access modes")
    if any(mode in unsupported for mode in requested):
        return False
    return True if set(requested) <= supported else None


def parameters_valid(sc, contracts):
    params = sc.get("parameters", {})
    if not isinstance(params, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in params.items()
    ):
        return False
    contract = contracts.get(sc.get("provisioner"))
    if contract is None:
        return None
    if not isinstance(contract, dict):
        raise ValueError("Provisioner contract must be an object")
    has_allowed, has_required = "allowed_parameters" in contract, "required_parameters" in contract
    if not has_allowed and not has_required:
        return None  # A capability-only contract does not validate parameters.
    if not has_allowed or not has_required:
        raise ValueError("Provisioner contract requires allowed_parameters and required_parameters")
    allowed, required = contract["allowed_parameters"], contract["required_parameters"]
    if (
        not isinstance(allowed, list)
        or not isinstance(required, list)
        or any(not isinstance(x, str) for x in [*allowed, *required])
    ):
        raise ValueError("Provisioner contract parameter names must be lists")
    choices = contract.get("parameter_values", {})
    if not isinstance(choices, dict) or any(
        not isinstance(key, str)
        or not isinstance(values, list)
        or any(not isinstance(x, str) for x in values)
        for key, values in choices.items()
    ):
        raise ValueError("Provisioner parameter_values must map names to lists of strings")
    if not set(required) <= set(allowed) or not set(choices) <= set(allowed):
        raise ValueError("Provisioner contract references parameters outside allowed_parameters")
    if not set(params) <= set(allowed) or not set(required) <= set(params):
        return False
    return all(key not in params or params[key] in values for key, values in choices.items())


def populate_storage(world, case, claims, *, profile=None):
    from confdr.shared.kubernetes import node_terms, selector_matches

    cluster = case["ClusterState"]
    classes, volumes = cluster.get("StorageClasses"), cluster.get("PVs")
    nodes = cluster["Nodes"]
    values = world.values
    contracts = provisioner_contracts(case)
    for c, raw_claim in claims.items():
        if raw_claim is None:
            values["claim_exists", c] = False if "PVCs" in cluster else None
        claim = raw_claim or {}
        fixed = claim.get("boundPV") or claim.get("volumeName")
        values["fixed_volume_exists", c] = not fixed or (None if volumes is None else fixed in volumes)
        class_name = effective_class(claim, classes)
        sc = (classes or {}).get(class_name, {})
        class_exists = (
            None if classes is None or class_name is None else bool(class_name and class_name in classes)
        )
        provisioner = sc.get("provisioner")
        configured = (
            None
            if class_exists is None
            else bool(
                class_exists
                and isinstance(provisioner, str)
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./_-]*", provisioner)
                and provisioner != "kubernetes.io/no-provisioner"
            )
        )
        explicit_classless = claim.get("storageClassName") == ""
        missing_default_without_pvs = (
            profile == "scheduling-v2"
            and claim.get("storageClassName") is None
            and class_name == ""
            and volumes is not None
            and not volumes
        )
        matching_static_class_without_sc = (
            profile == "scheduling-v2"
            and class_exists is False
            and bool(class_name)
            and volumes is not None
            and any((volume.get("storageClassName") or "") == class_name for volume in volumes.values())
        )
        if fixed or claim.get("selector") is not None or explicit_classless:
            eligible = False
        elif class_name is None:
            eligible = None
        elif provisioner == "kubernetes.io/no-provisioner":
            eligible = False
        elif matching_static_class_without_sc:
            eligible = False
        elif class_name == "":
            eligible = (
                None
                if profile == "scheduling-v2" and volumes is None
                else missing_default_without_pvs
            )
        else:
            eligible = True
        params_ok = parameters_valid(sc, contracts) if configured else configured
        values["storage_class_exists", c] = class_exists
        values["provisioner_configured", c] = configured
        values["provisioner_parameters_valid", c] = params_ok
        # Read only by catalogs that declare the predicate in taxonomy.predicates.
        values["access_modes_supported", c] = (
            access_modes_supported(claim, sc, contracts) if configured else configured
        )
        values["dynamic_eligible", c] = eligible
        values["dynamic_available", c] = (
            False
            if False in (eligible, configured, params_ok)
            else None
            if None in (eligible, configured, params_ok)
            else True
        )
        for v, volume in (volumes or {}).items():
            values["class_ok", c, v] = (
                None if class_name is None else class_name == (volume.get("storageClassName") or "")
            )
            ref = volume.get("claimRef") or {}
            same_ref = f"{ref.get('namespace', 'default')}/{ref.get('name')}" == c
            uid = claim.get("uid") or claim.get("metadata", {}).get("uid")
            if same_ref and ref.get("uid"):
                same_ref = None if uid is None else uid == ref["uid"]
            phase = volume.get("STATUS", volume.get("phase"))
            deleting = volume.get("metadata", {}).get("deletionTimestamp") or volume.get("deletionTimestamp")
            available = (
                False
                if deleting or ref and same_ref is False or phase in {"Released", "Failed"}
                else None
                if phase is None or ref and same_ref is None
                else bool(phase == "Available" or phase == "Bound" and same_ref)
            )
            values["volume_available", c, v] = available
            selector = claim.get("selector")
            labels = volume.get("labels", volume.get("metadata", {}).get("labels"))
            values["pv_selector_ok", c, v] = (
                True
                if not selector or fixed == v or same_ref is True
                else None
                if labels is None
                else selector_matches(selector, labels)
            )
        for n, node in nodes.items():
            topology = sc.get("allowedTopologies") or []
            if not eligible or configured is False:
                allowed = True  # S2/S3 constrain unavailable paths to avoid duplicate attribution.
            elif class_exists is None:
                allowed = None
            elif topology and sc.get("volumeBindingMode", "Immediate") == "Immediate":
                allowed = None  # Allowed topologies do not determine the final placement of an unprovisioned volume.
            elif topology and "labels" not in node:
                allowed = None
            else:
                allowed = not topology or any(
                    all(
                        node.get("labels", {}).get(e.get("key")) in e.get("values", [])
                        for e in term.get("matchLabelExpressions", [])
                    )
                    for term in topology
                )
            selected = (
                claim.get("metadata", {}).get("annotations", {}).get("volume.kubernetes.io/selected-node")
            )
            if selected and selected != n:
                allowed = False
            values["dynamic_node_ok", c, n] = allowed
    for v, volume in (volumes or {}).items():
        required = (volume.get("nodeAffinity") or {}).get("required")
        for n, node in nodes.items():
            world.values["volume_node_ok", v, n] = (
                True
                if required is None
                else None
                if "labels" not in node
                else node_terms(required.get("nodeSelectorTerms", []), n, node["labels"])
            )
