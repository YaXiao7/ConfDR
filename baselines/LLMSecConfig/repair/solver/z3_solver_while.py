from z3 import *
import json, itertools
import sys
import os
import time
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)
from repair.config import CASE_JSON, CASE_PATH

class K8sSchedulerVerifier:
    def __init__(self, cluster_file="cluster.json"):
        """
        init function: initialize variables
        """
        self.cluster_file = cluster_file
        self.solver = Solver()
        self.tracked = []
        self.Assign = {}
        
        self.Nodes, self.Pods = [], []
        
        self.AvailCPU, self.AvailMem = {}, {}
        self.ReqCPU, self.ReqMem = {}, {}
        self.RemainPods = {}
        self.UsedPorts = {}
        self.ReqPorts = {}
        
        self.PodNodeName = {}
        self.NodeLabels, self.PodSelector = {}, {}
        self.NodeTaints = {}
        self.PodTolerations = {}
        self.PodNodeAffinity = {}
        self.PVNodeAffinity = {}
        self.ExistingPods = {}
        self.ExistingPodLabels = {}

        self.Namespaces = {}
        self.PodVolumes = {}
        self.PVCs = {}
        self.PVs = {}
        self.StorageClasses = {}
        self.Bind = {}

    def load_case(self):
        """
        load cluster state from json file
        """
        with open(self.cluster_file) as f:
            data = json.load(f)

        self.all_nodes_specs = data.get("Nodes", {})
        self.all_pods_specs = data.get("WaitingDeployPods", {})

        self.Nodes = list(data["ClusterState"]["Nodes"].keys())
        self.Pods = list(data["WaitingDeployPods"].keys())
        self.AvailCPU = {n: data["ClusterState"]["Nodes"][n]["cpu"] for n in self.Nodes}
        self.AvailMem = {n: data["ClusterState"]["Nodes"][n]["mem"] for n in self.Nodes}
        self.ReqCPU = {p: data["WaitingDeployPods"][p]["cpu"] for p in self.Pods}
        self.ReqMem = {p: data["WaitingDeployPods"][p]["mem"] for p in self.Pods}
        self.RemainPods = {n: data["ClusterState"]["Nodes"][n]["remain_pods"] for n in self.Nodes}
        self.UsedPorts = {n: data["ClusterState"]["Nodes"][n].get("used_ports", []) for n in self.Nodes}
        self.ReqPorts = {p: data["WaitingDeployPods"][p].get("host_ports", []) for p in self.Pods}

        self.PodNodeName = {p: data["WaitingDeployPods"][p].get("nodeName", None) for p in self.Pods}
        self.NodeLabels = {n: data["ClusterState"]["Nodes"][n].get("labels", {}) for n in self.Nodes}
        self.PodSelector = {p: data["WaitingDeployPods"][p].get("nodeSelector", {}) for p in self.Pods}
        self.NodeTaints = {n: data["ClusterState"]["Nodes"][n].get("taints", []) for n in self.Nodes}
        self.PodTolerations = {p: data["WaitingDeployPods"][p].get("tolerations", []) for p in self.Pods}
        for p, pod_spec in self.all_pods_specs.items():
            affinity = pod_spec.get("affinity", {})
            node_affinity = affinity.get("nodeAffinity", {}) if affinity else {}
            required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {})
            self.PodNodeAffinity[p] = required.get("nodeSelectorTerms", [])
        self.ExistingPodLabels = {p: spec["labels"] for p, spec in self.ExistingPods.items()}

        self.Namespaces = {p: data["WaitingDeployPods"][p].get("namespace", "default") for p in self.Pods}
        self.PodVolumes = {
            p: [
                v["persistentVolumeClaim"]["claimName"] 
                for v in data["WaitingDeployPods"][p].get("volumes", []) or []
                if "persistentVolumeClaim" in v and "claimName" in v["persistentVolumeClaim"]
            ] 
            for p in self.Pods
        }
        self.PVCs = data.get("ClusterState", {}).get("PVCs", {})
        self.PVs = data.get("ClusterState", {}).get("PVs", {})
        self.StorageClasses = data.get("ClusterState", {}).get("StorageClasses", {})

        for pv_name, pv in self.PVs.items():
            if not isinstance(pv, dict):
                pv = {}
            
            node_affinity = pv.get("nodeAffinity")
            if not isinstance(node_affinity, dict):
                node_affinity = {}
            
            required = node_affinity.get("required", {})
            node_selector_terms = required.get("nodeSelectorTerms", [])
            
            self.PVNodeAffinity[pv_name] = node_selector_terms

        self.ExistingPods = data.get("ExistingPods", {})

        for p in self.Pods:
            for n in self.Nodes:
                self.Assign[(p, n)] = Bool(f"A_{p}_{n}")

    def add_tracked(self, name, cond, skip_tags=None):
        """
        tool function to add tracked constraints
        """
        if skip_tags and name in skip_tags:
            print(f"Skipping constraint: {name}")
            return None
        tag = Bool(name)
        self.solver.assert_and_track(cond, tag)
        self.tracked.append((name, tag, cond))
        return tag

    def add_rule_pod_assignment(self, skip_tags=None):
        for p in self.Pods:
            cond = PbEq([(self.Assign[(p, n)], 1) for n in self.Nodes], 1)
            self.add_tracked(f"REQ_ASSIGN_{p}", cond)
    
    def add_node_name_constraints(self, skip_tags=None):
        for p in self.Pods:
            node_name = self.PodNodeName.get(p, None)
            if node_name is None:
                continue
            for n in self.Nodes:
                if n == node_name:
                    cond = self.Assign[(p, n)]
                    self.add_tracked(f"NODE_NAME_{p}_{n}", cond, skip_tags)
                else:
                    cond = Not(self.Assign[(p, n)])
                    self.add_tracked(f"NODE_NAME_{p}_NOT_{n}", cond, skip_tags)

    def add_node_selector_constraints(self, skip_tags=None):
        for p in self.Pods:
            selector = self.PodSelector.get(p, {})
            if not selector:
                continue
            for n in self.Nodes:
                node_labels = self.NodeLabels.get(n, {})
                ok = all(node_labels.get(k) == v for k, v in selector.items())
                if not ok:
                    cond = Not(self.Assign[(p,n)])
                    self.add_tracked(f"NODE_SELECTOR_{p}_NOT_{n}", cond, skip_tags)

    def pod_tolerates_all_taints(self, pod, node):
        taints = self.NodeTaints.get(node, [])
        if not taints:
            return True
        for taint in taints:
            if taint.get("effect") not in ["NoSchedule", "NoExecute"]:
                continue
            if not self.pod_tolerates_taint(pod, taint):
                return False
        return True

    def pod_tolerates_taint(self, pod, taint):
        tolerations = self.PodTolerations.get(pod, []) or []
        t_key, t_val, t_eff = taint.get("key"), taint.get("value"), taint.get("effect")

        for tol in tolerations:
            op = tol.get("operator", "Equal")
            key = tol.get("key")
            val = tol.get("value")
            eff = tol.get("effect")

            if op == "Exists" and key is None and val is None:
                return True

            if op == "Equal" and key == t_key and val == t_val and eff == t_eff:
                return True

            if op == "Exists" and key == t_key and (eff == t_eff or eff is None):
                return True

        return False

    def add_taints_and_tolerations_constraints(self, skip_tags=None):
        for p in self.Pods:
            for n in self.Nodes:
                tolerated = self.pod_tolerates_all_taints(p, n)
                if not tolerated:
                    cond = Not(self.Assign[(p, n)])
                    self.add_tracked(f"Taints_Tolerations_{p}_NOT_{n}", cond, skip_tags)

    def match_expression_satisfied(self, node_labels, expr):
        key = expr["key"]
        operator = expr["operator"]
        values = expr.get("values", [])

        if operator == "In":
            return key in node_labels and node_labels[key] in values
        elif operator == "NotIn":
            return key not in node_labels or node_labels[key] not in values
        elif operator == "Exists":
            return key in node_labels
        elif operator == "DoesNotExist":
            return key not in node_labels
        else:
            raise NotImplementedError(f"Unsupported operator: {operator}")

    def node_satisfies_term(self, node_labels, term):
        expressions = term.get("matchExpressions", [])
        return all(self.match_expression_satisfied(node_labels, expr) for expr in expressions)

    def add_node_affinity_constraints(self, skip_tags=None):
        for p in self.Pods:
            terms = self.PodNodeAffinity.get(p, [])
            if not terms:
                continue

            for n in self.Nodes:
                node_labels = self.NodeLabels[n]

                satisfies = any(self.node_satisfies_term(node_labels, term) for term in terms)

                if not satisfies:
                    self.add_tracked(f"NodeAffinity_{p}_NOT_{n}", Not(self.Assign[(p, n)]), skip_tags)
    
    def match_one_selector(self, selector, labels):
        expressions = selector.get("matchExpressions", [])
        return all(self.match_expression_satisfied(labels, expr) for expr in expressions)


    def match_any_selector(self, selectors, labels):
        for term in selectors:
            selector = term.get("labelSelector", {})
            if self.match_one_selector(selector, labels):
                return True
        return False

    def add_pod_affinity_constraints(self, skip_tags=None):
        for p, pspec in self.all_pods_specs.items():
            affinity = pspec.get("affinity", {})
            podAffinityTerms = (affinity or {}).get("podAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution", [])
            if podAffinityTerms:
                for n in self.Nodes:
                    has_match = BoolVal(False)

                    for q, qspec in self.ExistingPods.items():
                        if qspec["nodeName"] == n and self.match_any_selector(podAffinityTerms, qspec.get("labels", {})):
                            has_match = BoolVal(True)

                    for q, qspec in self.all_pods_specs.items():
                        if q == p:
                            continue
                        if self.match_any_selector(podAffinityTerms, qspec.get("labels", {})):
                            has_match = Or(has_match, self.Assign[(q, n)])

                    self.add_tracked(
                        f"POD_AFFINITY_{p}_NOT_{n}",
                        Implies(self.Assign[(p, n)], has_match), skip_tags
                    )
            podAntiAffinityTerms = (affinity or {}).get("podAntiAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution", [])
            if podAntiAffinityTerms:
                for n in self.Nodes:
                    conflict = False

                    for q, qspec in self.ExistingPods.items():
                        if qspec["nodeName"] == n and self.match_any_selector(podAntiAffinityTerms, qspec.get("labels", {})):
                            conflict = True

                    for q, qspec in self.all_pods_specs.items():
                        if q == p:
                            continue
                        if self.match_any_selector(podAntiAffinityTerms, qspec.get("labels", {})):
                            conflict = Or(conflict, self.Assign[(q, n)])

                    self.add_tracked(
                        f"POD_ANTI_AFFINITY_{p}_{n}",
                        Implies(self.Assign[(p, n)], Not(conflict)), skip_tags
                    )
    
    def pv_allows_node(self, pv_name, node_labels):
        terms = self.PVNodeAffinity.get(pv_name, [])
        if not terms:
            return True
        return any(self.node_satisfies_term(node_labels, term) for term in terms)

    def add_pv_node_affinity_constraints(self, skip_tags=None):
        for p in self.Pods:
            ns = self.Namespaces.get(p, "default")
            for claim in self.PodVolumes.get(p, []):
                for pv_name in self.PVs.keys():
                    bvar = self.Bind.get((ns, claim, pv_name))
                    if bvar is None:
                        continue
                    for n in self.Nodes:
                        node_labels = self.NodeLabels[n]
                        ok = self.pv_allows_node(pv_name, node_labels)
                        if not ok:
                            cond = Implies(And(bvar, self.Assign[(p, n)]), False)
                            self.add_tracked(f"PV_NodeAffinity_{p}_{claim}_{pv_name}_NOT_{n}", cond, skip_tags)


    def add_rule_node_resources(self, skip_tags=None):
        for n in self.Nodes:
            cpu_sum = Sum([If(self.Assign[(p, n)], IntVal(self.ReqCPU[p]), IntVal(0)) for p in self.Pods])
            mem_sum = Sum([If(self.Assign[(p, n)], IntVal(self.ReqMem[p]), IntVal(0)) for p in self.Pods])
            self.add_tracked(f"CPU_LIMIT_{n}", cpu_sum <= IntVal(self.AvailCPU[n]), skip_tags)
            self.add_tracked(f"MEM_LIMIT_{n}", mem_sum <= IntVal(self.AvailMem[n]), skip_tags)

    def add_pod_capacity_constraints(self, skip_tags=None):
        for n in self.Nodes:
            pod_count = Sum([If(self.Assign[(p, n)], IntVal(1), IntVal(0)) for p in self.Pods])
            cond = pod_count <= IntVal(self.RemainPods[n])
            self.add_tracked(f"POD_LIMIT_{n}", cond, skip_tags)

    def add_hostport_constraints(self, skip_tags=None):
        all_ports = set()
        for p in self.Pods:
            for port in self.ReqPorts.get(p, []):
                all_ports.add(port)

        for n in self.Nodes:
            used_ports_on_node = set(self.UsedPorts.get(n, []))
            for port in all_ports:
                used_existing = 1 if port in used_ports_on_node else 0

                port_claim_sum = Sum([
                    If(self.Assign[(p, n)],
                    IntVal(1) if port in self.ReqPorts.get(p, []) else IntVal(0),
                    IntVal(0))
                    for p in self.Pods
                ])
                self.add_tracked(f"HOSTPORT_LIMIT_{n}_{port}", port_claim_sum + IntVal(used_existing) <= IntVal(1), skip_tags)

    def add_pvc_pv_binding_constraints(self, skip_tags=None):
        def to_list(x):
            if x is None:
                return []
            return x if isinstance(x, list) else [x]

        def bool_const(b: bool):
            return BoolVal(bool(b))

        pv_to_bindvars = {pv: [] for pv in self.PVs.keys()}

        for p in self.Pods:
            ns = self.Namespaces.get(p, "default")
            for claim in self.PodVolumes.get(p, []):
                pvc = self.PVCs.get(ns, {}).get(claim, None)

                if pvc is None:
                    self.add_tracked(f"PVC_{ns}_{claim}_NOTFOUND_FOR_{p}", BoolVal(False), skip_tags)
                    continue

                pvc_status = pvc.get("STATUS")
                if pvc_status == "Bound":
                    bound_pv = pvc.get("volumeName")
                    if bound_pv:
                        bvar = BoolVal(True)
                        self.Bind[(ns, claim, bound_pv)] = bvar
                    continue

                def parse_gi(value):
                    if isinstance(value, str) and value.lower().endswith("gi"):
                        return int(value[:-2])
                    return int(value)
                    
                pvc_cap = parse_gi(pvc.get("requestsGi", 0))

                pvc_modes = to_list(pvc.get("accessMode"))
                pvc_sc    = pvc.get("storageClassName", None)
                pvc_vm    = pvc.get("volumeMode", None)
                fixed_pv  = pvc.get("volumeName", None)

                bind_vars = []

                for pv_name, pv in self.PVs.items():
                    sc_ok   = Bool(f"OK_SC_{ns}_{claim}_{pv_name}")
                    am_ok   = Bool(f"OK_ACCESS_{ns}_{claim}_{pv_name}")
                    vm_ok   = Bool(f"OK_VMODE_{ns}_{claim}_{pv_name}")
                    cap_ok  = Bool(f"OK_CAP_{ns}_{claim}_{pv_name}")
                    av_ok   = Bool(f"OK_AVAIL_{ns}_{claim}_{pv_name}")

                    pv_sc      = pv.get("storageClassName", None)
                    pv_modes   = to_list(pv.get("accessMode"))
                    pv_vm      = pv.get("volumeMode", None)
                    pv_cap     = parse_gi(pv.get("capacityGi", 0))
                    pv_status_ok= (pv.get("STATUS", "Available") == "Available")

                    sc_match      = (pvc_sc is None and pv_sc is None) or (pv_sc == pvc_sc)
                    access_match  = all(m in pv_modes for m in pvc_modes)
                    vmode_match   = (not pvc_vm) or (not pv_vm) or (pvc_vm == pv_vm)
                    cap_match     = (pvc_cap == 0) or (pv_cap >= pvc_cap)
                    avail_match   = pv_status_ok

                    self.add_tracked(f"REASON_SC_MATCH_{ns}_{claim}_{pv_name}_FOR_{p}",   sc_ok   == bool_const(sc_match), skip_tags)
                    self.add_tracked(f"REASON_ACCESS_MATCH_{ns}_{claim}_{pv_name}_FOR_{p}", am_ok   == bool_const(access_match), skip_tags)
                    self.add_tracked(f"REASON_VMODE_MATCH_{ns}_{claim}_{pv_name}_FOR_{p}",  vm_ok   == bool_const(vmode_match), skip_tags)
                    self.add_tracked(f"REASON_CAP_MATCH_{ns}_{claim}_{pv_name}_FOR_{p}",    cap_ok  == bool_const(cap_match), skip_tags)
                    self.add_tracked(f"REASON_AVAIL_{ns}_{claim}_{pv_name}_FOR_{p}",        av_ok   == bool_const(avail_match), skip_tags)

                    bvar = Bool(f"BIND_{ns}_{claim}_{pv_name}")
                    self.Bind[(ns, claim, pv_name)] = bvar
                    bind_vars.append(bvar)
                    pv_to_bindvars[pv_name].append(bvar)

                    self.add_tracked(
                        f"BIND_IMPL_{ns}_{claim}_{pv_name}_FOR_{p}",
                        Implies(bvar, And(sc_ok, am_ok, vm_ok, cap_ok, av_ok))
                    )

                if fixed_pv:
                    if fixed_pv not in self.PVs:
                        self.add_tracked(f"PVC_{ns}_{claim}_FIXEDPV_{fixed_pv}_NOTFOUND", BoolVal(False), skip_tags)
                    else:
                        self.add_tracked(f"PVC_{ns}_{claim}_FIXED_BIND_{fixed_pv}", self.Bind[(ns, claim, fixed_pv)], skip_tags)

                        self.add_tracked(
                            f"PVC_{ns}_{claim}_UNIQUE_BINDING_FOR_{p}",
                            PbLe([(self.Bind[(ns, claim, fixed_pv)], 1)], 1), skip_tags
                        )
                        continue
                
                sc_found_ok      = Bool(f"OK_SC_FOUND_{ns}_{claim}")
                prov_ok          = Bool(f"OK_SC_PROVISIONER_{ns}_{claim}")

                sc_exist     = True
                prov_valid   = True
                if pvc_sc is None:
                    sc_exist = False
                else:
                    sc = self.StorageClasses.get(pvc_sc)
                    if sc is None:
                        sc_exist = False
                    else:
                        prov = sc.get("provisioner", "")
                        if prov == "kubernetes.io/no-provisioner" or prov == "":
                            prov_valid = False

                self.add_tracked(f"PVC_{ns}_{claim}_SC_NOTFOUND", sc_found_ok == bool_const(sc_exist), skip_tags)
                self.add_tracked(f"PVC_{ns}_{claim}_SC_NO_PROVISIONER", prov_ok == bool_const(prov_valid), skip_tags)

                self.add_tracked(
                    f"PVC_{ns}_{claim}_NEED_STATIC_OR_DYNAMIC_SATISFY_{p}",
                    Or(Or(bind_vars), And(sc_found_ok, prov_ok))
                )
               
                if bind_vars:
                    self.add_tracked(f"PVC_{ns}_{claim}_UNIQUE_BINDING_FOR_{p}", PbLe([(b,1) for b in bind_vars], 1), skip_tags)

        for pvname, binds in pv_to_bindvars.items():
            if binds:
                self.add_tracked(f"PV_{pvname}_UNIQUE_BINDING", PbLe([(b,1) for b in binds], 1), skip_tags)

    # -------------------------
    # SAT
    # -------------------------
    def _print_sat_model(self):
        m = self.solver.model()
        print("SAT: found a valid assignment!")
        for p in self.Pods:
            assigned = [n for n in self.Nodes if m.evaluate(self.Assign[(p, n)])]   
            print(f"  {p} -> {assigned[0]}" if assigned else f"  {p} -> (Pending)")

def run_single_check(case_file, skip_tags=None):
    """
    Run a single check with optional skip_tags.
    """
    verifier = K8sSchedulerVerifier(case_file)
    verifier.load_case()

    verifier.add_rule_pod_assignment(skip_tags)

    verifier.add_node_name_constraints(skip_tags)
    verifier.add_node_selector_constraints(skip_tags)
    verifier.add_taints_and_tolerations_constraints(skip_tags)
    verifier.add_node_affinity_constraints(skip_tags)
    verifier.add_pod_affinity_constraints(skip_tags)

    verifier.add_rule_node_resources(skip_tags)
    verifier.add_pod_capacity_constraints(skip_tags)
    verifier.add_hostport_constraints(skip_tags)

    verifier.add_pvc_pv_binding_constraints(skip_tags)
    verifier.add_pv_node_affinity_constraints(skip_tags)

    res = verifier.solver.check()

    if res.r == 1:  # sat
        verifier._print_sat_model()
    elif res == unknown:
        print("Solver returned unknown result.")
        return set()
    else:   # unsat
        core = verifier.solver.unsat_core()
        tags = {str(t) for t in core}
        return tags
    
def run_solver_single_round(case_path, case_json):
    """
    Ablation: single-round solving (no iterative conflict elimination)
    """
    output_file = os.path.join(case_path, "res_single_round.txt")

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"=== Single-Round Verification Result for Case: {os.path.basename(case_path)} ===\n\n")

            core_tags = run_single_check(case_json, skip_tags=None)

            if not core_tags:
                msg = "SAT: All constraints satisfied in a single check.\n"
                print(msg)
                f.write(msg)
            else:
                msg = f"UNSAT: {len(core_tags)} constraints in unsat core.\n"
                print(msg)
                f.write(msg)
                for tag in sorted(core_tags):
                    f.write(f"   {tag}\n")
    finally:
        print(f"[Single-round] Results written to: {output_file}")


def run_solver_with_output(case_path, case_json):
    output_file = os.path.join(case_path, "res.txt")
    time_path = os.path.join(case_path, "solve_time.json")

    skip_tags = set()
    all_cores_union = set()
    max_rounds = 10

    start_time = time.time()

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"=== Verification Result for Case: {os.path.basename(case_path)} ===\n\n")

            for i in range(max_rounds):
                print(f"\n=== Round {i+1} ===")
                f.write(f"--- Round {i+1} ---\n")

                core_tags = run_single_check(case_json, skip_tags)

                # Case 1: SAT in the first round
                if i == 0 and not core_tags:
                    msg = "SAT: All constraints satisfied in the first check.\n"
                    print(msg)
                    f.write(msg)
                    return

                # Case 2: SAT after removing conflicts
                if not core_tags:
                    msg = "SAT: System became satisfiable after filtering conflicting constraints.\n"
                    print(msg)
                    f.write(msg)
                    f.write(f"\nTotal number of unique conflicting constraints detected: {len(all_cores_union)}\n")
                    f.write("Union of all unsatisfiable constraint labels:\n")
                    for tag in sorted(all_cores_union):
                        f.write(f"   {tag}\n")
                    return

                # UNSAT in this round
                print(f"UNSAT in round {i+1}: {len(core_tags)} constraints in core.")
                f.write(f"UNSAT in round {i+1}: {len(core_tags)} constraints in core.\n")

                for tag in sorted(core_tags):
                    f.write(f"   {tag}\n")

                skip_tags |= core_tags
                all_cores_union |= core_tags

            # Final UNSAT after all rounds
            print("\nFinal result: UNSAT after all rounds.")
            f.write("\nFinal result: UNSAT after all rounds.\n")
            f.write(f"Total number of unique conflicting constraints detected: {len(all_cores_union)}\n\n")
            f.write("Union of all unsatisfiable constraint labels:\n")
            for tag in sorted(all_cores_union):
                f.write(f"   {tag}\n")

    finally:
        end_time = time.time()
        solve_time = end_time - start_time

        with open(time_path, "w", encoding="utf-8") as tf:
            json.dump({"solve_time_sec": solve_time}, tf, indent=2)

        print(f"Solve time (sec): {solve_time:.6f}")
        print(f"Results written to: {output_file}")


if __name__ == "__main__":
    run_solver_with_output(CASE_PATH, CASE_JSON)
    # run_solver_single_round(CASE_PATH, CASE_JSON)