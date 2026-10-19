import os
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import yaml


def load_yaml_files_from_dir(dir_path: str) -> List[Dict[str, Any]]:
    """Load all YAML files under a directory, supporting multi-document YAML.

    Returns a list of YAML documents (each is a dict) across all files.
    """
    docs: List[Dict[str, Any]] = []
    p = Path(dir_path)
    if not p.exists():
        return docs
    for fp in p.glob("**/*.y*ml"):
        with open(fp, "r", encoding="utf-8") as f:
            for doc in yaml.load_all(f.read(), Loader=yaml.FullLoader):
                if isinstance(doc, dict):
                    docs.append(doc)
    return docs


def dump_yaml_docs(docs: List[Dict[str, Any]]) -> str:
    """Dump multiple YAML docs into a single string with YAML multi-doc format."""
    return yaml.dump_all(docs, sort_keys=False)


def parse_yaml_str_to_docs(text: str) -> List[Dict[str, Any]]:
    """Parse multi-doc YAML string into a list of dicts."""
    return [doc for doc in yaml.load_all(text, Loader=yaml.FullLoader) if isinstance(doc, dict)]


def load_fix_examples_from_dir(dir_path: str) -> List[Dict[str, Any]]:
    """Load historical fix examples from a directory.

    Supported formats:
    - JSON files: objects with keys {"error", "before", "after", "notes"}
    - Pairs of YAML files: e.g., before.yaml / after.yaml in the same folder

    Returns a list of dicts with normalized keys: error, before, after, notes.
    """
    examples: List[Dict[str, Any]] = []
    p = Path(dir_path)
    if not p.exists():
        return examples

    # JSON examples
    for fp in p.glob("**/*.json"):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                examples.append({
                    "error": data.get("error"),
                    "before": data.get("before"),
                    "after": data.get("after"),
                    "notes": data.get("notes"),
                    "source": str(fp)
                })
        except Exception:
            continue

    # YAML pairs: detect before/after pairs
    for folder in p.glob("**/"):
        before = None
        after = None
        for fp in folder.glob("*.y*ml"):
            name = fp.stem.lower()
            if "before" in name:
                before = fp
            elif "after" in name:
                after = fp
        if before and after:
            try:
                before_text = Path(before).read_text(encoding="utf-8")
                after_text = Path(after).read_text(encoding="utf-8")
                examples.append({
                    "error": None,
                    "before": before_text,
                    "after": after_text,
                    "notes": None,
                    "source": str(folder)
                })
            except Exception:
                continue

    return examples


def format_examples_for_prompt(examples: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Format historical examples into prompt-ready text blocks.

    Returns a tuple (examples_text, examples_compact) where examples_text is rich
    with fenced code blocks, and examples_compact is a simplified text.
    """
    parts: List[str] = []
    compact: List[str] = []
    for ex in examples:
        err = ex.get("error")
        before = ex.get("before")
        after = ex.get("after")
        notes = ex.get("notes")
        src = ex.get("source")
        block = []
        if err:
            block.append(f"Error: {err}")
        if notes:
            block.append(f"Notes: {notes}")
        if src:
            block.append(f"Source: {src}")
        if before:
            block.append("Before:\n```yaml\n" + before.strip() + "\n```")
        if after:
            block.append("After:\n```yaml\n" + after.strip() + "\n```")
        parts.append("\n".join(block))
        compact.append(f"Example from {src or 'unknown'}")
    return ("\n\n".join(parts), "\n".join(compact))


def iter_case_dirs(dir_path: str) -> List[Path]:
    """Return immediate subdirectories under dir_path (cases).

    If there are no subdirectories, the caller may treat the dir itself as one case.
    """
    p = Path(dir_path)
    if not p.exists():
        return []
    return [d for d in p.iterdir() if d.is_dir()]


def iter_group_case_dirs(dir_path: str) -> List[Path]:
    """Return second-level case directories: group/CASE under the given dir_path.

    This matches datasets where data_dir contains multiple groups, and each group
    contains multiple case subfolders. Only directories are returned.
    """
    p = Path(dir_path)
    if not p.exists():
        return []
    cases: List[Path] = []
    for group in p.iterdir():
        if group.is_dir():
            for case in group.iterdir():
                if case.is_dir():
                    cases.append(case)
    return cases


def load_yaml_docs_from_case(case_dir: str | Path) -> List[Dict[str, Any]]:
    """Load all YAML documents from a case directory (recursive)."""
    return load_yaml_files_from_dir(str(case_dir))


def load_external_conflicts_from_case(case_dir: str | Path) -> List[Dict[str, Any]]:
    """Optionally load external conflicts detected by other tools for a case.

    Supported file names inside the case directory:
    - external_conflicts.json
    - conflicts.json
    - external.json

    The file can be a list of conflict objects, or a dict containing a
    key "conflicts" with a list value.
    """
    p = Path(case_dir)
    candidates = [
        p / "external_conflicts.json",
        p / "conflicts.json",
        p / "external.json",
    ]
    for fp in candidates:
        if fp.exists():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    conf = data.get("conflicts")
                    if isinstance(conf, list):
                        return conf
            except Exception:
                # On parse failure, return empty and proceed without external conflicts
                return []
    return []


def dedup_examples_by_source(examples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate examples by their source path (or content fallback)."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for ex in examples:
        key = ex.get("source") or (ex.get("before"), ex.get("after"), ex.get("notes"))
        if key not in seen:
            seen.add(key)
            out.append(ex)
    return out
