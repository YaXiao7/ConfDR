# run_case.py
import time
import subprocess
import sys
import os
import json
from repair.config import FIXED_YAML_PATH


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

    run_cmd_or_fail(
        "(kwokctl delete cluster || true) && kwokctl create cluster --kube-apiserver-image=registry.aliyuncs.com/google_containers/kube-apiserver:v1.29.6 --kube-controller-manager-image=registry.aliyuncs.com/google_containers/kube-controller-manager:v1.29.6 --kube-scheduler-image=registry.aliyuncs.com/google_containers/kube-scheduler:v1.29.6 --etcd-image=registry.aliyuncs.com/google_containers/etcd:3.5.11-0 --kwok-controller-binary=/usr/bin/kwok",
        "restart"
    )

    time.sleep(6)

    run_cmd_or_fail(
        f"kubectl apply --dry-run=client -f {FIXED_YAML_PATH}",
        "YAML dry-run"
    )

    run_cmd_or_fail(
        f"kubectl apply -f {FIXED_YAML_PATH}",
        "apply YAML"
    )

    time.sleep(40)

    run_cmd_or_fail(
        "python3 ./scripts/collect_cluster_state.py",
        "collecting..."
    )

    run_cmd_or_fail(
        "python3 ./scripts/data_preprocessing.py",
        "YAML -> JSON"
    )

    run_cmd_or_fail(
        "python3 ./scripts/merge_case_input.py",
        "merge case"
    )

    run_cmd_or_fail(
        "python3 ./solver/z3_solver_while.py",
        "running Z3"
    )

    print("\n all completed!")


if __name__ == "__main__":
    run_case()
