"""Read-only explanations of storage facts already used by the formal model.

No truth labels, deployment observations, or LLM judgments participate here.
Local path compatibility is not a new certificate of whole-cluster feasibility.
"""

from confdr.runtime.attribution import is_requirement
from confdr.shared.storage import effective_class, lookup_claim

STATIC_CHECKS = (
    "class_ok", "access_ok", "mode_ok", "capacity_ok", "volume_available", "pv_selector_ok",
)
DYNAMIC_CHECKS = ("dynamic_eligible", "storage_class_exists", "provisioner_configured",
                  "provisioner_parameters_valid")


def _checks(values, names, *entities):
    facts = {name: values.get((name, *entities)) for name in names}
    return {
        "status": "blocked" if False in facts.values() else (
            "unknown" if None in facts.values() else "compatible"
        ),
        "facts": facts,
        "failed_checks": [name for name, value in facts.items() if value is False],
        "unknown_checks": [name for name, value in facts.items() if value is None],
        "fact_refs": ["/".join((name, *entities)) for name in names],
    }


def storage_diagnostics(case, world, core):
    cluster = case.get("ClusterState", {})
    classes = cluster.get("StorageClasses")
    result = []
    for claim_id in world.domains.get("Claim", []):
        namespace, name = claim_id.split("/", 1)
        claim = lookup_claim(cluster.get("PVCs", {}), namespace, name)
        if claim is None:
            result.append({"claim": claim_id, "scope": "storage_path_explanation",
                           "status": "claim_reference_unresolved", "category_scope": "S1"})
            continue
        class_name = effective_class(claim, classes)
        sc = (classes or {}).get(class_name, {})
        default_provisioning_gap = (
            claim.get("storageClassName") is None
            and class_name == ""
            and cluster.get("PVs") == {}
            and world.values.get(("dynamic_eligible", claim_id)) is True
            and world.values.get(("storage_class_exists", claim_id)) is False
        )
        fixed = claim.get("boundPV") or claim.get("volumeName")
        static_only = []
        if fixed:
            static_only.append("fixed_volume_reference")
        if claim.get("selector") is not None:
            static_only.append("pvc_selector")
        if claim.get("storageClassName") == "":
            static_only.append("explicit_empty_storage_class")
        if sc.get("provisioner") == "kubernetes.io/no-provisioner":
            static_only.append("no_provisioner_storage_class")

        candidates = []
        for volume in world.domains.get("Volume", []):
            checks = (*STATIC_CHECKS, "fixed_to") if fixed else STATIC_CHECKS
            candidates.append({"volume": volume, **_checks(world.values, checks, claim_id, volume)})
        static_status = (
            "inventory_unknown" if "PVs" not in cluster else
            "no_candidate" if not candidates else
            "compatible" if any(v["status"] == "compatible" for v in candidates) else
            "unknown" if any(v["status"] == "unknown" for v in candidates) else "blocked"
        )
        from confdr.shared.logic import SIGNATURES

        dynamic_checks = DYNAMIC_CHECKS + (
            ("access_modes_supported",) if "access_modes_supported" in SIGNATURES else ()
        )
        dynamic = _checks(world.values, dynamic_checks, claim_id)
        if world.values.get(("dynamic_eligible", claim_id)) is False:
            dynamic["status"] = "ineligible"
        dynamic["controller_and_backend_health"] = "not_established_by_configuration_checks"
        dynamic["provisioner"] = sc.get("provisioner")
        dynamic["required_evidence"] = []
        if (world.values.get(("dynamic_eligible", claim_id)) is not False
                and world.values.get(("provisioner_parameters_valid", claim_id)) is None
                and world.values.get(("provisioner_configured", claim_id)) is True):
            dynamic["required_evidence"].append({
                "fact": f"provisioner_parameters_valid/{claim_id}",
                "kind": "driver_parameter_contract",
                "provisioner": sc.get("provisioner"),
                "requirement": "A trusted contract applicable to the deployed driver version and mode",
                "boundary": "A parameter contract does not prove controller or backend availability",
            })
        rules = [item for item in core if item.get("entities", {}).get("c") == claim_id]
        categories = sorted({item["category_id"] for item in rules if item.get("category_id")})
        alternatives = (
            "S2" in categories and "S3" in categories
            and any(
                is_requirement(item, "S3.static-compatibility", "S3/S3.static-binding-compatibility")
                for item in rules
            )
            and not static_only
        )
        result.append({
            "claim": claim_id,
            "scope": "local_storage_paths_excluding_topology_and_shared_volume_contention",
            "storage_class_field": (
                "explicit_empty" if claim.get("storageClassName") == "" else
                "omitted_or_null" if claim.get("storageClassName") is None else "named"
            ),
            "effective_storage_class": class_name,
            "path_policy": (
                "static_only" if static_only else
                "default_provisioning_gap" if default_provisioning_gap else
                "no_exclusive_path_intent_proven"
            ),
            "default_provisioning_gap": default_provisioning_gap,
            "static_only_reasons": static_only,
            "static": {"status": static_status, "candidates": candidates},
            "dynamic": dynamic,
            "core_category_ids": categories,
            "constraint_ids": [item.get("constraint_id") for item in rules],
            "attribution": "alternative_path_conflict_evidence" if alternatives else "path_evidence",
            "repair_guidance": (
                "The benchmark attributes an omitted StorageClass with no observed default "
                "and a complete empty PV inventory to missing default provisioning (S2). "
                "The absent class also makes driver and parameter checks false downstream; "
                "those are not three independent root causes. Verify the intended static or "
                "dynamic path before repairing."
                if default_provisioning_gap else
                "Static incompatibility and dynamic configuration appear in the same claim's conflict evidence. "
                "They constrain alternative paths; do not assume both require edits. "
                "Preserve any explicit storage-path intent and recheck the complete configuration."
                if alternatives else
                "Preserve explicit storage-path intent; unknown facts require evidence, not assumed validity."
            ),
        })
    return result
