"""CLI selection, fixed-background MUS correctness and interrupted-search evidence."""

import itertools

import pytest
import z3
from test_experiment import TransportDouble, sample

from confdr.cli import parser
from confdr.runtime.conflicts import enumerate_conflicts, skip_conflicts
from confdr.runtime.reporting import conflict_views, format_conflicts
from confdr.runtime.solver import solve
from confdr.shared.io import read


def test_overlapping_muses_are_found_even_when_skipping_misses_one():
    x = z3.Int("overlap")
    formulas = [x >= 1, x <= 0, x <= -1]
    skipped = skip_conflicts(formulas)
    full = enumerate_conflicts(formulas)
    assert len(skipped["cores"]) == 1 and not skipped["exhaustive"]
    assert {tuple(core) for core in full["cores"]} == {(0, 1), (0, 2)}
    assert full["exhaustive"] and full["solver_status"] == full["terminal_status"] == "unsat"


def test_protected_background_muses_match_bruteforce_oracle():
    x, y, z = z3.Bools("oracle_x oracle_y oracle_z")
    formulas = [z3.Or(x, y), z3.Not(x), z3.Not(y), z, z3.Not(z), z3.Not(x)]
    protected = {0}
    result = enumerate_conflicts(formulas, protected=protected)
    indices = set(range(len(formulas))) - protected

    def unsat(selected):
        solver = z3.Solver()
        solver.add(*[formulas[i] for i in protected | selected])
        return solver.check() == z3.unsat

    expected = set()
    for size in range(len(indices) + 1):
        for subset in itertools.combinations(sorted(indices), size):
            selected = set(subset)
            if unsat(selected) and all(not unsat(selected - {i}) for i in selected):
                expected.add(subset)
    assert result["complete"] and result["minimality_proved"]
    assert {tuple(core) for core in result["cores"]} == expected
    for core, context in zip(result["cores"], result["core_contexts"], strict=True):
        assert not (set(core) & protected) and set(context) <= protected
        solver = z3.Solver()
        solver.add(*[formulas[i] for i in core + context])
        assert solver.check() == z3.unsat


def test_background_inconsistency_is_not_a_minimal_category_fault():
    x = z3.Bool("bad_background")
    result = enumerate_conflicts([x, z3.Not(x), z3.BoolVal(True)], protected=[0, 1])
    assert result["solver_status"] == "unsat"
    assert result["base_inconsistent"] and not result["complete"] and not result["exhaustive"]
    assert result["cores"] == [] and not result["minimality_proved"]
    assert set(result["unminimized_cores"][0]) == {0, 1}


def _interrupt_solver(monkeypatch, *, after, timeout=False):
    import confdr.runtime.conflicts as module

    factory = z3.Solver
    state = {"checks": 0, "time": 0.0}

    class InterruptedSolver:
        def __init__(self):
            self.inner = factory()

        def __getattr__(self, key):
            return getattr(self.inner, key)

        def check(self, *args):
            state["checks"] += 1
            if state["checks"] == after:
                if not timeout:
                    return z3.unknown
                state["time"] = 2.0
            return self.inner.check(*args)

        def reason_unknown(self):
            return "test_interrupted"

    monkeypatch.setattr(module.z3, "Solver", InterruptedSolver)
    if timeout:
        monkeypatch.setattr(module.time, "monotonic", lambda: state["time"])


@pytest.mark.parametrize("timeout", [False, True])
def test_interrupted_shrinking_retains_unsat_without_claiming_minimality(monkeypatch, timeout):
    x = z3.Bool("interrupted")
    # Full input, fixed background, seed; the following deletion check is interrupted.
    _interrupt_solver(monkeypatch, after=3 if timeout else 4, timeout=timeout)
    result = enumerate_conflicts([x, z3.Not(x)], search_timeout_s=1)
    assert result["solver_status"] == result["terminal_status"] == "unsat"
    assert not result["complete"] and not result["exhaustive"]
    assert result["cores"] == [] and not result["minimality_proved"]
    assert set(result["unminimized_cores"][0]) == set(result["initial_core"]) == {0, 1}
    assert result["reason"] == ("search_timeout" if timeout else "test_interrupted")


def test_first_unknown_remains_unknown_without_invented_conflicts(monkeypatch):
    _interrupt_solver(monkeypatch, after=1)
    result = enumerate_conflicts([z3.BoolVal(True)])
    assert result["solver_status"] == "unknown"
    assert not result["complete"] and not result["exhaustive"]
    assert result["cores"] == result["initial_core"] == result["unminimized_cores"] == []


def test_solver_and_reports_separate_raw_core_mus_and_background(case, catalog):
    formal = solve(case, catalog, conflict_method="mus")
    assert formal["status"] == "unsat" and formal["all_conflicts_enumerated"]
    assert formal["core_search_complete"] and formal["minimality_proved"]
    assert all(c["category_id"] is not None for core in formal["cores"] for c in core)
    assert all(c["category_id"] is None for core in formal["core_contexts"] for c in core)
    assert formal["core_contexts"] and formal["unminimized_cores"] == []
    detection = {"formal": formal}
    text = format_conflicts("example", detection)
    assert "Single-Round" in text and "--- MUS 1 ---" in text
    assert "Supporting fixed background labels:" in text
    assert "MUS enumeration complete: yes" in text
    assert "System became satisfiable" not in text and "constraint_" not in text
    views = conflict_views(detection)
    assert not views["single_round"]["minimality_proved"]
    assert views["all_conflicts"]["minimality_proved"] and views["all_conflicts"]["exhaustive"]


def test_incomplete_mus_evidence_reaches_detection_report(monkeypatch, case, catalog):
    import confdr.runtime.solver as module

    def interrupted(formulas, *args, **kwargs):
        complete = enumerate_conflicts(formulas, *args, **kwargs)
        return {
            **complete,
            "cores": [],
            "core_contexts": [],
            "complete": False,
            "exhaustive": False,
            "minimality_proved": False,
            "unminimized_cores": [complete["initial_core"]],
            "reason": "search_timeout",
        }

    monkeypatch.setattr(module, "enumerate_conflicts", interrupted)
    formal = solve(case, catalog, conflict_method="mus")
    assert formal["status"] == "unsat" and formal["category_ids"] == ["test.constraint"]
    assert formal["core"] and not formal["all_conflicts_enumerated"]
    text = format_conflicts("example", {"formal": formal})
    assert "Unminimized core 1" in text and "minimality has not been proved" in text
    assert "MUS enumeration complete: no" in text and "Search incomplete: search_timeout" in text


@pytest.mark.parametrize("command", ["run", "experiment"])
@pytest.mark.parametrize("method", ["label_skipping", "mus"])
def test_cli_exposes_methods_and_separate_budgets(command, method):
    args = parser().parse_args(
        [
            command,
            "--dataset",
            "input",
            "--rules",
            "rules.yaml",
            "--output",
            "out",
            "--conflict-method",
            method,
            "--search-timeout-s",
            "0",
            "--solver-timeout-ms",
            "1234",
            "--max-rounds",
            "2",
        ]
    )
    assert args.conflict_method == method and args.search_timeout_s == 0
    assert args.solver_timeout_ms == 1234 and args.max_rounds == 2


@pytest.mark.parametrize(
    "option,value",
    [
        ("--conflict-method", "invalid"),
        ("--search-timeout-s", "nan"),
        ("--search-timeout-s", "inf"),
        ("--search-timeout-s", "-1"),
        ("--solver-timeout-ms", "0"),
    ],
)
def test_invalid_cli_detection_options_fail_before_running(option, value):
    with pytest.raises(SystemExit):
        parser().parse_args(["run", "--dataset", "in", "--rules", "r", "--output", "out", option, value])


@pytest.mark.parametrize(
    "method,algorithm", [("label_skipping", "label_skipping"), ("mus", "MUS_enumeration")]
)
def test_cli_selection_reaches_initial_detection_and_repair_rechecks(
    tmp_path, monkeypatch, case, documents, catalog, method, algorithm
):
    import confdr.cli
    import confdr.shared.catalog
    import confdr.shared.llm

    source = sample(tmp_path, documents, case)
    model = TransportDouble()
    model.preflight = lambda: None
    monkeypatch.setattr(confdr.shared.catalog, "read_bundle", lambda *a, **kw: catalog)
    monkeypatch.setattr(confdr.shared.llm, "LLM", lambda *a, **kw: model)
    output = tmp_path / "out"
    assert (
        confdr.cli.main(
            [
                "run",
                "--dataset",
                str(source),
                "--rules",
                str(tmp_path / "rules.yaml"),
                "--output",
                str(output),
                "--conflict-method",
                method,
                "--search-timeout-s",
                "9",
                "--solver-timeout-ms",
                "3000",
            ]
        )
        == 0
    )
    summary = read(output / "experiment.json")
    assert summary["solve_options"]["conflict_method"] == method
    assert summary["solve_options"]["search_timeout_s"] == 9
    assert summary["solve_options"]["timeout_ms"] == 3000
    result = read(output / "dataset/source/result.json")
    assert result["detection"]["formal"]["conflict_method"] == algorithm
    assert result["repair"]["status"] == "candidate_accepted"
    assert result["repair"]["rounds_used"] == 2
    assert all(
        r["validation"]["detection"]["formal"]["conflict_method"] == algorithm
        for r in result["repair"]["attempts"]
    )
    assert all(call["task"] != "diagnose" for call in model.calls)


def test_unknown_map_search_retains_verified_mus(monkeypatch):
    x = z3.Bool("map_interrupted")
    # Full check, background, seed, two deletions, then the MARCO map check.
    _interrupt_solver(monkeypatch, after=6)
    result = enumerate_conflicts([x, z3.Not(x)])
    assert result["solver_status"] == "unsat" and result["minimality_proved"]
    assert result["cores"] == [[0, 1]] and result["unminimized_cores"] == []
    assert not result["exhaustive"] and not result["complete"]


def test_frozen_deployment_postcheck_honors_mus(case, documents, catalog):
    from confdr.validation.repair_metrics import postcheck

    report = postcheck(
        {"status": "failed", "case": case, "observations": {}},
        catalog,
        options={"conflict_method": "mus", "timeout_ms": 3000, "search_timeout_s": 9},
    )
    assert report["conflict_method"] == "MUS_enumeration"
