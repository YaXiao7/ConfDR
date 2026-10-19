"""Merge category evidence without promoting or rewriting any stored review verdict."""

from __future__ import annotations

import copy
from pathlib import Path

from confdr.shared.catalog import read_bundle, save_bundle
from confdr.shared.io import digest, read
from confdr.shared.logic import SEMANTICS_VERSION
from confdr.shared.review import REVIEW_PROTOCOL
from confdr.shared.verification import verify_catalog


def _load(path):
    path = Path(path)
    catalog = read_bundle(path, require_current=False)
    evidence = read(path.with_name("verification.json"))
    if evidence.get("rules_hash") != digest(read(path)):
        raise ValueError(f"Stale source hash: {path}; revalidate the input first")
    if evidence.get("semantics_version") != SEMANTICS_VERSION:
        raise ValueError(f"Stale interpreter semantics: {path}; revalidate the input first")
    material = catalog.get("verification_material", {})
    if material.get("origin") != "blind_llm" or material.get("protocol") != REVIEW_PROTOCOL:
        raise ValueError(f"Merge requires current independently reviewed LLM evidence: {path}")
    return catalog, evidence


def merge_bundles(
    base_path, patch_path, output, *, category_ids, timeout_ms=3000, allow_new_categories=False
):
    """Replace selected categories; keep the base taxonomy and all review hashes intact."""
    if Path(output).exists():
        raise ValueError("Rule output directory already exists; choose a new directory")
    selected = set(category_ids)
    if not selected or len(selected) != len(category_ids):
        raise ValueError("Select nonempty, unique replacement category IDs")
    base, base_evidence = _load(base_path)
    patch, patch_evidence = _load(patch_path)
    base_cats = {c["id"]: c for c in base["taxonomy"]["categories"]}
    patch_cats = {c["id"]: c for c in patch["taxonomy"]["categories"]}
    if not selected <= patch_cats.keys() or (not allow_new_categories and not selected <= base_cats.keys()):
        raise ValueError("Selected categories must exist in both input taxonomies")
    if base["schema_version"] != patch["schema_version"] or any(
        base_cats[c] != patch_cats[c] for c in selected & base_cats.keys()
    ):
        raise ValueError("Category definitions differ; regenerate against the same taxonomy")
    new_categories = selected - base_cats.keys()
    metadata_ignored = {"categories"}
    if allow_new_categories:
        metadata_ignored |= {"taxonomy_version", "predicates"}
    if {k: v for k, v in base["taxonomy"].items() if k not in metadata_ignored} != {
        k: v for k, v in patch["taxonomy"].items() if k not in metadata_ignored
    }:
        raise ValueError("Taxonomy metadata or predicate vocabulary differs between bundles")
    predicates = {p["name"]: copy.deepcopy(p) for p in base["taxonomy"].get("predicates", [])}
    if allow_new_categories:
        for predicate in patch["taxonomy"].get("predicates", []):
            name = predicate["name"]
            if name in predicates and predicates[name] != predicate:
                raise ValueError(f"Predicate definition differs: {name}")
            predicates[name] = copy.deepcopy(predicate)
    # A focused generation projects source categories. Permit only that metadata change,
    # never changes to source text, URLs, ordering or missing/additional source evidence.
    for cat in sorted(selected):
        expected = [
            {**s, "categories": [c for c in s["categories"] if c in patch_cats]}
            for s in base["knowledge"]
            if cat in s["categories"]
        ]
        actual = [s for s in patch["knowledge"] if cat in s["categories"]]
        if cat not in new_categories and expected != actual:
            raise ValueError(f"Knowledge differs for {cat}; use matching source content")
        subset = {
            **patch,
            "taxonomy": {**patch["taxonomy"], "categories": [patch_cats[cat]]},
            "rules": [r for r in patch["rules"] if r["category_id"] == cat],
        }
        report = verify_catalog(subset, timeout_ms=timeout_ms)
        if report["status"] != "empirically_validated":
            raise ValueError(f"Replacement category {cat} failed fresh verification: {report}")
    merged = copy.deepcopy(base)
    for category in patch["taxonomy"]["categories"]:
        if category["id"] in new_categories:
            merged["taxonomy"]["categories"].append(copy.deepcopy(category))
    if allow_new_categories:
        if predicates or "predicates" in merged["taxonomy"] or "predicates" in patch["taxonomy"]:
            merged["taxonomy"]["predicates"] = list(predicates.values())
        if new_categories and "taxonomy_version" in patch["taxonomy"]:
            merged["taxonomy"]["taxonomy_version"] = patch["taxonomy"]["taxonomy_version"]
        sources = {source["id"]: source for source in merged["knowledge"]}
        for source in patch["knowledge"]:
            added = [c for c in source["categories"] if c in new_categories]
            if not added:
                continue
            existing = sources.get(source["id"])
            if existing is not None:
                if {k: v for k, v in existing.items() if k != "categories"} != {
                    k: v for k, v in source.items() if k != "categories"
                }:
                    raise ValueError(f"Knowledge source definition differs: {source['id']}")
                existing["categories"] = list(dict.fromkeys(existing["categories"] + added))
            else:
                item = {**copy.deepcopy(source), "categories": added}
                merged["knowledge"].append(item)
                sources[item["id"]] = item
    merged["repair_examples"] = [
        item
        for item in merged.get("repair_examples", [])
        if not (set(item.get("category_ids", [])) & selected)
    ] + [
        copy.deepcopy(item)
        for item in patch.get("repair_examples", [])
        if set(item.get("category_ids", [])) & selected and set(item.get("category_ids", [])) <= selected
    ]
    merged["rules"] = [
        copy.deepcopy(rule)
        for cat in [c["id"] for c in merged["taxonomy"]["categories"]]
        for rule in (patch["rules"] if cat in selected else base["rules"])
        if rule["category_id"] == cat
    ]
    material, replacement = merged["verification_material"], patch["verification_material"]
    for cat in selected:
        material["reference"]["categories"][cat] = copy.deepcopy(replacement["reference"]["categories"][cat])
        for key in ("reviews", "reference_reviews"):
            material.setdefault(key, {})[cat] = copy.deepcopy(replacement[key][cat])
    material["tests"] = [
        t for t in material.get("tests", []) if t["category_id"] not in selected
    ] + copy.deepcopy([t for t in replacement.get("tests", []) if t["category_id"] in selected])
    merged["generation"] = {
        "method": "category_bundle_merge",
        "merge": {
            "categories": sorted(selected),
            "added_categories": sorted(new_categories),
            "inputs": [
                {
                    "role": role,
                    "rules_path": str(Path(path).resolve()),
                    "rules_hash": evidence["rules_hash"],
                    "evidence_hash": digest(evidence),
                    "generation": evidence.get("generation", {}),
                }
                for role, path, evidence in (
                    ("base", base_path, base_evidence),
                    ("replacement", patch_path, patch_evidence),
                )
            ],
        },
    }
    # Recompute status, content hashes and rendered FOL. Changed source contexts deliberately
    # invalidate copied reviews; validate-rules can re-review just those records online.
    return save_bundle(output, merged, timeout_ms=timeout_ms)
