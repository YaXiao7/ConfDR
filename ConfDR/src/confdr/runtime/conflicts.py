"""Enumerate all minimal unsatisfiable subsets (MUSes).

Use a MARCO-style search map. Domain constraints remain unchanged throughout the search;
the map solver excludes only supersets of explored MUSes and subsets of maximal satisfiable subsets (MSSes),
so overlapping conflicts with shared constraints are not lost by removing the first core.
"""

from __future__ import annotations

import math
import time

import z3


class IncompleteSearch(Exception):
    """The solver returned UNKNOWN or the configured total search time was exhausted."""


def enumerate_conflicts(
    formulas: list,
    base: list | tuple = (),
    *,
    protected=(),
    timeout_ms: int = 5000,
    search_timeout_s: float = 0,
) -> dict:
    """Enumerate subset-minimal conflicts relative to the fixed background.

    Protected formula indices and ``base`` are always active. ``cores`` contains
    only verified minimal subsets of the remaining indices; ``core_contexts``
    records supporting protected labels. Unfinished shrink results are retained
    separately as ``unminimized_cores``, never described as MUSes.
    """
    if (
        type(timeout_ms) is not int
        or timeout_ms <= 0
        or not math.isfinite(search_timeout_s)
        or search_timeout_s < 0
    ):
        raise ValueError("Solver timeout_ms must be positive and search_timeout_s must be nonnegative")
    fixed = set(protected)
    if any(type(i) is not int or not 0 <= i < len(formulas) for i in fixed):
        raise ValueError("Protected indices must reference existing formulas")
    universe = set(range(len(formulas))) - fixed
    started = time.monotonic()
    deadline = started + search_timeout_s if search_timeout_s else None
    subset_solver, map_solver = z3.Solver(), z3.Solver()
    gates = [z3.FreshBool("constraint") for _ in formulas]
    subset_solver.add(*base)
    subset_solver.add(*[z3.Implies(gate, formula) for gate, formula in zip(gates, formulas, strict=True)])
    checks, muses, contexts = 0, [], []
    full_status, base_inconsistent, witness = "unknown", False, None
    initial_core, pending = [], None
    first_check_seconds = None

    def check(solver, assumptions=()):
        nonlocal checks
        remaining = timeout_ms
        if deadline is not None:
            remaining = min(remaining, int((deadline - time.monotonic()) * 1000))
            if remaining <= 0:
                raise IncompleteSearch("search_timeout")
        solver.set(timeout=remaining)
        status = solver.check(*assumptions)
        checks += 1
        if status == z3.unknown:
            raise IncompleteSearch(solver.reason_unknown())
        return status

    def check_subset(indices):
        active = set(indices) | fixed
        return check(subset_solver, [g if i in active else z3.Not(g) for i, g in enumerate(gates)])

    def proof_core():
        selected = set(subset_solver.unsat_core())
        return [i for i, gate in enumerate(gates) if gate in selected]

    def report(complete: bool, reason: str) -> dict:
        return {
            "algorithm": "MUS_enumeration",
            "solver_status": full_status,
            # Subset SAT is a search step, never a new verdict on the original input.
            "terminal_status": full_status,
            "cores": muses,
            "core_contexts": contexts,
            "unminimized_cores": [pending] if pending is not None else [],
            "minimality_proved": bool(muses),
            "minimality_scope": "category_constraints_relative_to_fixed_background",
            "initial_core": initial_core,
            "first_check_seconds": first_check_seconds,
            "complete": complete,
            "exhaustive": complete and not base_inconsistent,
            "base_inconsistent": base_inconsistent,
            "reason": reason,
            "checks": checks,
            "seconds": round(time.monotonic() - started, 6),
            "model": witness,
        }

    try:
        # The first reported check always uses the entire original constraint set.
        try:
            full_status = str(check_subset(universe))
        finally:
            first_check_seconds = round(time.monotonic() - started, 6)
        if full_status == "sat":
            witness = subset_solver.model()
            return report(True, "all_constraints_satisfiable")
        initial_core = proof_core()
        pending = initial_core
        if check_subset(set()) == z3.unsat:
            base_inconsistent = True
            pending = proof_core()
            return report(False, "protected_constraints_inconsistent")
        seed = universe
        while True:
            if check_subset(seed) == z3.unsat:
                pending = proof_core()
                core = set(pending) - fixed
                for index in sorted(core):
                    if check_subset(core - {index}) == z3.unsat:
                        core.remove(index)
                        pending = proof_core()
                # Every deletion was checked. This is subset minimal, not minimum cardinality.
                muses.append(sorted(core))
                contexts.append(sorted(set(pending) & fixed))
                pending = None
                map_solver.add(z3.Or([z3.Not(gates[index]) for index in core]))
            else:
                maximal = set(seed)
                for index in sorted(universe - maximal):
                    if check_subset(maximal | {index}) == z3.sat:
                        maximal.add(index)
                map_solver.add(z3.Or([gates[index] for index in universe - maximal]))
            if check(map_solver) == z3.unsat:
                return report(True, "all_minimal_conflicts_enumerated")
            model = map_solver.model()
            seed = {
                index for index in universe if z3.is_true(model.eval(gates[index], model_completion=True))
            }
    except IncompleteSearch as exc:
        return report(False, str(exc))


def skip_conflicts(
    formulas, base=(), *, protected=(), timeout_ms=5000, search_timeout_s=0, on_round=None
):
    """Skip concrete core instances; keep shared axioms active on every check.

    Continue until the filtered constraints are SAT, Z3 is UNKNOWN, or only
    inconsistent protected constraints remain. Each UNSAT step removes at least
    one active unprotected constraint, so no round limit is needed. Filtered SAT
    completes this traversal, not an exhaustive MUS search.
    """
    if (
        type(timeout_ms) is not int
        or timeout_ms <= 0
        or not math.isfinite(search_timeout_s)
        or search_timeout_s < 0
    ):
        raise ValueError("Invalid detection timeout")
    started = time.monotonic()
    solver = z3.Solver()
    gates = [z3.FreshBool("skip_constraint") for _ in formulas]
    solver.add(*base)
    solver.add(*[z3.Implies(g, f) for g, f in zip(gates, formulas, strict=True)])
    active, protected = set(range(len(formulas))), set(protected)
    rounds, cores, initial = [], [], []
    original, terminal, reason, witness = "not_run", "not_run", "not_run", None
    complete = False
    first_check_seconds = None
    index = 0
    while True:
        remaining = timeout_ms
        if search_timeout_s:
            remaining = min(remaining, int((search_timeout_s - time.monotonic() + started) * 1000))
            if remaining <= 0:
                reason = "search_timeout"
                break
        solver.set(timeout=remaining)
        # Explicitly disable skipped constraints, keeping the reduced problem unambiguous.
        assumptions = [g if i in active else z3.Not(g) for i, g in enumerate(gates)]
        terminal = str(solver.check(*assumptions))
        if index == 0:
            original = terminal
            first_check_seconds = round(time.monotonic() - started, 6)
        core = []
        if terminal == "unsat":
            core_gates = set(solver.unsat_core())
            core = [i for i in sorted(active) if gates[i] in core_gates]
            cores.append(core)
            if index == 0:
                initial = core
        removable = set(core) - protected
        row = {
            "round": index + 1,
            "status": terminal,
            "core": core,
            "skipped_before": sorted(set(range(len(formulas))) - active),
            "removed": sorted(removable),
        }
        rounds.append(row)
        if on_round:
            on_round(row)
        if terminal == "sat":
            complete, reason = (
                True,
                "filtered_constraints_satisfiable" if cores else "all_constraints_satisfiable",
            )
            if original == "sat":
                witness = solver.model()
            break
        if terminal == "unknown":
            reason = solver.reason_unknown()
            break
        if not removable:
            reason = "protected_constraints_inconsistent"
            break
        active -= removable
        index += 1
    return {
        "solver_status": original,
        "terminal_status": terminal,
        "algorithm": "label_skipping",
        "initial_core": initial,
        "first_check_seconds": first_check_seconds,
        "cores": cores,
        "rounds": rounds,
        "complete": complete,
        "exhaustive": False,
        "base_inconsistent": reason == "protected_constraints_inconsistent",
        "reason": reason,
        "checks": len(rounds),
        "seconds": round(time.monotonic() - started, 6),
        "model": witness,
    }
