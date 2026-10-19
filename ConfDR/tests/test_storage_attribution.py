from confdr.runtime.attribution import attribute_conflicts, attributed_categories


def evidence(category, rule, claim):
    return {"category_id": category, "rule_id": rule, "entities": {"c": claim}}


def test_path_selection_is_supporting_evidence_for_same_claim():
    core = [
        evidence("S3", "S3/S3.path-selection", "default/data"),
        evidence("S2", "S2/S2.class.dynamic-storage-class-exists", "default/data"),
    ]
    assert attributed_categories(core) == ["S2"]
    core[1] = evidence("S4", "S4/S4-static-selected-volume-topology", "default/data")
    assert attributed_categories(core) == ["S4"]


def test_independent_binding_failure_keeps_s3():
    core = [
        evidence("S3", "S3/S3.path-selection", "default/data"),
        evidence("S2", "S2/S2.class.dynamic-storage-class-exists", "default/other"),
    ]
    assert attributed_categories(core) == ["S2", "S3"]
    core[0] = evidence("S3", "S3/S3.static-binding-compatibility", "default/other")
    assert attributed_categories(core) == ["S2", "S3"]


def test_mixed_claims_keep_independent_failure_and_explain_support_separately():
    core = [
        evidence("S3", "S3/S3.path-selection", "default/dynamic"),
        evidence("S2", "S2/S2.class.dynamic-storage-class-exists", "default/dynamic"),
        evidence("S3", "S3/S3.static-binding-compatibility", "default/static"),
    ]
    roles = attribute_conflicts(core)
    assert roles[0]["role"] == "supporting_path_obligation"
    assert roles[2]["role"] == "conflict_evidence"
    assert attributed_categories(core) == ["S2", "S3"]


def test_missing_claim_identity_cannot_suppress_an_unrelated_path():
    core = [
        evidence("S3", "S3/S3.path-selection", None),
        evidence("S2", "S2/S2.class.dynamic-storage-class-exists", None),
    ]
    assert attributed_categories(core) == ["S2", "S3"]


def test_regenerated_rule_ids_are_matched_by_requirement():
    # Rule IDs are produced by the generation model and may change between bundles.
    path = {**evidence("S3", "S3/S3.path", "default/data"), "requirement_ids": ["S3.path"]}
    blocking = evidence("S2", "S2/S2.dynamic-access-modes-supported", "default/data")
    assert attributed_categories([path, blocking]) == ["S2"]
    renamed = {**evidence("S3", "S3/S3.static-compatibility", "default/data"),
               "requirement_ids": ["S3.static-compatibility"]}
    assert attributed_categories([renamed, blocking]) == ["S2", "S3"]
