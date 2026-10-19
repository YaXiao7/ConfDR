from typing import Any
import re
import yaml
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda


def _extract_yaml_blocks(text: str) -> list[str]:
    """Extract YAML code blocks from LLM output.
    - Prefer fenced blocks labeled as yaml: ```yaml ... ```
    - Fallback to generic fenced blocks: ``` ... ```
    Returns list of YAML snippets as strings.
    """
    if not isinstance(text, str):
        text = str(text)
    blocks: list[str] = []

    # Prefer explicit yaml fences
    for m in re.finditer(r"```yaml\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE):
        blocks.append(m.group(1).strip())

    # Fallback: generic fences
    if not blocks:
        for m in re.finditer(r"```\s*(.*?)\s*```", text, flags=re.DOTALL):
            blocks.append(m.group(1).strip())

    # If still empty, try to heuristically capture between '---' separators
    if not blocks:
        # Capture sequences that look like YAML docs separated by ---
        docs = re.split(r"\n\s*---\s*\n", text)
        if len(docs) > 1:
            # basic heuristic: keep only segments that parse as YAML
            for d in docs:
                try:
                    parsed = list(yaml.load_all(d, Loader=yaml.FullLoader))
                    if parsed:
                        blocks.append(d.strip())
                except Exception:
                    continue
    return blocks


def _parse_yaml_text(data: Any, strict: bool) -> str:
    """Parse LLM output into YAML text without capturing self to avoid circular deps."""
    # Unwrap message content if present
    if isinstance(data, BaseMessage):
        text = data.content
    elif isinstance(data, str):
        text = data
    else:
        # LangChain model may return dict-like content
        try:
            text = getattr(data, "content", None) or str(data)
        except Exception:
            text = str(data)

    blocks = _extract_yaml_blocks(text)
    if blocks:
        yaml_text = "\n---\n".join(blocks)
    else:
        yaml_text = text.strip()

    if strict:
        # Try to load and re-dump to ensure valid YAML
        try:
            docs = [doc for doc in yaml.load_all(yaml_text, Loader=yaml.FullLoader) if isinstance(doc, dict)]
            if docs:
                yaml_text = yaml.dump_all(docs, sort_keys=False)
        except Exception:
            # keep original yaml_text on failure
            pass

    return yaml_text


class YAMLTextOutputParser(RunnableLambda):
    """Runnable output parser that extracts YAML from LLM responses and returns YAML text.

    - Accepts `BaseMessage` or raw string from the model.
    - Extracts fenced YAML blocks (prefers ```yaml), or falls back to the full text.
    - Optionally validates and canonicalizes multi-doc output.
    """

    def __init__(self, strict: bool = False) -> None:
        # Avoid capturing `self` inside the lambda to prevent circular deps in LangChain.
        strict_flag = strict

        def _fn(x: Any) -> str:
            return _parse_yaml_text(x, strict_flag)

        super().__init__(_fn)