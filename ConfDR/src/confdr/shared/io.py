"""Files, configuration and reproducible hashes; all domain inputs and outputs use JSON/YAML."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if not (ROOT / "config/settings.yaml").exists():
    ROOT = Path(sys.prefix) / "share/confdr"


def read(path: str | Path) -> Any:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        try:
            return json.load(stream) if path.suffix == ".json" else yaml.safe_load(stream)
        except yaml.YAMLError:
            raise ValueError(f"Invalid YAML syntax: {path}") from None


def manifests(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        try:
            docs = [item for item in yaml.safe_load_all(stream) if item is not None]
        except yaml.YAMLError:
            raise ValueError(f"Invalid manifest YAML syntax: {path}") from None
    if not docs or any(not isinstance(item, dict) for item in docs):
        raise ValueError("Manifests must contain Kubernetes objects")

    def flatten(items, depth=0):
        if depth > 20:
            raise ValueError("Kubernetes List nesting is too deep")
        result = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Kubernetes List.items must contain objects")
            if item.get("kind") == "List":
                children = item.get("items")
                if not isinstance(children, list):
                    raise ValueError("Kubernetes List is missing an items list")
                result.extend(flatten(children, depth + 1))
            else:
                result.append(item)
        return result

    return flatten(docs)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def write(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".confdr-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def yaml_text(docs: list[dict]) -> str:
    return yaml.safe_dump_all(docs, allow_unicode=True, sort_keys=False)


def settings(path: str | Path | None = None) -> dict:
    result = read(path or ROOT / "config/settings.yaml")
    if not isinstance(result, dict):
        raise ValueError("Configuration root must be an object")
    return result
