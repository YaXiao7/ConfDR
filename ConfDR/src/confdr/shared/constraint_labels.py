"""Readable labels for concrete constraints; solver tracking IDs remain unchanged."""

import re
from collections import Counter


def _token(value, *, entity=False):
    pattern = r"[^\w-]+" if entity else r"[^\w]+"
    text = re.sub(pattern, "_", str(value)).strip("_")
    return text if entity else text.upper()


def constraint_label(item):
    """Describe the grounded rule, clause and bound entities without inventing failed atoms."""
    if item.get("constraint_label"):
        return item["constraint_label"]
    rule = item.get("rule_id") or item.get("title") or item["constraint_id"]
    category = _token(item.get("category_id") or "")
    name = _token(rule.split("/", 1)[-1] if category else rule)
    if category and name != category and not name.startswith(category + "_"):
        name = category + "_" + name
    if str(rule).startswith("SHARED/") and item.get("title"):
        name += "_" + _token(item["title"])
    clause = item.get("clause", "root")
    if clause != "root":
        name += "_CLAUSE_" + _token(clause.removeprefix("root."))
    for variable, entity in sorted(item.get("entities", {}).items()):
        name += "_FOR_" + _token(variable) + "_" + _token(entity, entity=True)
    return name


def constraint_labels(items):
    """Disambiguate equal readable labels so distinct constraints never collapse in reports."""
    labels = {item["constraint_id"]: constraint_label(item) for item in items}
    counts = Counter(labels.values())
    return {key: value if counts[value] == 1 else value + "__" + _token(key) for key, value in labels.items()}
