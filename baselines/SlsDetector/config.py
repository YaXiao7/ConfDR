import os

# dataset root and case id are provided at runtime
DATASET_ROOT = os.environ.get("DATASET_ROOT", "")
CASE_ID = os.environ.get("CASE_ID", "")

# case path
CASE_PATH = os.path.join(DATASET_ROOT, CASE_ID)

# paths within a case
ENVS_PATH = os.path.join(CASE_PATH, "envs")
APPS_PATH = os.path.join(CASE_PATH, "apps")

CLUSTER_STATE_JSON = os.path.join(CASE_PATH, "cluster_state.json")
POD_INFO_JSON = os.path.join(CASE_PATH, "pod_info.json")
APPS_JSON = os.path.join(CASE_PATH, "apps.json")
CASE_JSON = os.path.join(CASE_PATH, "case.json")
