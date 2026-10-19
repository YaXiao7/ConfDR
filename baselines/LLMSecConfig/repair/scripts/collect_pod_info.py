# scripts/collect_pod_info.py
import subprocess
import json
from pathlib import Path
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from repair.config import POD_INFO_JSON  

def run_cmd(cmd):
    """Run shell command and return JSON result"""
    return json.loads(subprocess.check_output(cmd, shell=True))

def collect_pod_status_and_events(output_path=None):
    target_path = output_path if output_path else POD_INFO_JSON
    print(f"[INFO] Collecting pod status & events to {target_path}...")

    pods_json = run_cmd("kubectl get pods -A -o json")
    pod_items = pods_json.get("items", [])

    pod_status = {}
    pod_events = {}
    is_pending = False

    for item in pod_items:
        name = item["metadata"]["name"]
        namespace = item["metadata"]["namespace"]
        phase = item.get("status", {}).get("phase", "Unknown")

        pod_status[name] = {
            "namespace": namespace,
            "phase": phase,
        }

        if phase == "Pending":
            is_pending = True

        try:
            cmd = f"kubectl get event -n {namespace} --field-selector involvedObject.name={name} -o json"
            events_json = run_cmd(cmd)
            pod_events[name] = events_json.get("items", [])
        except Exception as e:
            pod_events[name] = []
            print(f"[WARN] Failed to get events for pod {name}: {e}")

    result = {
        "is_pending": is_pending,
        "pod_status": pod_status,
        "pod_events": pod_events,
        "error_labels": []  
    }

    Path(target_path).write_text(json.dumps(result, indent=2))
    print(f"[OK] Pod info saved to {target_path}")


if __name__ == "__main__":
    collect_pod_status_and_events()
