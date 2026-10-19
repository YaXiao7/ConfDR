import re

# (Regex, Type, Description, Suggestions)
HARDCODED_RULES = [
    (re.compile(r"^REQ_ASSIGN_(?P<pod>.+)$"), "UnscheduledPod", 
     "Global Unsatisfiability: Pod cannot fit on ANY node due to aggregate constraints (resources + selectors + taints).", 
     ["Check cluster capacity", "Relax hard anti-affinity", "Remove conflicting nodeSelectors"]), 

    (re.compile(r"^NODE_NAME_(?P<pod>.+)_NOT_(?P<node>.+)$"), "NodeNameMismatch", 
     "Hard Constraint Violation: 'spec.nodeName' is set to a node that does not satisfy other constraints.", 
     ["Remove 'nodeName' to allow scheduler to choose", "Fix the specific node's taint or resources"]), 

    (re.compile(r"^NODE_SELECTOR_(?P<pod>.+)_NOT_(?P<node>.+)$"), "NodeSelectorMismatch", 
     "Label Mismatch: Node labels do not match Pod's 'nodeSelector'.", 
     ["Correct the key/value in nodeSelector", "Ensure the label exists on the target node"]), 

    (re.compile(r"^Taints_Tolerations_(?P<pod>.+)_NOT_(?P<node>.+)$"), "TaintViolation", 
     "Taint/Toleration Conflict: Node has a taint (NoSchedule) that the Pod does not tolerate.", 
     ["Add matching 'tolerations' to Pod", "Remove taint from Node if not needed"]), 

    (re.compile(r"^NodeAffinity_(?P<pod>.+)_NOT_(?P<node>.+)$"), "NodeAffinityMismatch", 
     "Node Affinity Violation: Node does not match 'nodeAffinity.requiredDuringScheduling'.", 
     ["Check 'matchExpressions' logic", "Relax to 'preferredDuringScheduling' if possible"]), 

    (re.compile(r"^POD_AFFINITY_(?P<pod>.+)_NOT_(?P<node>.+)$"), "PodAffinityMissing", 
     "Inter-Pod Affinity: Required co-located Pod is not running in the topology domain.", 
     ["Ensure the target Pod is scheduled first", "Check labelSelector matches existing Pods"]), 

    (re.compile(r"^POD_ANTI_AFFINITY_(?P<pod>.+)_(?P<node>.+)$"), "PodAntiAffinityConflict", 
     "Inter-Pod Anti-Affinity: A conflicting Pod exists in this topology domain.", 
     ["Use a different topologyKey", "Relax anti-affinity rules"]), 

    (re.compile(r"^PV_NodeAffinity_(?P<pod>.+)_(?P<claim>.+)_(?P<pv>.+)_NOT_(?P<node>.+)$"), "VolumeNodeMismatch", 
     "Storage Topology Conflict: The PV bound to the Claim is local to a specific Node, but the Pod cannot be scheduled there.", 
     ["Pod MUST schedule to the PV's node", "ACTION: GENERATE a new PV without nodeAffinity if migration is needed"]), 

    (re.compile(r"^PVC_(?P<ns>.+)_(?P<claim>.+)_NOTFOUND_FOR_(?P<pod>.+)$"), "PVCMissing", 
     "Dependency Error: The PersistentVolumeClaim referenced by the Pod does not exist in the namespace.", 
     ["ACTION: GENERATE a new PVC yaml file", "ensure accessModes and storageClassName match the Pod usage"]), 

    (re.compile(r"^REASON_CAP_MATCH_(?P<ns>.+)_(?P<claim>.+)_(?P<pv>.+)_FOR_(?P<pod>.+)$"), "CapacityMismatch", 
     "Storage Resource: PV capacity < PVC request.", 
     ["Increase PV capacity", "Reduce PVC storage request"]), 

    (re.compile(r"^REASON_ACCESS_MATCH_(?P<ns>.+)_(?P<claim>.+)_(?P<pv>.+)_FOR_(?P<pod>.+)$"), "AccessModeMismatch", 
     "Mode Conflict: PV accessModes does not support PVC request (e.g., RWO vs RWX).", 
     ["Align PVC accessModes with PV capabilities"]), 

    (re.compile(r"^PVC_(?P<ns>.+)_(?P<claim>.+)_SC_NOTFOUND$"), "StorageClassMissing", 
     "Config Error: The StorageClass specified in the PVC is missing.", 
     ["ACTION: GENERATE a new StorageClass yaml file", "Ensure provisioner matches the cluster environment"]), 
    
    (re.compile(r"^CPU_LIMIT_(?P<node>.+)$"), "NodeCpuExhausted", 
     "Resource Starvation: Node has insufficient CPU for the Pod request.", 
     ["Reduce Pod CPU 'requests'", "Add more nodes"]), 

    (re.compile(r"^MEM_LIMIT_(?P<node>.+)$"), "NodeMemExhausted", 
     "Resource Starvation: Node has insufficient Memory for the Pod request.", 
     ["Reduce Pod Memory 'requests'", "Add more nodes"]), 

    (re.compile(r"^POD_LIMIT_(?P<node>.+)$"), "MaxPodsExceeded", 
    "Quota Limit: Node has reached max-pods limit.", 
    [ 
    "Remove 'nodeName' to allow scheduling on other nodes",  
    "Relax nodeSelector or anti-affinity rules",            
    "ACTION: GENERATE a new Node (if in simulation)"       
    ]), 
    
    (re.compile(r"^HOSTPORT_LIMIT_(?P<node>.+)_(?P<port>.+)$"), "PortConflict", 
     "Network Conflict: hostPort is already occupied on this node.", 
     ["Remove hostPort (use Service instead)", "Change port number"]), 

    (re.compile(r"^CONFIGMAP_NOT_FOUND_(?P<name>.+)$"), "ConfigMapMissing", 
    "Dependency Error: The Pod references a ConfigMap that does not exist.", 
    ["ACTION: GENERATE the missing ConfigMap", "Correct the name in the Pod spec"]), 
     
    (re.compile(r"^SERVICE_ACCOUNT_NOT_FOUND_(?P<name>.+)$"), "ServiceAccountMissing", 
    "Dependency Error: The Pod references a ServiceAccount that does not exist.", 
    ["ACTION: GENERATE the missing ServiceAccount", "Remove serviceAccountName field if not needed"])
]

def match_error_code_to_rule(error_code):
    for pattern, type_name, desc, suggestions in HARDCODED_RULES:
        if pattern.match(error_code):
            return {
                "type": type_name,
                "description": desc,
                "suggestions": suggestions
            }
    return None
