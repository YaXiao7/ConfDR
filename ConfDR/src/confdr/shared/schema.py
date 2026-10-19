"""Validate recursive tagged AST unions without revisiting impossible branches."""

from jsonschema import Draft202012Validator, ValidationError, validators


def _tagged_any_of(validator, alternatives, instance, schema):
    # The AST union consists of closed objects. Keys and enum tags are necessary
    # conditions, so excluding nonmatching branches preserves JSON Schema semantics.
    if not isinstance(instance, dict) or not all(
        isinstance(branch, dict)
        and branch.get("type") == "object"
        and branch.get("additionalProperties") is False
        and isinstance(branch.get("properties"), dict)
        and set(branch) <= {"type", "additionalProperties", "properties", "required"}
        for branch in alternatives
    ):
        yield from Draft202012Validator.VALIDATORS["anyOf"](validator, alternatives, instance, schema)
        return
    errors = []
    for index, branch in enumerate(alternatives):
        properties = branch["properties"]
        if not set(branch.get("required", [])) <= instance.keys() <= properties.keys():
            continue
        if any(
            "enum" in definition and instance[key] not in definition["enum"]
            for key, definition in properties.items()
            if key in instance and isinstance(definition, dict)
        ):
            continue
        branch_errors = list(validator.descend(instance, branch, schema_path=index))
        if not branch_errors:
            return
        errors.extend(branch_errors)
    yield ValidationError(
        "Object does not match an allowed AST shape, tag or argument schema", context=errors
    )


ModelOutputValidator = validators.extend(Draft202012Validator, {"anyOf": _tagged_any_of})
