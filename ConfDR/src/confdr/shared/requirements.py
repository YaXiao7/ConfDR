"""Declarative extraction coverage checks; these are not a proof of natural-language correctness."""

from confdr.shared.logic import vocabulary


def atoms(expr):
    if isinstance(expr, dict):
        return ({expr["call"]} if "call" in expr else set()) | set().union(*(atoms(v) for v in expr.values()))
    if isinstance(expr, list):
        return set().union(*(atoms(v) for v in expr))
    return set()


def validate_requirements(category):
    if "requirements" not in category:
        return
    reqs = category["requirements"]
    known = {a["name"] for a in vocabulary()}
    if not isinstance(reqs, list) or not reqs:
        raise ValueError("requirements must be a nonempty list")
    ids = set()
    for req in reqs:
        if not isinstance(req, dict) or any(
            not isinstance(req.get(k), str) or not req[k].strip() for k in ("id", "when", "must")
        ):
            raise ValueError("Each requirement needs id, when and must")
        if req["id"] in ids:
            raise ValueError("Duplicate requirement ID")
        ids.add(req["id"])
        if type(req.get("guarded")) is not bool:
            raise ValueError("Each requirement must explicitly declare guarded=true/false")
        names = req.get("required_atoms")
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(n, str) or n not in known for n in names)
        ):
            raise ValueError("Requirement atoms must resolve to the declared vocabulary")
    allowed = category.get("allowed_atoms")
    if not isinstance(allowed, list) or any(not isinstance(a, str) or a not in known for a in allowed):
        raise ValueError("allowed_atoms must resolve to the declared vocabulary")
    if any(not set(r["required_atoms"]) <= set(allowed) for r in reqs):
        raise ValueError("Required atoms must be allowed by the category")
    if category.get("test_policy") != "obligation-boundaries-v1":
        raise ValueError("Structured categories require obligation-boundaries-v1 test policy")


def check_coverage(category, rules):
    validate_requirements(category)
    if "requirements" not in category:
        return
    requirements = {r["id"]: r for r in category["requirements"]}
    grouped = {rid: [] for rid in requirements}
    for rule in rules:
        refs = rule.get("requirement_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(r, str) or r not in requirements for r in refs)
        ):
            raise ValueError(f"Rule {rule['id']} needs valid requirement_ids")
        used = atoms(rule["formula"])
        extra = used - set(category["allowed_atoms"])
        if extra:
            raise ValueError(f"Rule {rule['id']} uses atoms outside its category: {sorted(extra)}")
        for rid in refs:
            grouped[rid].append(used)
    for rid, req in requirements.items():
        used = set().union(*grouped[rid])
        missing = set(req["required_atoms"]) - used
        if not grouped[rid] or missing:
            raise ValueError(f"Requirement {rid} has no covering rule or is missing atoms: {sorted(missing)}")


def check_test_coverage(category, tests):
    if category.get("test_policy") != "obligation-boundaries-v1":
        return
    reqs = {r["id"]: r for r in category["requirements"]}
    covered = {r: set() for r in reqs}
    for test in tests:
        refs, kind = test.get("requirement_ids"), test.get("scenario_kind")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(r, str) or r not in reqs for r in refs)
        ):
            raise ValueError("Scenario must cite declared requirement_ids")
        if kind not in {"positive", "negative", "guard_inactive"}:
            raise ValueError("Scenario needs positive/negative/guard_inactive kind")
        expected = "unsat" if kind == "negative" else "sat"
        if test.get("expected") != expected:
            raise ValueError("Scenario kind contradicts expected solver status")
        for rid in refs:
            covered[rid].add(kind)
    for rid, req in reqs.items():
        needed = {"positive", "negative"}
        if req.get("guarded") is True:
            needed.add("guard_inactive")
        if not needed <= covered[rid]:
            raise ValueError(f"Requirement {rid} lacks scenario coverage: {sorted(needed - covered[rid])}")
