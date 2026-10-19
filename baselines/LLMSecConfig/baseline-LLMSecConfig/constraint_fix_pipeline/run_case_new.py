# run_case.py
import time
import subprocess
import sys
import os
import json
from config import FIXED_YAML_PATH


def rewrite_pod_info_empty():
    yaml_dir = os.path.dirname(FIXED_YAML_PATH)
    pod_info_path = os.path.join(yaml_dir, "pod_info.json")

    empty_pod_info = {
        "pod_status": {},
        "pod_events": {},
        "is_pending": False,
        "error_labels": []
    }

    with open(pod_info_path, "w", encoding="utf-8") as f:
        json.dump(empty_pod_info, f, indent=2)

    print(f"[INFO] pod_info.json rewritten (empty) -> {pod_info_path}")


def run_cmd_or_fail(cmd, desc):
    print(f"\n[INFO] {desc}")
    result = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.stdout:
        print(result.stdout)

    if result.returncode != 0:
        print(f"[FATAL] {desc} failed!")

        if result.stderr:
            print("STDERR:")
            print(result.stderr)

        rewrite_pod_info_empty()

        sys.exit(1)


def run_case():
    print("Resetting the cluster...")
    run_cmd_or_fail(
        "kwokctl delete cluster && kwokctl create cluster",
        "Reset the cluster"
    )

    print("Waiting for the cluster to start...")
    time.sleep(6)

    print("Applying the environment configuration...")
    run_cmd_or_fail(
        f"kubectl apply --dry-run=client -f {FIXED_YAML_PATH}",
        "Dry-run validation of the environment YAML"
    )

    run_cmd_or_fail(
        f"kubectl apply -f {FIXED_YAML_PATH}",
        "Apply the environment YAML"
    )

    print("Waiting for the environment configuration to take effect...")
    time.sleep(10)

    print("Collecting the cluster state...")
    run_cmd_or_fail(
        "python ./scripts/collect_cluster_state.py",
        "Collect the cluster state"
    )

    print("Converting the application configuration...")
    run_cmd_or_fail(
        "python ./scripts/data_preprocessing.py",
        "Convert the application YAML to JSON"
    )

    print("Merging the input data...")
    run_cmd_or_fail(
        "python ./scripts/merge_case_input.py",
        "Merge the case input"
    )

    print("Running the solver...")
    run_cmd_or_fail(
        "python ./solver/z3_solver_while.py",
        "Run the Z3 solver"
    )

    print("\n All steps completed!")


if __name__ == "__main__":
    run_case()
