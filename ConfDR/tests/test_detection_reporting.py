"""First raw cores, overlapping MUSes and labelled evaluation remain distinct."""

import z3

from confdr.runtime.conflicts import enumerate_conflicts
from confdr.runtime.evaluation import compare_labels, multilabel_metrics, truth
from confdr.runtime.reporting import compare_detection, evaluate_detection_views, format_conflicts
from confdr.shared.io import write


def test_first_core_and_overlapping_muses():
    a, b = z3.Bools("report_a report_b")
    formulas = [a, z3.Not(a), z3.And(z3.Not(a), b)]
    report = enumerate_conflicts(formulas)
    assert report["complete"]
    assert {tuple(core) for core in report["cores"]} == {(0, 1), (0, 2)}
    check = z3.Solver()
    check.add(*[formulas[i] for i in report["initial_core"]])
    assert check.check() == z3.unsat
    # Deleting all members of the first MUS would miss the other overlapping conflict.
    assert 0 in report["initial_core"]


def test_pending_labels_match_current_catalog_without_mapping(tmp_path):
    write(tmp_path / "truth.json", {"is_pending": True, "pending_labels": ["S3", "S2", "S3"]})
    expected = truth(tmp_path, known_categories=["S2", "S3"])
    assert expected["category_labelled"]
    assert expected["category_ids"] == ["S2", "S3"]
    comparison = compare_labels(expected, {"status": "fault", "category_ids": ["S3", "R1"]})
    assert comparison["tp"] == ["S3"]
    assert comparison["fp"] == ["R1"]
    assert comparison["fn"] == ["S2"]


def test_unknown_label_not_silently_dropped(tmp_path):
    write(tmp_path / "truth.json", {"is_pending": True, "pending_labels": ["S3", "legacy-storage"]})
    expected = truth(tmp_path, known_categories=["S3"])
    assert not expected["category_labelled"]
    assert expected["unmapped_labels"] == ["legacy-storage"]
    assert not compare_labels(expected, {"status": "fault", "category_ids": ["S3"]})["scored"]
    mapped = truth(tmp_path, {"legacy-storage": ["S2"]}, known_categories=["S3"])
    assert mapped["category_ids"] == ["S2", "S3"]


def test_multilabel_precision_counts_false_positives_and_abstentions():
    records = [
        {
            "truth": {"status": "fault", "category_ids": ["S2", "S3"], "category_labelled": True},
            "result": {"detection": {"status": "fault", "category_ids": ["S3", "R1"]}},
        },
        {
            "truth": {"status": "fault", "category_ids": ["R1"], "category_labelled": True},
            "result": {"detection": {"status": "unknown", "category_ids": []}},
        },
        {
            "truth": {"status": "healthy", "category_ids": [], "category_labelled": True},
            "result": {"detection": {"status": "fault", "category_ids": ["R1"]}},
        },
    ]
    metrics = multilabel_metrics(records)
    assert metrics["micro"] == {"tp": 1, "fp": 2, "fn": 2, "precision": 1 / 3, "recall": 1 / 3, "f1": 1 / 3}
    assert metrics["exact_match"] == 0 and metrics["abstentions"] == 1
    assert metrics["per_category"]["R1"]["fp"] == 2


def test_views_do_not_invent_historical_first_core_or_recovery():
    c = {"constraint_id": "constraint_1", "category_id": "S3", "rule_id": "S3/bind", "entities": {}}
    detection = {
        "status": "fault",
        "category_ids": ["S3"],
        "formal": {
            "status": "unsat",
            "solver_status": "unsat",
            "core": [c],
            "cores": [[c]],
            "core_search_complete": False,
            "reason": "search_timeout",
        },
    }
    expected = {"status": "fault", "category_ids": ["S3"], "category_labelled": True}
    report = compare_detection(expected, detection)
    assert report["single_round"]["abstained"]
    assert report["single_round"]["fn"] == ["S3"]
    output = format_conflicts("case", detection)
    assert "First Z3 core was not recorded" in output
    assert "Search incomplete" in output and "Original constraints remain UNSAT" in output
    assert (
        evaluate_detection_views([{"truth": expected, "result": {"detection": detection}}])["single_round"][
            "unrecorded_cases"
        ]
        == 1
    )
    detection["formal"]["initial_core"] = [c]
    assert compare_detection(expected, detection)["single_round"]["exact_match"]
    assert "not necessarily minimal" in format_conflicts("case", detection)


def test_healthy_empty_labels_and_missing_truth_are_distinct(tmp_path):
    assert not compare_labels(truth(tmp_path), {"status": "healthy"})["scored"]
    write(tmp_path / "truth.json", {"is_pending": False, "pending_labels": []})
    expected = truth(tmp_path, known_categories=["R1"])
    assert compare_labels(expected, {"status": "healthy"})["exact_match"]
    assert not compare_labels(expected, {"status": "unknown"})["exact_match"]


def test_text_labels_identify_entities_and_clauses_without_collapsing_collisions():
    from confdr.shared.constraint_labels import constraint_labels

    base = {"rule_id": "S3/S3.static-binding-compatibility", "category_id": "S3", "clause": "root.then.0"}
    items = [
        {**base, "constraint_id": "constraint_0", "entities": {"c": "default/a-b"}},
        {**base, "constraint_id": "constraint_1", "entities": {"c": "default_a-b"}},
        {**base, "constraint_id": "constraint_2", "entities": {"c": "default/a-b"}, "clause": "root.then.1"},
    ]
    names = constraint_labels(items)
    assert len(set(names.values())) == 3
    assert all(name.startswith("S3_STATIC_BINDING_COMPATIBILITY_CLAUSE_THEN_") for name in names.values())
    assert all("FOR_C_default_a-b" in name for name in names.values())
    assert names == constraint_labels(list(reversed(items)))


def test_first_round_union_and_json_share_readable_constraint_labels(case, catalog):
    from confdr.runtime.reporting import format_multiple_rounds, format_single_round
    from confdr.runtime.solver import solve

    formal = solve(case, catalog)
    detection = {"status": "fault", "formal": formal}
    first = format_single_round("example", detection)
    combined = format_multiple_rounds("example", detection)
    assert "UNSAT in round 1:" in combined
    assert "SAT: System became satisfiable after filtering conflicting constraints." in combined
    assert "Union of all unsatisfiable constraint labels:" in combined
    assert "Overall formal result: UNSAT" in combined
    assert " | " not in first and "constraint_" not in combined
    for item in formal["initial_core"]:
        label = item["constraint_label"]
        assert first.count("   " + label + "\n") == 1
        assert combined.count("   " + label + "\n") >= 2
        assert item["constraint_id"].startswith("constraint_")
        assert item["title"]
