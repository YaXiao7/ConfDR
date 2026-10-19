import copy
import subprocess
import sys

import pytest
import yaml

from confdr.formalization.generation import validate_taxonomy
from confdr.formalization.service import revalidate
from confdr.runtime.solver import solve
from confdr.shared.catalog import read_bundle, save_bundle
from confdr.shared.io import read, write


def test_blank_entry_is_intentional():
    with pytest.raises(ValueError, match="Taxonomy is empty"):
        validate_taxonomy({"categories": []})


def test_rule_edit_requires_validation(tmp_path, catalog, case):
    output = tmp_path / "rules"
    assert save_bundle(output, catalog)["status"] == "bounded_verified"
    path = output / "rules.yaml"
    assert solve(case, read_bundle(path))["status"] == "unsat"
    source = read(path)
    source["rules"][0]["title"] = "edited title"
    write(path, yaml.safe_dump(source))
    with pytest.raises(ValueError, match="validation is stale"):
        read_bundle(path)
    before = path.read_bytes()
    assert revalidate(path)["status"] == "bounded_verified"
    assert path.read_bytes() == before
    assert read_bundle(path)["rules"][0]["title"] == "edited title"
    source["rules"][0]["formula"] = {"bool": True}
    write(path, yaml.safe_dump(source))
    assert revalidate(path)["status"] == "rejected"
    with pytest.raises(ValueError, match="Not all rules passed"):
        read_bundle(path)


def test_solver_uses_current_facts(catalog, case):
    failed = solve(case, catalog)
    assert failed["status"] == "unsat" and failed["cores"] and failed["core_search_complete"]
    repaired = copy.deepcopy(case)
    for pod in repaired["WaitingDeployPods"].values():
        pod["nodeSelector"] = {"slot": "present"}
    passed = solve(repaired, catalog)
    assert passed["status"] == "sat" and passed["formal_complete"]


def test_runtime_does_not_import_generator():
    code = "import sys; import confdr.runtime.experiment; assert not any(n.startswith(('confdr.formalization', 'confdr.validation')) for n in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_generated_bundle_can_be_used_independently(tmp_path, catalog):
    from confdr.formalization.service import generate

    class GenerationTransport:
        backend = "test_transport"
        model = "test_transport"

        def complete(self, task, system, payload, schema):
            if task == "plan_vocabulary":
                return {"predicates": []}
            if task == "generate_rules":
                assert "reference" not in payload and "tests" not in payload
                return {"rules": copy.deepcopy(catalog["rules"])}
            if task == "independent_spec":
                assert "rules" not in payload and "previous_candidate" not in payload
                tests = []
                for i, allowed in enumerate((False, True, False, True)):
                    tests.append(
                        {
                            "id": str(i),
                            "category_id": "test.constraint",
                            "description": f"Explicit selector match={allowed}",
                            "domains": {"Pod": ["p"], "Node": ["n"], "Claim": [], "Volume": []},
                            "facts": [
                                {"predicate": "selector_ok", "arguments": ["p", "n"], "value": allowed}
                            ],
                            "expected": "sat" if allowed else "unsat",
                        }
                    )
                return {"rules": copy.deepcopy(catalog["rules"]), "tests": tests}
            if task == "backtranslate":
                return {"interpretation": "Each Pod must be assigned to a node matching its selector."}
            assert task == "semantic_review"
            return {
                "equivalent": True,
                "coverage_complete": True,
                "issues": [],
                "summary": "Test semantics agree",
            }

    path = tmp_path / "taxonomy.yaml"
    write(path, yaml.safe_dump(catalog["taxonomy"]))
    llm = GenerationTransport()
    report = generate(path, tmp_path / "generated", llm, llm)
    assert report["status"] == "empirically_validated"
    bundle = read_bundle(tmp_path / "generated/rules.yaml")
    assert bundle["validation"]["status"] == "empirically_validated"
    assert len(list((tmp_path / "generated").iterdir())) == 3
