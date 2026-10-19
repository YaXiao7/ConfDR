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

- Respond **only** with the final, fully repaired YAML manifest 
  (not a patch and not explanatory text).
- Output must be wrapped in a fenced `yaml` code block.
- Do not provide commentary, reasoning, or description.
- The response must be runnable using `kubectl apply -f`.
- **Ordering Requirement**: Ensure resources are ordered by dependency: 
    StorageClass -> PV -> PVC -> Deployment/Service -> Node (if any).
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

**Error Pattern Details (from constraint_model.csv):**
{error_patterns if error_patterns else "(No matching patterns found)"}

**Repair Examples & Guidelines (from constraint_model_repair):**
{repair_examples if repair_examples else "(No repair examples found)"}
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