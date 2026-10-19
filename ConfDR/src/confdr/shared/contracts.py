"""Model-visible DSL and runtime contracts, shared by generation and verification.

These document the abstraction boundary, not a proof of the atom interpreters.
"""

from confdr.shared.logic import DECISIONS, vocabulary


def language_contract() -> dict:
    return {
        "variables": "var denotes a bound entity variable, never a predicate name or a Boolean. "
        "Bind every variable with forall/exists/sum; nested binders must have distinct names.",
        "calls": "Use call plus args for predicates. Match declared entity sorts and arity exactly.",
        "operators": {
            "and/or": "one or more Bool arguments -> Bool",
            "not": "exactly one Bool -> Bool",
            "implies": "exactly [Bool antecedent, Bool consequent] -> Bool; combine conditions with and",
            "eq/ne": "exactly two arguments of the same type -> Bool",
            "le/lt/ge/gt": "exactly [Int, Int] -> Bool",
            "add/sub": "exactly [Int, Int] -> Int; nest binary operations for longer sums",
            "ite": "exactly [Bool condition, T then, T else] -> T, with T either Bool or Int",
        },
        "quantifiers": "forall/exists have a Bool body and return Bool; sum has an Int body and returns Int. "
        "sum is a quantifier node, never an op. Each rule must return Bool.",
        "syntax_fragments": {
            "predicate_call": {"call": "is_gpu_resource", "args": [{"var": "r"}]},
            "conditional_integer": {"op": "ite", "args": [{"bool": True}, {"int": 1}, {"int": 0}]},
            "integer_sum": {"quantifier": "sum", "variable": "x", "sort": "Pod", "body": {"int": 1}},
        },
        "example_scope": "Fragments illustrate syntax, not category solutions. r must be bound by its caller.",
    }


def semantic_contract(profile: str | None) -> dict:
    return {
        "profile": profile,
        "domains": {
            "Pod": "Only target Pods, including replicas. Background Pods are handled by atom interpreters.",
            "Node": "Observed candidate nodes in the fixed snapshot.",
            "Claim": "Referenced PVC identities qualified as namespace/name, including missing references. "
            "uses(p,c) relates a target only to references in its own namespace.",
            "Volume": "Observed PV inventory; missing inventory is not a known empty domain.",
            "Resource": "Distinct extended-resource names, never interchangeable.",
            "Spread": "Individual DoNotSchedule spread constraints owned by target Pods.",
            "Driver": "CSI storage driver identities. Unresolved required identities remain explicit unknown entries, not an empty domain.",
        },
        "decisions": sorted(DECISIONS),
        "shared_axioms": ["Exactly one assign(p,n) per target Pod."]
        + (
            [
                "schedule_rank is a distinct integer in [0, number of target Pods).",
                "At most one static bind or dynamic path per PVC; at most one PVC per PV. "
                "Storage path existence is NOT a shared axiom; express it in the applicable category.",
            ]
            if profile in {"scheduling-v1", "scheduling-v2"}
            else []
        ),
        "observation_semantics": "Missing facts are unconstrained typed symbols, never false or zero. "
        "Runtime reports unknown for satisfiable but incomplete snapshots. Rules use two-valued logic; "
        "do not invent an unknown predicate or require evidence completeness in each rule.",
        "atom_contracts": {
            "volume_limit_applies/volume_limit": "CSI-only Kubernetes 1.30 scheduling limits from CSINode drivers.allocatable.count. "
            "A known absent count means no declared limit; failed or missing collection is unknown, never zero or infinity. "
            "In-tree migration and legacy Node attach limits require separate support; leave unknown when relevant.",
            "volume_count_before/volume_count_new": "Integer counts per incoming Pod, node and driver, deduplicated by "
            "driver and PV volumeHandle. Background reservations and earlier assign/schedule_rank targets contribute "
            "to before; new excludes identities already reserved. Bound PVCs use their observed PV. Unbound PVC "
            "paths depend on static bind or dynamic decisions; a dynamic identity uses namespace/PVC name. "
            "No selected path contributes zero here; path existence belongs to storage categories. Counts include "
            "concurrent targets without double-counting shared volumes. The category compares before+new with limit "
            "only for scheduler-managed assigned targets, applicable limits and positive new count. Reuse without "
            "new volumes passes even if a background reservation already exceeds the limit. Inline CSI ephemeral "
            "volumes are outside persistent-volume counting; unresolved generic ephemeral/migrated sources are unknown.",
            "cpu_request/memory_request": "Effective target request includes application containers, init "
            "container peaks, restartable init containers, applicable request defaults and Pod overhead. "
            "CPU uses millicores; memory uses bytes. This calculation is delegated to the interpreter.",
            "free_cpu/free_memory/pod_slots/resource_free": "Remaining allocatable budget after background "
            "request reservations/occupancy, not measured utilization. Targets are counted once by the rule. "
            "The supplied category determines which nodes are constrained; do not silently change its policy "
            "for negative budgets. Concerns about that policy are contract gaps, not missing arithmetic atoms.",
            "host_ports_free/ports_disjoint": "Compare protocol, hostPort and overlapping hostIP reservations "
            "using Kubernetes 1.30 application-container semantics. containerPort alone and Service ports "
            "are not reservations. Init-container host ports are unsupported, not silently included. "
            "Keep requested port facts when reconstructing candidates. Missing required inventory is unknown.",
            "pod_affinity_ok": "In scheduling-v1, the runtime expands this into a formula over assign and "
            "schedule_rank, checking required selectors, namespaces/namespaceSelector and topology against "
            "background and earlier target Pods. Includes the first self-affine Pod exception and excludes "
            "future cyclic witnesses. Preferred terms impose no hard constraint.",
            "pod_anti_affinity_ok": "In scheduling-v1, checks incoming and reverse required anti-affinity "
            "against background and earlier target Pods with namespace/topology scope.",
            "spread_count/spread_global_min": "Runtime expressions depend on placement and scheduling rank, "
            "with matching background and earlier targets, minDomains and inclusion policies. "
            "The incoming Pod contribution is spread_self_match.",
            "scheduling_tolerates": "All NoSchedule/NoExecute taints match key/operator/value/effect; "
            "PreferNoSchedule and eviction expiry are outside hard scheduling feasibility.",
            "fixed_volume_exists/fixed_to": "Use boundPV first, then volumeName, matching storage.py and "
            "kubernetes.py. A separate bound_to predicate is not needed for this representation.",
            **(
                {
                    "dynamic_eligible": "In scheduling-v2 only, an omitted/null PVC storageClassName "
                    "with no effective default and a completely observed empty PV inventory may "
                    "select the dynamic path so S2 attributes the missing default class. An explicit "
                    "empty string disables this path. Existing PV inventory retains the static path, "
                    "and missing inventory remains unknown. A named class without a StorageClass "
                    "but with an observed same-class PV also uses static S3 attribution."
                }
                if profile == "scheduling-v2"
                else {}
            ),
        },
        "verification_scope": "Bounded equivalence treats atoms as abstract functions. Scenarios explicitly "
        "supply non-decision fact values; they do not execute Kubernetes atom interpreters. Those need separate "
        "tests. Review composition relative to these contracts; do not demand their internal implementation "
        "in every rule or claim it has been proved. Other categories' constraints are outside this review.",
    }


def verification_base(taxonomy: dict, category: dict, knowledge: list[dict]) -> dict:
    return {
        "review_protocol": "ast-review-v2",
        "taxonomy": {**taxonomy, "categories": [category]},
        "retrieved_sources": [
            {**s, "categories": [category["id"]], "provenance_categories": s.get("categories", [])}
            for s in knowledge
            if category["id"] in s.get("categories", [])
        ],
        "vocabulary": vocabulary(),
        "language_contract": language_contract(),
        "semantic_contract": semantic_contract(taxonomy.get("semantics_profile")),
    }
