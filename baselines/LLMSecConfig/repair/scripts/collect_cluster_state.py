# k8s_z3/collect_cluster_state.py
import sys
import os
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

import subprocess
import json
from pathlib import Path
from repair.config import CLUSTER_STATE_JSON

def parse_quantity(value: str, resource_type: str) -> int:
    if value is None:
        return 0

    value = str(value).strip()

    if resource_type == "cpu":
        if value.endswith("m"):  # 500m
            return int(value[:-1])
        elif value.endswith("k"):  # 1k -> 1000m
            return int(value[:-1]) * 1000
        elif value.isdigit():  # 3 -> 3000m
            return int(value) * 1000
        else:
            raise ValueError(f"Unsupported CPU format: {value}")

    # ================= Memory =================
    elif resource_type == "memory":
        units = {
            "Ki": 1 / 1024,
            "Mi": 1,
            "Gi": 1024,
            "Ti": 1024 * 1024,
            "Pi": 1024 * 1024 * 1024,
            "Ei": 1024 * 1024 * 1024 * 1024,
        }
        for unit, factor in units.items():
            if value.endswith(unit):
                return int(int(value[:-len(unit)]) * factor)
        if value.isdigit(): 
            return int(value)
        raise ValueError(f"Unsupported Memory format: {value}")

    # ================= Pods =================
    elif resource_type == "pods":
        if value.endswith("M"):  # 1M -> 1000000
            return int(value[:-1]) * 1000000
        elif value.endswith("k"):  # 2k -> 2000
            return int(value[:-1]) * 1000
        elif value.isdigit():  
            return int(value)
        else:
            raise ValueError(f"Unsupported Pods format: {value}")

    else:
        raise ValueError(f"Unknown resource_type: {resource_type}")


def run_cmd(cmd):
    return json.loads(subprocess.check_output(cmd, shell=True))

def parse_nodes(nodes_json, pods_json):

    result = {}
    node_used = {}

    for node in nodes_json["items"]:
        name = node["metadata"]["name"]

        
        alloc_cpu_m = parse_quantity(node["status"]["allocatable"]["cpu"], "cpu")
        alloc_mem_mi = parse_quantity(node["status"]["allocatable"]["memory"], "memory")
        alloc_pods = parse_quantity(node["status"]["allocatable"]["pods"], "pods")

        result[name] = {
            "alloc_cpu_m": alloc_cpu_m,
            "alloc_mem_mi": alloc_mem_mi,
            "alloc_pods": alloc_pods,

            "labels": node["metadata"].get("labels", {}),
            "taints": node["spec"].get("taints", []),
            "used_ports": []  
        }

        node_used[name] = {"cpu_m": 0, "mem_mi": 0, "pods": 0}

    
    for pod in pods_json["items"]:
        node_name = pod.get("spec", {}).get("nodeName")
        if not node_name or node_name not in node_used:
            continue

        node_used[node_name]["pods"] += 1

        for c in pod.get("spec", {}).get("containers", []):
            resources = c.get("resources", {}).get("requests", {})
            cpu_req = resources.get("cpu", "0")
            mem_req = resources.get("memory", "0")

            cpu_m = parse_quantity(cpu_req, "cpu")
            mem_mi = parse_quantity(mem_req, "memory")

            node_used[node_name]["cpu_m"] += cpu_m
            node_used[node_name]["mem_mi"] += mem_mi

            for port in c.get("ports", []):
                if "hostPort" in port:
                    result[node_name]["used_ports"].append(port["hostPort"])

    for node_name in result:
        alloc = result[node_name]
        used = node_used[node_name]

        alloc["cpu"] = alloc["alloc_cpu_m"] - used["cpu_m"]
        alloc["mem"] = alloc["alloc_mem_mi"] - used["mem_mi"]
        alloc["remain_pods"] = alloc["alloc_pods"] - used["pods"]

    return result

def parse_existing_pods(pods_json):
    result = {}
    for pod in pods_json["items"]:
        name = pod["metadata"]["name"]
        ns = pod["metadata"]["namespace"]
        labels = pod["metadata"].get("labels", {})
        node_name = pod.get("spec", {}).get("nodeName", None)

        if node_name:  
            result[name] = {
                "namespace": ns,
                "labels": labels,
                "nodeName": node_name
            }
    return result

def parse_pvcs(pvcs_json):
    result = {}
    for pvc in pvcs_json["items"]:
        ns = pvc["metadata"]["namespace"]
        name = pvc["metadata"]["name"]
        spec = pvc["spec"]
        status = pvc["status"]

        result.setdefault(ns, {})
        result[ns][name] = {
            "accessMode": spec.get("accessModes", []),
            "storageClassName": spec.get("storageClassName"),
            "volumeMode": spec.get("volumeMode"),
            "requestsGi": spec["resources"]["requests"]["storage"],
            "volumeName": spec.get("volumeName"),
            "STATUS": status.get("phase"),
            "boundPV": spec.get("volumeName")
        }
    return result

def parse_pvs(pvs_json):
    result = {}
    for pv in pvs_json["items"]:
        name = pv["metadata"]["name"]
        spec = pv["spec"]
        result[name] = {
            "accessMode": spec.get("accessModes", []),
            "storageClassName": spec.get("storageClassName"),
            "volumeMode": spec.get("volumeMode"),
            "capacityGi": spec["capacity"]["storage"],
            "STATUS": pv["status"]["phase"],
            "nodeAffinity": spec.get("nodeAffinity")
        }
    return result

def parse_scs(scs_json):
    result = {}
    for sc in scs_json["items"]:
        name = sc["metadata"]["name"]
        result[name] = {"provisioner": sc.get("provisioner")}
    return result

def collect_cluster_state(output_file: str):
    nodes = run_cmd("kubectl get nodes -o json")
    pods = run_cmd("kubectl get pods -A -o json")
    pvcs = run_cmd("kubectl get pvc -A -o json")
    pvs = run_cmd("kubectl get pv -o json")
    scs = run_cmd("kubectl get sc -o json")

    cluster_state = {
        "Nodes": parse_nodes(nodes, pods),
        "ExistingPods": parse_existing_pods(pods),
        "PVCs": parse_pvcs(pvcs),
        "PVs": parse_pvs(pvs),
        "StorageClasses": parse_scs(scs)
    }

    Path(output_file).write_text(json.dumps(cluster_state, indent=2))
    print(f"[OK] Cluster state saved to {output_file}")

if __name__ == "__main__":
    collect_cluster_state(CLUSTER_STATE_JSON)
