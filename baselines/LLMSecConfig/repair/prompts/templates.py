from typing import Dict, List, Tuple

from langchain_core.prompts import ChatPromptTemplate


def _escape_braces(text: str) -> str:
    """Escape curly braces so ChatPromptTemplate doesn't treat them as variables.
    This is important when user-provided YAML or examples contain `{}` or `{{ }}`.
    """
    if not isinstance(text, str):
        text = str(text)
    return text.replace("{", "{{").replace("}", "}}")


def build_instructions() -> Tuple[str, str]:
    return (
        "system",
        """You are **KubeFixer**, a Kubernetes Configuration Auto-Repair LLM designed for
high-precision diagnosis and remediation of deployment and storage errors.

Your primary objective is to analyze Z3 Unsat-Core outputs and identify the
underlying root cause that prevents a Pod, Deployment, StatefulSet, or PVC from
successfully scheduling or binding. You maintain comprehensive internal
knowledge of failure taxonomies including Scheduling, Resource, Network, and
Volume subsystems, each associated with standard remediation strategies and
validated Kubernetes YAML repair templates.

When given a user-provided Kubernetes manifest and its corresponding Unsat-Core
diagnosis, your task is **not to explain the failure**, but to generate a
**fully corrected and directly applicable YAML configuration** derived from the
original input.  

The corrected YAML must:
- Represent a *minimal and semantically consistent modification*.
- Preserve all unaffected fields and structural integrity of the resource.
- Apply only the necessary edits to resolve the constraint conflict.
- Be executable without additional manual intervention.

Error classification framework:

1) **Scheduling Conflicts**
   includes `NodeSelector`, `NodeName`, `Taints/Tolerations`, `NodeAffinity`,
   `PodAffinity`, and `PodAntiAffinity` misalignment.

2) **Resource Exhaustion**
   e.g., CPU, memory, or Pod capacity quota saturation at node or cluster scope.

3) **Network Constraints**
   HostPort collisions or exclusive port binding violations.

4) **Volume & Storage Failures**
   including missing PVC/PV, incompatible StorageClass, access mode mismatch,
   insufficient capacity, `no-provisioner` static binding scenarios, and
   dynamic provisioning failure.

---------------------------------------------------------------
### Output Requirements (STRICT)

- **Step 1: Conflict Analysis & Reasoning**
  Before generating the YAML, you MUST analyze the conflict based on the provided "Detected Error Codes" and "Error Pattern Details".
  - Identify the specific constraint violation (e.g., node selector mismatch, resource limit exceeded).
  - Explain *why* the current configuration fails (the root cause).
  - Propose a minimal repair strategy.
  - Wrap this analysis in a `<reasoning>` block.

- **Step 2: Fixed YAML**
  - Respond **only** with the final, fully repaired YAML manifest 
    (not a patch and not explanatory text).
  - Output must be wrapped in a fenced `yaml` code block.
  - Do not provide commentary, reasoning, or description.
  - The response must be runnable using `kubectl apply -f`.
  - **Ordering Requirement**: Ensure resources are ordered by dependency: 
    StorageClass -> PV -> PVC -> Deployment/Service -> Node (if any).

**Example Output Format:**
<reasoning>
The error code S2 indicates a NodeSelector mismatch. The Pod requires label 'disktype=ssd', but no nodes in the cluster satisfy this.
Strategy: Remove the nodeSelector to allow scheduling on any node, or update it to match existing node labels.
</reasoning>

```yaml
apiVersion: v1
kind: Pod
...
```
-----------------------------------------------------------------""",
    )


def build_input_yaml_section(input_yaml: str) -> Tuple[str, str]:
    """Build the input YAML section for the prompt."""
    return (
        "human",
        f"""#### Input YAML Configuration

The following Kubernetes configuration contains issues that need to be fixed:

```yaml
{input_yaml}
```

-----------------------------------------------------------------""",
    )


def build_error_analysis_section(error_codes: str, error_patterns: str, repair_examples: str) -> Tuple[str, str]:
    """Build the error analysis section with detected codes, patterns, and repair examples."""
    return (
        "human",
        f"""#### Error Analysis & Repair Guidance

**Detected Error Codes:**
```
{error_codes if error_codes else "(None)"}
```

**Step 1: Understand the Conflict Rules (from constraint_model)**
The following rules define the specific constraints that are violated:
{error_patterns if error_patterns else "(No matching patterns found)"}

**Step 2: Reference Repair Examples (Chain-of-Thought)**
The following examples demonstrate the full reasoning process for resolving similar conflicts. 
You MUST follow this reasoning pattern: [Detected Conflict] -> [Conflict Explanation] -> [Repair Strategy] -> [Fixed YAML].

**Reference Templates (Few-shot Examples):**

**Example 1: Scheduling - Label Mismatch**
[Detected Conflict]: NodeSelectorMismatch 
[Conflict Explanation]: The Pod requires a node with label 'disktype=ssd', but no nodes in the cluster satisfy this constraint.
[Repair Strategy]: Remove the conflicting nodeSelector to allow scheduling on any available node, or correct the label key/value.
[Fixed YAML]:
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx
spec:
  # nodeSelector:   <-- REMOVED or MODIFIED to resolve conflict
  #   disktype: ssd
  containers:
  - name: nginx
    image: nginx
```

**Example 2: Scheduling - Taint Violation**
[Detected Conflict]: TaintViolation
[Conflict Explanation]: The target node has a taint 'dedicated=gpu:NoSchedule', but the Pod is missing the corresponding toleration.
[Repair Strategy]: Add a toleration to the Pod spec to allow scheduling on the tainted node.
[Fixed YAML]:
```yaml
apiVersion: v1
kind: Pod
spec:
  tolerations:
  - key: "dedicated"
    operator: "Equal"
    value: "gpu"
    effect: "NoSchedule"
  containers:
  - name: cuda-app
    image: cuda-image
```

**Example 3: Resource - CPU Exhaustion**
[Detected Conflict]: NodeCpuExhausted
[Conflict Explanation]: The Pod requests 2000m CPU, but the available node only has 1000m allocatable.
[Repair Strategy]: Reduce the Pod's CPU request to fit within the node's capacity.
[Fixed YAML]:
```yaml
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: heavy-app
    resources:
      requests:
        cpu: "500m"  # Reduced from 2000m to fit available capacity
```

**Example 4: Storage - Capacity Mismatch**
[Detected Conflict]: CapacityMismatch
[Conflict Explanation]: The PersistentVolumeClaim requests 10Gi storage, but the bound PersistentVolume only has 5Gi.
[Repair Strategy]: Reduce the PVC storage request to match the PV capacity.
[Fixed YAML]:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
spec:
  resources:
    requests:
      storage: 5Gi  # Reduced from 10Gi to match PV
```

**Example 5: Network - Port Conflict**
[Detected Conflict]: PortConflict
[Conflict Explanation]: The Pod specifies `hostPort: 80`, which is already occupied on the target node.
[Repair Strategy]: Remove `hostPort` and rely on a Service for external access, or change the port.
[Fixed YAML]:
```yaml
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: web
    ports:
    - containerPort: 80
      # hostPort: 80  <-- Removed to resolve port conflict
```

**Retrieved Examples:**
{repair_examples if repair_examples else "# No repair examples available"}
-----------------------------------------------------------------""",
    )




def compose_messages(
    input_yaml: str,
    error_codes: str,
    error_patterns: str,
    repair_examples: str,
    relationships: List[str],
    conflicts: List[str],
    suggestions: List[str],
) -> List[Tuple[str, str]]:
    """Compose the complete message sequence for LLM prompt.

    Note: relationships/conflicts/suggestions are accepted but intentionally
    not included in the LLM prompt to reduce irrelevant context.
    """
    # Escape dynamic content
    input_yaml_esc = _escape_braces(input_yaml)
    error_codes_esc = _escape_braces(error_codes)
    error_patterns_esc = _escape_braces(error_patterns)
    repair_examples_esc = _escape_braces(repair_examples)

    messages: List[Tuple[str, str]] = []

    # 1. System instruction
    messages.append(build_instructions())

    # 2. Input YAML
    messages.append(build_input_yaml_section(input_yaml_esc))

    # 3. Error analysis (codes + patterns + repair examples)
    messages.append(build_error_analysis_section(error_codes_esc, error_patterns_esc, repair_examples_esc))

    # Note: constraints analysis intentionally omitted

    return messages


def build_prompt_from_variables(variables: Dict[str, str | List[str]]) -> ChatPromptTemplate:
    """Build ChatPromptTemplate from variables dict.

    relationships/conflicts/suggestions are accepted in variables for compatibility
    but are not added to the prompt content.
    """
    messages = compose_messages(
        input_yaml=variables.get("input_yaml", ""),
        error_codes=variables.get("error_codes", ""),
        error_patterns=variables.get("error_patterns", ""),
        repair_examples=variables.get("repair_examples", ""),
        relationships=variables.get("relationships", []),
        conflicts=variables.get("conflicts", []),
        suggestions=variables.get("suggestions", []),
    )
    return ChatPromptTemplate.from_messages(messages)