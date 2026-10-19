# First-order constraints

Generated from rules.yaml. Edit formula in rules.yaml and run validate-rules to revalidate.

Validation establishes bounded reference agreement and scenario checks, not unbounded correctness.
These are candidate formulas. Consult verification.json category_status before using a category.

## R1/R1.cpu-request-budget: Assigned target CPU requests fit each node budget

Category: R1

- R1.budget: When For each node, Sum cpu_request of targets selected by assign must be <= free_cpu. Equality passes; requests use millicores.

```text
∀n:Node.(le(Σp:Pod.(ite(assign(p, n), cpu_request(p), 0)), free_cpu(n)))
```

## R2/R2.memory-assignment-budget: Assigned target memory fits each node budget

Category: R2

- R2.budget: When For each node, Sum memory_request of targets selected by assign must be <= free_memory. Equality passes; requests use bytes.

```text
∀n:Node.(le(Σp:Pod.(ite(assign(p, n), memory_request(p), 0)), free_memory(n)))
```

## R3/R3.gpu-resource-budget: GPU resource request budget per node and resource

Category: R3

- R3.budget: When For each node and Resource where is_gpu_resource is true, Sum resource_request of assigned targets must be <= resource_free for that same resource name.

```text
∀n:Node.(∀r:Resource.(implies(is_gpu_resource(r), le(Σp:Pod.(ite(assign(p, n), resource_request(p, r), 0)), resource_free(n, r)))))
```

## R4/R4.slots.assigned-target-pods-within-node-slots: Assigned target Pods do not exceed remaining node Pod slots

Category: R4

- R4.slots: When For each node, Number of assigned target Pods must be <= pod_slots. Equality passes.

```text
∀n:Node.(le(Σp:Pod.(ite(assign(p, n), 1, 0)), pod_slots(n)))
```

## A1/A1.tolerations.scheduler-assignment: Scheduler-assigned Pods tolerate hard taints

Category: A1

- A1.tolerations: When uses_scheduler(p) and assign(p,n), Require scheduling_tolerates(p,n). A direct nodeName target does not activate this requirement.

```text
∀p:Pod.(∀n:Node.(implies(and(uses_scheduler(p), assign(p, n)), scheduling_tolerates(p, n))))
```

## P1/P1-selector-on-assigned-scheduler-node: Scheduled assignments satisfy node selectors

Category: P1

- P1.selector: When uses_scheduler(p) and assign(p,n), Require selector_ok(p,n).

```text
∀p:Pod.(∀n:Node.(implies(and(uses_scheduler(p), assign(p, n)), selector_ok(p, n))))
```

## P1/P1-required-node-affinity-on-assigned-scheduler-node: Scheduled assignments satisfy required node affinity

Category: P1

- P1.affinity: When uses_scheduler(p) and assign(p,n), Require node_affinity_ok(p,n), conjunctively with node selection.

```text
∀p:Pod.(∀n:Node.(implies(and(uses_scheduler(p), assign(p, n)), node_affinity_ok(p, n))))
```

## P2/P2.required-pod-affinity-on-assignment: Scheduler-assigned Pods satisfy required Pod affinity

Category: P2

- P2.affinity: When uses_scheduler(p) and assign(p,n), Require pod_affinity_ok(p,n); atom semantics supply scheduling order and the self-affinity exception.

```text
∀p:Pod.(∀n:Node.(implies(and(uses_scheduler(p), assign(p, n)), pod_affinity_ok(p, n))))
```

## P3/P3-required-pod-anti-affinity: Scheduler-assigned Pods satisfy required pod anti-affinity

Category: P3

- P3.anti-affinity: When uses_scheduler(p) and assign(p,n), Require pod_anti_affinity_ok(p,n); atom semantics supply incoming and reverse terms.

```text
∀p:Pod.(∀n:Node.(implies(and(uses_scheduler(p), assign(p, n)), pod_anti_affinity_ok(p, n))))
```

## P4/topology-key-for-assigned-scheduler-pod: Assigned scheduler-managed Pods require spread topology keys

Category: P4

- P4.topology-key: When uses_scheduler(p) and spread_applies(s,p) and assign(p,n), Require spread_has_key(s,n). spread_applies links a hard constraint to its owner; uses_scheduler guards scheduler filtering.

```text
∀p:Pod.(∀s:Spread.(∀n:Node.(implies(and(uses_scheduler(p), spread_applies(s, p), assign(p, n)), spread_has_key(s, n)))))
```

## P4/skew-bound-for-assigned-scheduler-pod: Assigned scheduler-managed Pods satisfy hard spread skew

Category: P4

- P4.skew: When uses_scheduler(p) and spread_applies(s,p) and assign(p,n), Require spread_count(s,n) + spread_self_match(s) - spread_global_min(s) <= spread_max_skew(s).

```text
∀p:Pod.(∀s:Spread.(∀n:Node.(implies(and(uses_scheduler(p), spread_applies(s, p), assign(p, n)), le(sub(add(spread_count(s, n), spread_self_match(s)), spread_global_min(s)), spread_max_skew(s))))))
```

## S1/S1-claim-exists: Referenced claims exist

Category: S1

- S1.claim: When uses(p,c), Require claim_exists(c).

```text
∀p:Pod.(∀c:Claim.(implies(uses(p, c), claim_exists(c))))
```

## S1/S1-fixed-volume-exists: Existing referenced claims have valid fixed-volume references

Category: S1

- S1.fixed-reference: When uses(p,c) and claim_exists(c), Require fixed_volume_exists(c); the atom is true when no fixed volume is named.

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c)), fixed_volume_exists(c))))
```

## S2/S2.class.dynamic-storage-class-exists: Dynamic claims require an existing storage class

Category: S2

- S2.class: When uses(p,c) and claim_exists(c) and dynamic(c), Require storage_class_exists(c). Do not force selection of dynamic.

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c), dynamic(c)), storage_class_exists(c))))
```

## S2/S2.driver.dynamic-provisioner-configured: Dynamic claims require a configured provisioner

Category: S2

- S2.driver: When uses(p,c) and claim_exists(c) and dynamic(c), Require provisioner_configured(c).

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c), dynamic(c)), provisioner_configured(c))))
```

## S2/S2.parameters.dynamic-provisioner-parameters-valid: Dynamic claims require valid provisioner parameters

Category: S2

- S2.parameters: When uses(p,c) and claim_exists(c) and dynamic(c), Require provisioner_parameters_valid(c).

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c), dynamic(c)), provisioner_parameters_valid(c))))
```

## S3/S3.path-selection: Referenced eligible claims select a storage path

Category: S3

- S3.path: When uses(p,c) and claim_exists(c) and fixed_volume_exists(c), Require (dynamic(c) AND dynamic_eligible(c)) OR an existing Volume v selected by bind(c,v). This is implication from the guard, never OR with the guard.

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c), fixed_volume_exists(c)), or(and(dynamic(c), dynamic_eligible(c)), ∃v:Volume.(bind(c, v))))))
```

## S3/S3.static-binding-compatibility: Static bindings satisfy claim and volume compatibility

Category: S3

- S3.static-compatibility: When uses(p,c) and claim_exists(c) and fixed_volume_exists(c) and bind(c,v), Require class_ok(c,v), access_ok(c,v), mode_ok(c,v), capacity_ok(c,v), volume_available(c,v), and pv_selector_ok(c,v) together.

```text
∀p:Pod.(∀c:Claim.(∀v:Volume.(implies(and(uses(p, c), claim_exists(c), fixed_volume_exists(c), bind(c, v)), and(class_ok(c, v), access_ok(c, v), mode_ok(c, v), capacity_ok(c, v), volume_available(c, v), pv_selector_ok(c, v))))))
```

## S3/S3.fixed-volume-choice: Fixed-volume claims prohibit dynamic provisioning and select their fixed volume

Category: S3

- S3.fixed-choice: When uses(p,c) and claim_exists(c) and fixed_volume_exists(c) and has_fixed_volume(c), Forbid dynamic(c); every selected bind(c,v) must satisfy fixed_to(c,v).

```text
∀p:Pod.(∀c:Claim.(implies(and(uses(p, c), claim_exists(c), fixed_volume_exists(c), has_fixed_volume(c)), and(not(dynamic(c)), ∀v:Volume.(implies(bind(c, v), fixed_to(c, v)))))))
```

## S4/S4-static-selected-volume-topology: Selected static volume permits assigned node

Category: S4

- S4.static-topology: When uses(p,c) and assign(p,n) and bind(c,v), Require volume_node_ok(v,n), only for the selected PV.

```text
∀p:Pod.(∀c:Claim.(∀n:Node.(∀v:Volume.(implies(and(uses(p, c), assign(p, n), bind(c, v)), volume_node_ok(v, n))))))
```

## S4/S4-dynamic-selected-path-topology: Selected dynamic path permits assigned node

Category: S4

- S4.dynamic-topology: When uses(p,c) and assign(p,n) and dynamic(c), Require dynamic_node_ok(c,n), only for the selected dynamic path.

```text
∀p:Pod.(∀c:Claim.(∀n:Node.(implies(and(uses(p, c), assign(p, n), dynamic(c)), dynamic_node_ok(c, n)))))
```

## E1/E1-background-host-ports-free: Assigned Pods Require Free Background Host Ports

Category: E1

- E1.background: When assign(p,n), Require host_ports_free(p,n) against background reservations.

```text
∀p:Pod.(∀n:Node.(implies(assign(p, n), host_ports_free(p, n))))
```

## E1/E1-target-pair-host-ports-disjoint: Co-Located Distinct Pods Require Disjoint Host Ports

Category: E1

- E1.target-pair: When p and q are distinct targets, assign(p,n) and assign(q,n), Require ports_disjoint(p,q). Different nodes must not conflict under this requirement.

```text
∀p:Pod.(∀q:Pod.(∀n:Node.(implies(and(not(eq(p, q)), assign(p, n), assign(q, n)), ports_disjoint(p, q)))))
```

## R5/R5-driver-volume-budget: Scheduler-managed CSI volume attachment counts must respect per-node driver limits

Category: R5

- R5.driver-volume-budget: When uses_scheduler(p) and assign(p,n) and volume_limit_applies(n,d) and volume_count_new(p,n,d) > 0, Require volume_count_before(p,n,d) + volume_count_new(p,n,d) <= volume_limit(n,d). Equality passes; zero new volumes imposes no budget check.

```text
∀p:Pod.(∀n:Node.(∀d:Driver.(implies(and(uses_scheduler(p), assign(p, n), volume_limit_applies(n, d), gt(volume_count_new(p, n, d), 0)), le(add(volume_count_before(p, n, d), volume_count_new(p, n, d)), volume_limit(n, d))))))
```
