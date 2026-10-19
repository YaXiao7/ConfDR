import sys
import os
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)
import yaml
import json
from repair.config import FIXED_YAML_PATH, CASE_PATH

def parse_pod_spec(pod_spec, metadata, replicas=1):
    pods = {}
    base_name = metadata.get("name", "unnamed-pod")
    namespace = metadata.get("namespace", "default")

    for i in range(replicas):
        pod_name = f"{base_name}-{i}" if replicas > 1 else base_name

        cpu_total = 0
        mem_total = 0
        host_ports = []

        for c in pod_spec.get("containers", []):
            resources = c.get("resources", {})
            requests = resources.get("requests", {})
            cpu_val = requests.get("cpu", "0")
            mem_val = requests.get("memory", "0")

            def normalize_cpu(cpu_val):
                if isinstance(cpu_val, str):
                    if cpu_val.endswith("m"):  
                        return int(cpu_val[:-1])
                    else:  
                        return int(float(cpu_val) * 1000)
                return int(cpu_val * 1000)


            def normalize_mem(mem_val):
                if isinstance(mem_val, str):
                    if mem_val.endswith("Gi"):
                        return int(float(mem_val[:-2]) * 1024)   # Gi → Mi
                    elif mem_val.endswith("Mi"):
                        return int(float(mem_val[:-2]))         
                    else:  
                        return int(float(mem_val))
                return int(mem_val)
            
            cpu_total += normalize_cpu(cpu_val)
            mem_total += normalize_mem(mem_val)

            for port in c.get("ports", []):
                if "hostPort" in port:
                    host_ports.append(port["hostPort"])

        pods[pod_name] = {
            "cpu": cpu_total,
            "mem": mem_total,
            "host_ports": host_ports,
            "labels": metadata.get("labels", {}),
            "nodeName": pod_spec.get("nodeName"),
            "nodeSelector": pod_spec.get("nodeSelector"),
            "tolerations": pod_spec.get("tolerations"),
            "affinity": pod_spec.get("affinity"),
            "volumes": pod_spec.get("volumes"),
            "namespace": namespace
        }

    return pods


def process_case(case_dir, output_file="apps.json"):
    all_pods = {}
    print(case_dir)
    for filename in os.listdir(case_dir):
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        filepath = os.path.join(case_dir, filename)
        if filename == "input.yaml":
            continue
        with open(filepath,"r") as f:
            docs = yaml.safe_load_all(f)
            for data in docs:
                if not data:
                    continue
                kind = data.get("kind")
                if kind == "Pod":
                    pods = parse_pod_spec(data["spec"], data["metadata"], replicas=1)
                    all_pods.update(pods)

                elif kind == "Deployment":
                    replicas = data["spec"].get("replicas", 1)
                    pod_template = data["spec"]["template"]
                    metadata = pod_template["metadata"]
                    if "namespace" not in metadata and "namespace" in data.get("metadata", {}):
                        metadata["namespace"] = data["metadata"]["namespace"]
                    if "name" not in metadata and "name" in data.get("metadata", {}):
                        metadata["name"] = data["metadata"]["name"]
                    pod_spec = pod_template["spec"]
                    pods = parse_pod_spec(pod_spec, metadata, replicas=replicas)
                    all_pods.update(pods)

                elif kind == "CronJob":
                    job_template = data.get("spec", {}).get("jobTemplate", {})
                    pod_template = job_template.get("spec", {}).get("template", None)

                    if pod_template is None:
                        print(f"CronJob {filename}  template")
                        continue

                    metadata = pod_template.get("metadata", {})
                    top_meta = data.get("metadata", {})

                    if "name" not in metadata and "name" in top_meta:
                        metadata["name"] = top_meta["name"] + "-cronpod"
                    if "namespace" not in metadata and "namespace" in top_meta:
                        metadata["namespace"] = top_meta["namespace"]

                    pod_spec = pod_template.get("spec", {})

                    pods = parse_pod_spec(pod_spec, metadata, replicas=1)
                    all_pods.update(pods)

    result = {
        "WaitingDeployPods": all_pods,
        "ClusterState": {
        }
    }

    out_path = os.path.join(output_file, "apps.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"output: {out_path}")

if __name__ == "__main__":
    yaml_path = CASE_PATH
    output_path = CASE_PATH
    process_case(yaml_path, output_path)
