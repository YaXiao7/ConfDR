"""Typed finite-domain FOL and integer-sum AST; compile expressions directly without mapping rule IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import z3

SORTS = ("Pod", "Node", "Claim", "Volume", "Resource", "Spread", "Driver")
SEMANTICS_VERSION = "scheduling-atoms-v1"


def semantics_version(profile: str | None) -> str:
    """Version observation semantics separately from the legacy scheduling profile."""
    return "scheduling-atoms-v2" if profile == "scheduling-v2" else SEMANTICS_VERSION
# Argument types, return types and predicate meanings form the generator-visible vocabulary; no reference formulas are supplied.
BASE_SIGNATURES = {
    "volume_limit_applies": (
        ("Node", "Driver"),
        "Bool",
        "Observed finite CSI driver volume limit on node; failed collection is unknown, known absent limit is false",
    ),
    "volume_limit": (
        ("Node", "Driver"),
        "Int",
        "Maximum unique CSI volumes allowed on node for driver; equality is allowed",
    ),
    "volume_count_before": (
        ("Pod", "Node", "Driver"),
        "Int",
        "Unique volumes of driver reserved on node by background and earlier assigned targets before this Pod; symbolic count",
    ),
    "volume_count_new": (
        ("Pod", "Node", "Driver"),
        "Int",
        "Additional unique volumes of driver needed by this Pod on node, excluding already reserved identities; symbolic count",
    ),
    "assign": (("Pod", "Node"), "Bool", "Target Pod assigned to a node; Boolean decision"),
    "bind": (("Claim", "Volume"), "Bool", "PVC assigned to an existing PV; Boolean decision"),
    "dynamic": (("Claim",), "Bool", "PVC selects dynamic provisioning; Boolean decision"),
    "uses_scheduler": (("Pod",), "Bool", "Pod does not explicitly set nodeName"),
    "node_name_ok": (("Pod", "Node"), "Bool", "Explicit nodeName is absent or equals this node"),
    "selector_ok": (("Pod", "Node"), "Bool", "All nodeSelector entries match the observed node labels"),
    "tolerates": (
        ("Pod", "Node"),
        "Bool",
        "Legacy hard-taint check; nodeName skips NoSchedule but not NoExecute",
    ),
    "node_affinity_ok": (("Pod", "Node"), "Bool", "Required node affinity matches this node"),
    "pod_affinity_ok": (("Pod", "Node"), "Bool", "Required Pod affinity holds at this Pod's scheduling step"),
    "pod_anti_affinity_ok": (
        ("Pod", "Node"),
        "Bool",
        "Incoming and existing required anti-affinity permit this scheduling step",
    ),
    "cpu_request": (("Pod",), "Int", "Effective target Pod CPU request in millicores"),
    "memory_request": (("Pod",), "Int", "Effective target Pod memory request in bytes"),
    "free_cpu": (("Node",), "Int", "Remaining node CPU request budget in millicores"),
    "free_memory": (("Node",), "Int", "Remaining node memory request budget in bytes"),
    "pod_slots": (("Node",), "Int", "Remaining node Pod slots"),
    "host_ports_free": (
        ("Pod", "Node"),
        "Bool",
        "Host port requests do not conflict with background allocations",
    ),
    "ports_disjoint": (("Pod", "Pod"), "Bool", "Two Pods have no conflicting host port reservations"),
    "node_ready": (("Node",), "Bool", "Observed Ready condition is true"),
    "node_schedulable": (("Node",), "Bool", "Observed node is not cordoned"),
    "claim_exists": (
        ("Claim",),
        "Bool",
        "Referenced PVC exists in the Pod namespace; absent inventory is unknown",
    ),
    "uses": (("Pod", "Claim"), "Bool", "Target Pod references this PVC"),
    "class_ok": (("Claim", "Volume"), "Bool", "Effective PVC storage class matches the PV class"),
    "access_ok": (("Claim", "Volume"), "Bool", "Requested access modes are a subset of PV access modes"),
    "mode_ok": (("Claim", "Volume"), "Bool", "PVC and PV volume modes agree"),
    "capacity_ok": (("Claim", "Volume"), "Bool", "PV capacity covers PVC storage request"),
    "volume_available": (
        ("Claim", "Volume"),
        "Bool",
        "PV is available to this PVC, respecting claimRef and binding identity",
    ),
    "has_fixed_volume": (("Claim",), "Bool", "PVC names or is already bound to a PV"),
    "fixed_to": (("Claim", "Volume"), "Bool", "PVC boundPV or volumeName identifies this PV"),
    "dynamic_available": (
        ("Claim",),
        "Bool",
        "Dynamic configuration prerequisites hold; not proof of backend health",
    ),
    "volume_node_ok": (("Volume", "Node"), "Bool", "Selected PV required node affinity permits this node"),
    "dynamic_node_ok": (
        ("Claim", "Node"),
        "Bool",
        "Selected dynamic path topology permits this node; unknown when unresolved",
    ),
    "schedule_rank": (("Pod",), "Int", "Target scheduling order; distinct bounded integer decision"),
    "scheduling_tolerates": (("Pod", "Node"), "Bool", "All NoSchedule and NoExecute taints are tolerated"),
    "is_gpu_resource": (("Resource",), "Bool", "Resource belongs to the configured GPU resource names"),
    "resource_request": (("Pod", "Resource"), "Int", "Effective extended resource request in integer units"),
    "resource_free": (
        ("Node", "Resource"),
        "Int",
        "Remaining extended resource budget after background Pods",
    ),
    "spread_applies": (("Spread", "Pod"), "Bool", "Hard spread constraint belongs to this target Pod"),
    "spread_has_key": (
        ("Spread", "Node"),
        "Bool",
        "Node has the topologyKey of this individual hard spread constraint",
    ),
    "spread_count": (("Spread", "Node"), "Int", "Matching Pods in this domain before the owner is scheduled"),
    "spread_self_match": (
        ("Spread",),
        "Int",
        "One if the incoming owner matches its spread selector, else zero",
    ),
    "spread_global_min": (("Spread",), "Int", "Minimum prior domain count, or zero below minDomains"),
    "spread_max_skew": (("Spread",), "Int", "Declared positive maxSkew"),
    "fixed_volume_exists": (
        ("Claim",),
        "Bool",
        "PV identified by boundPV (existing binding) or volumeName exists; true if neither is set; missing required PV inventory is unknown",
    ),
    "storage_class_exists": (("Claim",), "Bool", "Effective StorageClass resolves to an observed object"),
    "dynamic_eligible": (
        ("Claim",),
        "Bool",
        "PVC may select dynamic supply: no fixed PV or selector and a class intent",
    ),
    "provisioner_configured": (("Claim",), "Bool", "Class has a syntactically valid non-no-provisioner name"),
    "provisioner_parameters_valid": (
        ("Claim",),
        "Bool",
        "Parameters pass a matching configured driver contract; otherwise unknown",
    ),
    "pv_selector_ok": (
        ("Claim", "Volume"),
        "Bool",
        "PVC selector matches PV labels when selection is applicable",
    ),
}
SIGNATURES = dict(BASE_SIGNATURES)
DECISIONS = {"assign", "bind", "dynamic", "schedule_rank"}


def load_facts(world, facts: list[dict], *, extra_only: bool = False) -> None:
    """Load typed observations without requiring Python code for new predicates; never treat missing facts as false."""
    if not isinstance(facts, list) or len(facts) > 100000:
        raise ValueError("facts must be a bounded list")
    seen = set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"predicate", "arguments", "value"}:
            raise ValueError("Fact fields must be predicate/arguments/value")
        name, args, value = fact["predicate"], fact["arguments"], fact["value"]
        if name not in SIGNATURES or name in DECISIONS or extra_only and name in BASE_SIGNATURES:
            raise ValueError(f"Facts must not override base interpreters or decision variables: {name}")
        sorts, returns, _ = SIGNATURES[name]
        if not isinstance(args, list) or len(args) != len(sorts):
            raise ValueError("Fact argument count does not match")
        if any(not isinstance(x, str) or x not in world.domains[s] for x, s in zip(args, sorts, strict=True)):
            raise ValueError("Fact references an entity outside the domain")
        if type(value) is not (bool if returns == "Bool" else int):
            raise ValueError("Fact value type does not match")
        key = (name, *args)
        if key in seen:
            raise ValueError("Duplicate fact")
        seen.add(key)
        world.values[key] = value


def configure_vocabulary(extra: list[dict] | None = None) -> None:
    """Load new predicates from the taxonomy; base Kubernetes predicates cannot be redefined."""
    global SIGNATURES
    SIGNATURES = dict(BASE_SIGNATURES)
    for item in extra or []:
        if not isinstance(item, dict) or set(item) != {"name", "arguments", "returns", "meaning"}:
            raise ValueError("taxonomy.predicates must contain name, arguments, returns and meaning")
        name, arguments, returns = item["name"], item["arguments"], item["returns"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or not isinstance(arguments, list)
            or any(x not in SORTS for x in arguments)
            or returns not in {"Bool", "Int"}
            or not isinstance(item["meaning"], str)
            or not item["meaning"].strip()
        ):
            raise ValueError(f"Invalid predicate definition in taxonomy.predicates: {name!r}")
        if name in BASE_SIGNATURES:
            raise ValueError(f"Base predicates must not be overridden: {name}")
        if name in SIGNATURES:
            raise ValueError(f"Duplicate predicate: {name}")
        SIGNATURES[name] = (tuple(arguments), returns, item["meaning"])


def vocabulary() -> list[dict]:
    return [
        {"name": k, "arguments": list(v[0]), "returns": v[1], "meaning": v[2]} for k, v in SIGNATURES.items()
    ]


def expression_schema() -> dict:
    def obj(properties: dict) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }

    ref = {"$ref": "#/$defs/expression"}
    variable = {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}

    def application(field, names, minimum, maximum=None):
        args = {"type": "array", "items": ref, "minItems": minimum}
        if maximum is not None:
            args["maxItems"] = maximum
        return obj({field: {"type": "string", "enum": names}, "args": args})

    return {
        "anyOf": [
            obj({"var": variable}),
            obj({"int": {"type": "integer"}}),
            obj({"bool": {"type": "boolean"}}),
            *[application("call", [name], len(spec[0]), len(spec[0])) for name, spec in SIGNATURES.items()],
            application("op", ["and", "or"], 1),
            application("op", ["not"], 1, 1),
            application("op", ["implies", "eq", "ne", "le", "lt", "ge", "gt", "add", "sub"], 2, 2),
            application("op", ["ite"], 3, 3),
            obj(
                {
                    "quantifier": {"type": "string", "enum": ["forall", "exists", "sum"]},
                    "variable": variable,
                    "sort": {"type": "string", "enum": list(SORTS)},
                    "body": ref,
                }
            ),
        ]
    }


def typecheck(expr: dict, expected: str = "Bool") -> None:
    budget = [0]

    def visit(node: Any, env: dict, depth: int, path: str = "$") -> str:
        def failure(message):
            return ValueError(f"{message} at {path}; bound variables: {env}")

        budget[0] += 1
        if depth > 40 or budget[0] > 2000:
            raise failure("Expression exceeds depth/node limits")
        if not isinstance(node, dict):
            raise failure("AST nodes must be objects")
        keys = set(node)
        if keys == {"var"}:
            if node["var"] not in env:
                raise failure(f"Free variable: {node['var']}")
            return env[node["var"]]
        if keys == {"int"} and type(node["int"]) is int:
            return "Int"
        if keys == {"bool"} and type(node["bool"]) is bool:
            return "Bool"
        if keys == {"quantifier", "variable", "sort", "body"}:
            name, sort, quantifier = node["variable"], node["sort"], node["quantifier"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise failure("Invalid variable name")
            if name in env or sort not in SORTS or quantifier not in {"forall", "exists", "sum"}:
                raise failure("Invalid variable shadowing, type or quantifier")
            body_type = visit(node["body"], {**env, name: sort}, depth + 1, path + "/body")
            result_type = "Int" if quantifier == "sum" else "Bool"
            if body_type != result_type:
                raise failure("Quantifier body type does not match")
            return result_type
        if keys not in ({"call", "args"}, {"op", "args"}) or not isinstance(node["args"], list):
            raise failure("Unknown AST node or field")
        args = [visit(x, env, depth + 1, f"{path}/args/{i}") for i, x in enumerate(node["args"])]
        if "call" in node:
            spec = SIGNATURES.get(node["call"])
            if not spec or args != list(spec[0]):
                raise failure(f"Invalid predicate or argument types: {node['call']} {args}")
            return spec[1]
        op = node["op"]
        if op in {"and", "or"} and len(args) >= 1 and all(t == "Bool" for t in args):
            return "Bool"
        if op == "not" and args == ["Bool"]:
            return "Bool"
        if op == "implies" and args == ["Bool", "Bool"]:
            return "Bool"
        if op in {"eq", "ne"} and len(args) == 2 and args[0] == args[1]:
            return "Bool"
        if op in {"le", "lt", "ge", "gt"} and args == ["Int", "Int"]:
            return "Bool"
        if op in {"add", "sub"} and args == ["Int", "Int"]:
            return "Int"
        if op == "ite" and len(args) == 3 and args[0] == "Bool" and args[1] == args[2]:
            if args[1] in {"Bool", "Int"}:
                return args[1]
        raise failure(f"Invalid operator/argument types: {op} {args}")

    if visit(expr, {}, 0) != expected:
        raise ValueError(f"Expression result must be {expected} at $")


@dataclass(frozen=True)
class Entity:
    sort: str
    name: str


@dataclass
class World:
    domains: dict[str, list[str]]
    values: dict[tuple, Any] = field(default_factory=dict)
    symbolic: bool = False
    missing: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    symbols: dict[tuple, Any] = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    expanded: int = 0
    max_expanded: int = 200_000
    domain_missing: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self):
        # Support legacy four-entity-domain snapshots; new domains default to empty without fabricating resource facts.
        self.domains = {**{s: [] for s in SORTS}, **self.domains}

    def call(self, name: str, args: list[Entity]):
        key = (name, *(x.name for x in args))
        if not self.symbolic and name not in DECISIONS:
            self.reads.add("/".join(key))
        if key in self.values and self.values[key] is not None:
            value = self.values[key]
            if type(value) is bool:
                return z3.BoolVal(value)
            if type(value) is int:
                return z3.IntVal(value)
            return value
        if key not in self.symbols:
            symbol = f"V{len(self.symbols)}_{name}"
            self.symbols[key] = z3.Bool(symbol) if SIGNATURES[name][1] == "Bool" else z3.Int(symbol)
            if name in {"cpu_request", "memory_request"}:
                self.assumptions.append(self.symbols[key] >= 0)
        if not self.symbolic and name not in DECISIONS:
            self.missing.add("/".join(key))
        return self.symbols[key]

    def assignment_axioms(self) -> list:
        return [
            z3.PbEq(
                [
                    (self.call("assign", [Entity("Pod", p), Entity("Node", n)]), 1)
                    for n in self.domains["Node"]
                ],
                1,
            )
            if self.domains["Node"]
            else z3.BoolVal(False)
            for p in self.domains["Pod"]
        ]

    def support_axioms(self, profile: str | None = None) -> list:
        if profile not in {"scheduling-v1", "scheduling-v2"}:
            return []
        pods, claims, volumes = (self.domains[s] for s in ("Pod", "Claim", "Volume"))
        ranks = [self.call("schedule_rank", [Entity("Pod", p)]) for p in pods]
        axioms = [z3.And(r >= 0, r < len(pods)) for r in ranks]
        if ranks:
            axioms.append(z3.Distinct(ranks))
        for c in claims:
            choices = [self.call("bind", [Entity("Claim", c), Entity("Volume", v)]) for v in volumes]
            choices.append(self.call("dynamic", [Entity("Claim", c)]))
            axioms.append(z3.PbLe([(choice, 1) for choice in choices], 1))
        for v in volumes:
            choices = [self.call("bind", [Entity("Claim", c), Entity("Volume", v)]) for c in claims]
            if choices:
                axioms.append(z3.PbLe([(choice, 1) for choice in choices], 1))
        return axioms


def compile_expr(expr: dict, world: World, env: dict | None = None):
    world.expanded += 1
    if world.expanded > world.max_expanded:
        raise ValueError("Finite-domain expansion exceeds the budget")
    env = env or {}
    if "var" in expr:
        return env[expr["var"]]
    if "int" in expr:
        return z3.IntVal(expr["int"])
    if "bool" in expr:
        return z3.BoolVal(expr["bool"])
    if "quantifier" in expr:
        items = [
            compile_expr(expr["body"], world, {**env, expr["variable"]: Entity(expr["sort"], name)})
            for name in world.domains[expr["sort"]]
        ]
        if expr["quantifier"] == "forall":
            return z3.And(items)
        if expr["quantifier"] == "exists":
            return z3.Or(items)
        return z3.Sum(items) if items else z3.IntVal(0)
    # Do not read missing facts for inapplicable conditions; for example, do not check provisioner parameters for a nonexistent PVC.
    if expr.get("op") == "implies":
        antecedent = z3.simplify(compile_expr(expr["args"][0], world, env))
        if z3.is_false(antecedent):
            return z3.BoolVal(True)
        return z3.Implies(antecedent, compile_expr(expr["args"][1], world, env))
    if expr.get("op") == "ite":
        condition = z3.simplify(compile_expr(expr["args"][0], world, env))
        if z3.is_true(condition) or z3.is_false(condition):
            return compile_expr(expr["args"][1 if z3.is_true(condition) else 2], world, env)
        return z3.If(
            condition, compile_expr(expr["args"][1], world, env), compile_expr(expr["args"][2], world, env)
        )
    args = [compile_expr(x, world, env) for x in expr["args"]]
    if "call" in expr:
        return world.call(expr["call"], args)
    op = expr["op"]
    if op in {"eq", "ne"} and isinstance(args[0], Entity):
        equal = args[0] == args[1]
        return z3.BoolVal(equal if op == "eq" else not equal)
    operations = {
        "and": lambda: z3.And(args),
        "or": lambda: z3.Or(args),
        "not": lambda: z3.Not(args[0]),
        "implies": lambda: z3.Implies(*args),
        "eq": lambda: args[0] == args[1],
        "ne": lambda: args[0] != args[1],
        "le": lambda: args[0] <= args[1],
        "lt": lambda: args[0] < args[1],
        "ge": lambda: args[0] >= args[1],
        "gt": lambda: args[0] > args[1],
        "add": lambda: args[0] + args[1],
        "sub": lambda: args[0] - args[1],
        "ite": lambda: z3.If(*args),
    }
    return operations[op]()


def render(expr: dict) -> str:
    """Render a readable expression from the AST; the rendered text is not executed."""
    if "var" in expr:
        return expr["var"]
    if "int" in expr:
        return str(expr["int"])
    if "bool" in expr:
        return str(expr["bool"]).lower()
    if "quantifier" in expr:
        symbol = {"forall": "∀", "exists": "∃", "sum": "Σ"}[expr["quantifier"]]
        return f"{symbol}{expr['variable']}:{expr['sort']}.({render(expr['body'])})"
    args = [render(x) for x in expr["args"]]
    if "call" in expr:
        return f"{expr['call']}({', '.join(args)})"
    return f"{expr['op']}({', '.join(args)})"
