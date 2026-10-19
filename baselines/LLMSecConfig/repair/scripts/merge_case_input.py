# k8s_z3/merge_case_input.py
import json
import sys
import os
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)
from pathlib import Path
from repair.config import APPS_JSON, CLUSTER_STATE_JSON, CASE_PATH


def merge_case_input(yaml_json_file, cluster_state_file, output_file):
    pod_part = json.loads(Path(yaml_json_file).read_text())
    cluster_state = json.loads(Path(cluster_state_file).read_text())

    merged = {
        "WaitingDeployPods": pod_part["WaitingDeployPods"],
        "ClusterState": cluster_state
    }

    Path(output_file).write_text(json.dumps(merged, indent=2))
    print(f"[OK] case input saved to {output_file}")

    for path in [CLUSTER_STATE_JSON, APPS_JSON]:
        if os.path.exists(path):
            os.remove(path)
    print(f"[INFO] deleted.")

if __name__ == "__main__":
    case_path = CASE_PATH

    out_path = os.path.join(case_path, "case.json")

    merge_case_input(APPS_JSON, CLUSTER_STATE_JSON, out_path)
