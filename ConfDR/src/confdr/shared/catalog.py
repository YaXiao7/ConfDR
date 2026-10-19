"""File boundary between editable rules and validation evidence; the runtime does not depend on the rule generator."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from confdr.shared.io import digest, read, write
from confdr.shared.logic import configure_vocabulary, render, semantics_version, typecheck
from confdr.shared.verification import ACCEPTED, verify_catalog

SOURCE_KEYS = {"schema_version", "taxonomy", "knowledge", "repair_examples", "rules"}


def source_content(catalog: dict) -> dict:
    source = {key: copy.deepcopy(value) for key, value in catalog.items() if key in SOURCE_KEYS}
    for rule in source.get("rules", []):
        rule.pop("fol", None)
    return source


def render_fol(catalog: dict) -> str:
    configure_vocabulary(catalog.get("taxonomy", {}).get("predicates"))
    lines = [
        "# First-order constraints",
        "",
        "Generated from rules.yaml. Edit formula in rules.yaml and run validate-rules to revalidate.",
        "",
        "Validation establishes bounded reference agreement and scenario checks, not unbounded correctness.",
        "These are candidate formulas. Consult verification.json category_status before using a category.",
        "",
    ]
    for rule in catalog.get("rules", []):
        try:
            typecheck(rule["formula"])
            formula = render(rule["formula"])
        except (ValueError, TypeError, KeyError):
            formula = "[Invalid formula; inspect verification.json]"
        lines.extend(
            [
                f"## {rule.get('id', '?')}: {rule.get('title', '')}",
                "",
                f"Category: {rule.get('category_id', '?')}",
                "",
                *[
                    f"- {req['id']}: When {req['when']}, {req['must']}"
                    for category in catalog.get("taxonomy", {}).get("categories", [])
                    if category["id"] == rule.get("category_id")
                    for req in category.get("requirements", [])
                    if req["id"] in rule.get("requirement_ids", [])
                ],
                "",
                "```text",
                formula,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def save_validation(path: str | Path, catalog: dict, *, timeout_ms: int = 3000) -> dict:
    path = Path(path)
    source = source_content(catalog)
    generation = catalog.get("generation", {})
    catalog = {**source, "verification_material": catalog.get("verification_material", {})}
    validation = verify_catalog(catalog, timeout_ms=timeout_ms)
    evidence = {
        "rules_hash": digest(source),
        "semantics_version": semantics_version(source.get("taxonomy", {}).get("semantics_profile")),
        "verification_material": catalog["verification_material"],
        "validation": validation,
        "generation": generation,
    }
    write(path.with_name("verification.json"), evidence)
    write(path.with_name("fol.md"), render_fol(catalog))
    return validation


def save_bundle(directory: str | Path, catalog: dict, *, timeout_ms: int = 3000) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "rules.yaml"
    write(path, yaml.safe_dump(source_content(catalog), allow_unicode=True, sort_keys=False))
    return save_validation(path, catalog, timeout_ms=timeout_ms)


def read_bundle(path: str | Path, *, require_current: bool = True, timeout_ms: int = 3000) -> dict:
    path = Path(path)
    source = read(path)
    if not isinstance(source, dict) or set(source) - SOURCE_KEYS:
        raise ValueError(
            "rules.yaml may contain only rule source fields; validation information must be stored in verification.json"
        )
    evidence = read(path.with_name("verification.json"))
    if not isinstance(evidence, dict):
        raise ValueError("verification.json must be an object")
    if require_current and evidence.get("rules_hash") != digest(source):
        raise ValueError(
            "Rules have changed and validation is stale; first run validate-rules --rules " + str(path)
        )
    if (
        require_current
        and source.get("taxonomy", {}).get("semantics_profile") in {"scheduling-v1", "scheduling-v2"}
        and evidence.get("semantics_version")
        != semantics_version(source.get("taxonomy", {}).get("semantics_profile"))
    ):
        raise ValueError("Interpreter semantics changed; revalidate this rule bundle")
    catalog = {
        **source,
        "verification_material": evidence.get("verification_material", {}),
        "generation": evidence.get("generation", {}),
    }
    if require_current:
        validation = verify_catalog(catalog, timeout_ms=timeout_ms)
        if validation["status"] not in ACCEPTED:
            raise ValueError("Not all rules passed validation; experiments cannot run: " + str(validation))
        catalog["validation"] = validation
    return catalog
