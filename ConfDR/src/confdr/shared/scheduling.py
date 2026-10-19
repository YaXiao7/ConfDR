"""Scheduling-step facts for required Pod affinity and hard topology spread."""

import z3

from confdr.shared.logic import Entity


def before(world, first, second):
    return world.call("schedule_rank", [Entity("Pod", first)]) < world.call(
        "schedule_rank", [Entity("Pod", second)]
    )


def placed(world, pod, node):
    return world.call("assign", [Entity("Pod", pod), Entity("Node", node)])


def term_matches(term, owner, other, namespace_labels):
    from confdr.shared.kubernetes import selector_matches

    # Advanced affinity fields not enabled in the 1.30 configuration must not be silently ignored.
    if term.get("matchLabelKeys") or term.get("mismatchLabelKeys"):
        return None
    namespaces = set(term.get("namespaces") or [])
    ns_selector = term.get("namespaceSelector")
    if ns_selector is not None:
        if namespace_labels is None:
            return None
        namespaces |= {n for n, labels in namespace_labels.items() if selector_matches(ns_selector, labels)}
    elif not namespaces:
        namespaces = {owner.get("namespace") or "default"}
    if (other.get("namespace") or "default") not in namespaces:
        return False
    if "labels" not in other:
        return None
    # A nil Kubernetes LabelSelector matches no objects; an empty object matches all labels.
    return term.get("labelSelector") is not None and selector_matches(term["labelSelector"], other["labels"])


def ordered_affinity(world, case, p, n, mode):
    cluster, pods = case["ClusterState"], case["WaitingDeployPods"]
    nodes, pod = cluster["Nodes"], pods[p]
    existing = case.get("ExistingPods", cluster.get("ExistingPods"))
    ns_labels = cluster.get("NamespaceLabels")
    terms = ((pod.get("affinity") or {}).get(mode) or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    reverse = []
    if mode == "podAntiAffinity":
        if existing is None:
            return None  # Even without its own anti-affinity, a Pod may be constrained by existing Pod anti-affinity.
        reverse = [(None, item) for item in existing.values()] + [
            (q, item) for q, item in pods.items() if q != p
        ]
    if terms and (existing is None or "labels" not in nodes[n]):
        return None
    conditions, all_global, self_matches = [], [], []

    def candidate_matches(term, item):
        checks = [term_matches(t, pod, item, ns_labels) for t in (terms if mode == "podAffinity" else [term])]
        if False in checks:
            return False
        return None if None in checks else True

    for term in terms:
        key = term.get("topologyKey")
        target = nodes[n].get("labels", {}).get(key)
        if target is None:
            # Incoming anti-affinity does not treat a missing topologyKey alone as a conflict.
            conditions.append(z3.BoolVal(mode == "podAntiAffinity"))
            self_matches.append(False)
            continue
        local, anywhere = [], []
        for item in (existing or {}).values():
            match = candidate_matches(term, item)
            if match is None:
                return None
            if match:
                node = nodes.get(item.get("nodeName"))
                if node is None or "labels" not in node:
                    return None
                if key in node["labels"]:
                    anywhere.append(z3.BoolVal(True))
                if node["labels"].get(key) == target:
                    local.append(z3.BoolVal(True))
        for q, item in pods.items():
            if q == p:
                continue
            match = candidate_matches(term, item)
            if match is None:
                return None
            if match:
                for m, node in nodes.items():
                    if "labels" not in node:
                        return None
                    active = z3.And(before(world, q, p), placed(world, q, m))
                    if key in node["labels"]:
                        anywhere.append(active)
                    if node["labels"].get(key) == target:
                        local.append(active)
        found = z3.Or(local)
        conditions.append(z3.Not(found) if mode == "podAntiAffinity" else found)
        all_global.extend(anywhere)
        match_self = term_matches(term, pod, pod, ns_labels)
        if match_self is None:
            return None
        self_matches.append(match_self)
    if mode == "podAffinity":
        if not terms:
            return z3.BoolVal(True)
        return z3.Or(z3.And(conditions), z3.And(z3.Not(z3.Or(all_global)), *self_matches))
    for q, owner in reverse:
        if "affinity" not in owner:
            return None
        owner_terms = ((owner.get("affinity") or {}).get("podAntiAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution", []
        )
        for term in owner_terms:
            match = term_matches(term, owner, pod, ns_labels)
            if match is None:
                return None
            if not match:
                continue
            key = term.get("topologyKey")
            if "labels" not in nodes[n]:
                return None
            target = nodes[n]["labels"].get(key)
            if target is None:
                continue
            if q is None:
                node = nodes.get(owner.get("nodeName"))
                if node is None or "labels" not in node:
                    return None
                conditions.append(z3.BoolVal(node["labels"].get(key) != target))
            else:
                for m, node in nodes.items():
                    if "labels" not in node:
                        return None
                    if node["labels"].get(key) == target:
                        conditions.append(z3.Not(z3.And(before(world, q, p), placed(world, q, m))))
    return z3.And(conditions)


def populate_spread(world, case):
    from confdr.shared.kubernetes import node_terms, selector_matches, tolerates

    pods, cluster = case["WaitingDeployPods"], case["ClusterState"]
    nodes = cluster["Nodes"]
    existing = case.get("ExistingPods", cluster.get("ExistingPods"))
    world.domain_missing["Spread"] = [
        f"WaitingDeployPods/{p}/topologySpreadConstraints"
        for p, pod in pods.items()
        if "topologySpreadConstraints" not in pod
    ]
    constraints = {
        f"{p}/spread/{i}": (p, term)
        for p, pod in pods.items()
        for i, term in enumerate(pod.get("topologySpreadConstraints") or [])
        if term.get("whenUnsatisfiable") == "DoNotSchedule"
    }
    world.domains["Spread"] = list(constraints)
    for t, (p, term) in constraints.items():
        pod = pods[p]
        for q in pods:
            world.values["spread_applies", t, q] = p == q
        max_skew, min_domains = term.get("maxSkew"), term.get("minDomains", 1)
        if type(max_skew) is not int or max_skew < 1 or type(min_domains) is not int or min_domains < 1:
            raise ValueError("Hard topology spread requires positive maxSkew and minDomains")
        world.values["spread_max_skew", t] = max_skew
        selector = term.get("labelSelector")
        selector = (
            None if selector is None else {**selector, "matchLabels": dict(selector.get("matchLabels", {}))}
        )
        if selector is not None:
            for key in term.get("matchLabelKeys") or []:
                if key in pod.get("labels", {}):
                    selector.setdefault("matchExpressions", [])
                    selector["matchExpressions"] = [
                        *selector["matchExpressions"],
                        {"key": key, "operator": "In", "values": [pod["labels"][key]]},
                    ]

        def matches(item, selector=selector, pod=pod):
            return (
                selector is not None
                and (item.get("namespace") or "default") == (pod.get("namespace") or "default")
                and selector_matches(selector, item.get("labels", {}))
            )

        world.values["spread_self_match", t] = int(matches(pod))
        all_keys = {x.get("topologyKey") for _, (owner, x) in constraints.items() if owner == p}
        affinity_policy = term.get("nodeAffinityPolicy", "Honor")
        taints_policy = term.get("nodeTaintsPolicy", "Ignore")
        if affinity_policy not in {"Honor", "Ignore"} or taints_policy not in {"Honor", "Ignore"}:
            raise ValueError("Invalid topology spread node inclusion policy")
        incomplete = existing is None or any("labels" not in node for node in nodes.values())
        incomplete |= taints_policy == "Honor" and any("taints" not in node for node in nodes.values())
        incomplete |= any("labels" not in item for item in (existing or {}).values())
        incomplete |= any(item.get("nodeName") not in nodes for item in (existing or {}).values())
        eligible = {}
        for n, node in nodes.items():
            labels = node.get("labels", {})
            has_keys = all(key in labels for key in all_keys)
            world.values["spread_has_key", t, n] = has_keys if "labels" in node else None
            ok = has_keys
            if affinity_policy == "Honor":
                required = ((pod.get("affinity") or {}).get("nodeAffinity") or {}).get(
                    "requiredDuringSchedulingIgnoredDuringExecution"
                )
                ok &= selector_matches({"matchLabels": pod.get("nodeSelector") or {}}, labels)
                ok &= required is None or node_terms(required.get("nodeSelectorTerms", []), n, labels)
            if taints_policy == "Honor":
                ok &= tolerates({**pod, "nodeName": None}, node)
            if ok:
                eligible[n] = labels[term["topologyKey"]]
        counts = {}
        for domain in set(eligible.values()):
            background = sum(
                matches(item) and eligible.get(item.get("nodeName")) == domain
                for item in (existing or {}).values()
            )
            additions = [
                z3.If(z3.And(before(world, q, p), placed(world, q, n)), 1, 0)
                for q, item in pods.items()
                if q != p and matches(item)
                for n, value in eligible.items()
                if value == domain
            ]
            counts[domain] = z3.IntVal(background) + (z3.Sum(additions) if additions else 0)
        minimum = z3.IntVal(0)
        if len(counts) >= min_domains and counts:
            values = list(counts.values())
            minimum = values[0]
            for value in values[1:]:
                minimum = z3.If(value < minimum, value, minimum)
        world.values["spread_global_min", t] = None if incomplete else minimum
        for n, node in nodes.items():
            domain = node.get("labels", {}).get(term.get("topologyKey"))
            world.values["spread_count", t, n] = None if incomplete else counts.get(domain, z3.IntVal(0))
