import os
import time
import pandas as pd
import re
from openai import OpenAI   # type: ignore

chose_model = "gpt-4o"

client = OpenAI(
    base_url='XXX',
    api_key= 'xxx',

)

def get_content(text):
    pattern = r'(<START>.*?<END>)'
    result = re.findall(pattern, text, re.DOTALL)

    return result if result else [text]

def get_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()

def create_prompt(data):
    prompt = f"""{data}
        Based on the above application configuration (Deployment/Pod) and the cluster state (Node, PV, PVC, etc.), determine whether the Pod will enter the Pending state during Kubernetes scheduling.  
        Please examine the configuration according to the following three categories of constraints:

        1. Node Matching Constraints:
        Check whether the Pod can be matched with at least one schedulable node. Pay special attention to:
        - Whether nodeName refers to a valid and schedulable node;
        - Whether nodeSelector keys and values exactly match node labels (including spelling and case);
        - Whether Taints and Tolerations match correctly;
        - Whether the required terms of NodeAffinity can be satisfied by at least one node;
        - Whether Pod Affinity / AntiAffinity rules are satisfiable (including labelSelector and topologyKey);
        - If PVs are used, whether PV.nodeAffinity allows scheduling the Pod onto the corresponding node.

        2. Resource Constraints:
        Check whether all resource-related scheduling requirements can be satisfied, including:
        - Whether the Pod's requested CPU and memory exceed the available resources on all nodes;
        - Whether the maximum number of schedulable Pods on the node has already been reached;
        - Whether HostPort conflicts with existing Pods on the node;
        - Whether resource fields have correct formats, units, and placements.

        3. Storage Dependency Constraints:
        Examine whether the Pod's storage requirements can be fulfilled, including:
        - Whether the referenced PVC exists;
        - Whether the PVC is successfully bound to a PV;
        - Whether PVC–PV matching fields (storageClass, accessModes, capacity, labelSelector, etc.) are consistent;
        - Whether fixed volume names are valid;
        - Whether dynamic provisioning is correctly configured;
        - Whether a PVC is being illegally shared by multiple Pods (expected one-to-one mapping).

        Please provide the following two results:

        (1) Whether the Pod will enter the Pending state (“Pending” or “Not Pending”).  
        (2) If the Pod will be Pending, list the specific configuration entries (fields) that directly cause the scheduling failure. And briefly describe why it leads to the Pod being Pending. 

        Answer format (You MUST follow this exactly):
        All content MUST be written between <START> and <END> tags.
        Do NOT add any explanations outside the tags.

        If the Pod will NOT be Pending:
        <START>
        {{
            "is_pending": false
        }}
        <END>

        If the Pod WILL be Pending:
        <START>
        {{
            "is_pending": true,
            "reasons": [
                {{
                "related_fields": ["..."],
                "description": "..."
                }}
            ]
        }}
        <END>

        Rules you MUST follow:
        1. If "is_pending" = false → DO NOT output the "reasons" field.
        2. JSON MUST be valid and strictly follow the structure above.
        3. Keys MUST use lowercase_with_underscore format exactly as written.
        4. Do NOT add extra fields.
    """
    return prompt

def constraint_method(data):
    
    prompt = create_prompt(data)

    # Record start timestamp (for latency measurement)
    start_time = time.time()

    response = client.chat.completions.create(
        model=chose_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert Kubernetes scheduling and configuration analyst. "
                    "You specialize in detecting pod Pending causes by analyzing "
                    "Deployment/Pod configurations together with Node, PV, and PVC states. "
                    "Your task is to determine whether the pod will be Pending and explain "
                    "which configuration entries cause this behavior."
                )
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )

    # Record end timestamp
    end_time = time.time()
    latency_ms = round((end_time - start_time) * 1000, 2)

    constraint_response = response.choices[0].message.content
    print(constraint_response)

    return get_content(constraint_response), latency_ms


def main():

    DATASET_ROOTS = [
        "./dataset/baseline-deploy-fix/k8s",
        "./dataset/extended_cases",
        "./dataset/stackoverflow_cases",
    ]

    output_file = "./output/output-gpt4o.csv"

    records = []

    for dataset_root in DATASET_ROOTS:

        if not os.path.isdir(dataset_root):
            print(f"[WARN] Dataset not found: {dataset_root}")
            continue

        dataset_name = os.path.basename(dataset_root)

        for case_name in sorted(os.listdir(dataset_root)):
            case_dir = os.path.join(dataset_root, case_name)

            if not os.path.isdir(case_dir):
                continue

            yaml_path = os.path.join(case_dir, "deploy.yaml")

            if not os.path.exists(yaml_path):
                print(f"[WARN] deploy.yaml not found in {case_dir}")
                continue

            print(f"[PROCESSING] {yaml_path}")

            with open(yaml_path, "r", encoding="utf-8") as f:
                content = f.read()

            constraint_response, latency_ms = constraint_method(content)

            row = {
                "Dataset": dataset_name,
                "Case": case_name,
                "Model": chose_model,
                "Configuration": yaml_path,
                "Final_responses": constraint_response,
                "Latency(ms)": latency_ms
            }

            records.append(row)

    df = pd.DataFrame(records)
    df.to_csv(output_file, mode='a', index=False, header=False)

    print("\n All cases processed and appended to CSV!")


if __name__ == "__main__":
    main()
