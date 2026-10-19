import copy

import pytest
from jsonschema import Draft202012Validator

from confdr.shared.logic import configure_vocabulary
from confdr.shared.schema import ModelOutputValidator
from confdr.shared.verification import generation_schema


def test_deep_recursive_ast_and_nested_errors(catalog):
    configure_vocabulary()
    # The stock anyOf traversal repeatedly validates args for impossible call/op
    # branches, causing exponential work. Tagged dispatch visits each level once.
    body = {"bool": True}
    leaf = body
    for _ in range(24):
        body = {"op": "not", "args": [body]}
    rule = copy.deepcopy(catalog["rules"][0])
    rule["formula"] = body
    validator = ModelOutputValidator(generation_schema())
    assert validator.is_valid({"rules": [rule]})
    leaf["bool"] = "not a boolean"
    assert not validator.is_valid({"rules": [rule]})


@pytest.mark.parametrize(
    "formula",
    [
        {"bool": True},
        {"int": 1},
        {"bool": "false"},
        {"int": True},
        {},
        {"var": "p"},
        {"var": "Invalid Name"},
        {"bool": True, "extra": 1},
        {"call": "unknown", "args": []},
        {"op": "not", "args": []},
        {"call": "uses_scheduler", "args": [{"var": "p"}]},
        {"op": "ite", "args": [{"bool": True}, {"int": 1}, {"int": 0}]},
    ],
)
def test_dispatch_preserves_schema_acceptance(catalog, formula):
    configure_vocabulary()
    rule = copy.deepcopy(catalog["rules"][0])
    rule["formula"] = formula
    schema = generation_schema()
    instance = {"rules": [rule]}
    assert ModelOutputValidator(schema).is_valid(instance) == Draft202012Validator(schema).is_valid(instance)


def test_formalization_progress_is_flushed_to_stderr(capsys):
    from confdr.shared.progress import progress

    progress("R1: candidate round 1/3")
    output = capsys.readouterr()
    assert not output.out and "R1: candidate round 1/3" in output.err
