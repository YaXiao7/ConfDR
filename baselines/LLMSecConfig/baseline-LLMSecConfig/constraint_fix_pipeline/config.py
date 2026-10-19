# config.py
import os

DATASET_ROOT = "/home/ubuntu/ConfDR/dataset/stackoverflow_cases"
# DATASET_ROOT = "/root/k8s_z3/dataset/   baseline-deploy-fix/k8s  extended_cases  trainticket_cases    stackoverflow_cases"

CASE_ID = os.environ.get("CASE_ID", "")

CASE_PATH = os.path.join(DATASET_ROOT, CASE_ID)

FIXED_YAML_PATH = os.path.join(CASE_PATH,"fixed.yaml")
CLUSTER_STATE_JSON = os.path.join(CASE_PATH, "cluster_state.json")
POD_INFO_JSON = os.path.join(CASE_PATH, "pod_info.json")
APPS_JSON = os.path.join(CASE_PATH, "apps.json")
CASE_JSON = os.path.join(CASE_PATH, "case.json")
