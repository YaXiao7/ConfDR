"""Offline file entry points and online formalization, independent of detection and repair."""

from __future__ import annotations

from confdr.formalization.generation import (
    generate_reference,
    generate_rules,
    validate_taxonomy,
)
from confdr.shared.catalog import read_bundle, save_bundle, save_validation
from confdr.shared.contracts import verification_base
from confdr.shared.io import digest, read
from confdr.shared.logic import configure_vocabulary
from confdr.shared.review import REVIEW_PROTOCOL, review_passed, review_rules


def generate(
    taxonomy_path,
    output,
    llm,
    verifier,
    *,
    knowledge_path=None,
    max_rounds=3,
    timeout_ms=3000,
    category_ids=None,
):
    taxonomy = validate_taxonomy(read(taxonomy_path))
    knowledge = read(knowledge_path) if knowledge_path else None
    if category_ids is not None:
        requested = set(category_ids)
        known = {item["id"] for item in taxonomy["categories"]}
        if not requested or requested - known:
            raise ValueError(f"Unknown or empty category selection: {sorted(requested - known)}")
        taxonomy["categories"] = [item for item in taxonomy["categories"] if item["id"] in requested]
        if "validation_cases" in taxonomy:
            taxonomy["validation_cases"] = [
                t for t in taxonomy["validation_cases"] if t["category_id"] in requested
            ]
        if knowledge is not None:
            if not isinstance(knowledge, list):
                raise ValueError("knowledge must be a list")
            knowledge = [
                {**item, "categories": [cat for cat in item["categories"] if cat in requested]}
                for item in knowledge
                if requested.intersection(item["categories"])
            ]
    catalog = generate_rules(
        llm,
        taxonomy,
        verifier=verifier,
        knowledge=knowledge,
        max_rounds=max_rounds,
        timeout_ms=timeout_ms,
    )
    # Combine records needed to diagnose generation failures in the verification file; do not create per-round intermediate files.
    generation = catalog.get("generation", {})
    generation["model_calls"] = list(getattr(llm, "calls", []))
    if verifier is not llm:
        generation["model_calls"] += list(getattr(verifier, "calls", []))
    return save_bundle(output, catalog, timeout_ms=timeout_ms)


def revalidate(path, *, verifier=None, refresh_reference=False, timeout_ms=3000, max_rounds=3):
    if not 1 <= max_rounds <= 10:
        raise ValueError("Reference extraction rounds must be between 1 and 10")
    catalog = read_bundle(path, require_current=False)
    validate_taxonomy(catalog["taxonomy"])
    material = catalog["verification_material"]
    if refresh_reference:
        if verifier is None:
            raise ValueError(
                "Refreshing independent references requires a configured online validation model"
            )
        material = {
            "origin": "blind_llm",
            "protocol": REVIEW_PROTOCOL,
            "reference_reviews": {},
            "reference": {"categories": {}},
            "tests": [],
            "reviews": {},
        }
        catalog["verification_material"] = material
    configure_vocabulary(catalog["taxonomy"].get("predicates"))
    if material.get("origin") == "blind_llm" and verifier is not None:
        for category in catalog["taxonomy"]["categories"]:
            cat = category["id"]
            base = verification_base(catalog["taxonomy"], category, catalog["knowledge"])
            rules = [r for r in catalog["rules"] if r["category_id"] == cat]
            if refresh_reference:
                spec = generate_reference(
                    verifier,
                    base,
                    catalog,
                    max_rounds=max_rounds,
                    timeout_ms=timeout_ms,
                    attempts=catalog.setdefault("generation", {}).setdefault("reference_attempts", []),
                )
                material["reference"]["categories"][cat] = spec["rules"]
                material["reference_reviews"][cat] = spec["review_record"]
                material["tests"].extend(spec["tests"])
            # A category merge may change source context without changing its blind reference.
            # Re-review that reference in the current context; never forge a new source hash.
            reference = material.get("reference", {}).get("categories", {}).get(cat, [])
            prior_reference = material.setdefault("reference_reviews", {}).get(cat, {})
            if material.get("protocol") == REVIEW_PROTOCOL and not (
                prior_reference.get("candidate_hash") == digest(reference)
                and prior_reference.get("source_hash") == digest(base)
                and prior_reference.get("protocol") == REVIEW_PROTOCOL
                and review_passed(prior_reference.get("review", {}))
            ):
                from confdr.shared.verification import check_structure, verify_scenarios

                reference_catalog = {
                    **catalog,
                    "taxonomy": base["taxonomy"],
                    "rules": reference,
                }
                try:
                    check_structure(reference_catalog, catalog["knowledge"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not verify_scenarios(reference_catalog, cat, timeout_ms=timeout_ms)["passed"]:
                    continue
                material["reference_reviews"][cat] = review_rules(
                    verifier, base, reference, subject="reference"
                )
            prior = material.setdefault("reviews", {}).get(cat, {})
            if (
                prior.get("candidate_hash") == digest(rules)
                and prior.get("source_hash") == digest(base)
                and review_passed(prior.get("review", {}))
            ):
                continue
            # Invalid ASTs are reported by machine validation; do not ask a model to interpret malformed rules.
            from confdr.shared.verification import check_structure

            try:
                check_structure(
                    {**catalog, "taxonomy": base["taxonomy"], "rules": rules}, catalog["knowledge"]
                )
            except (ValueError, TypeError, KeyError):
                continue
            material["reviews"][cat] = review_rules(verifier, base, rules, subject="candidate")
    if verifier is not None:
        catalog.setdefault("generation", {}).setdefault("revalidation_model_calls", []).extend(
            getattr(verifier, "calls", [])
        )
    return save_validation(path, catalog, timeout_ms=timeout_ms)
