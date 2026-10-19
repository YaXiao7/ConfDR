"""Application-first changes, with explicit evidence before environment fallback."""

import json
import re

from confdr.collection.files import manifest_scope, object_key
from confdr.shared.io import digest

# These inventories can support creation proposals, not proof of backing infrastructure.
INVENTORY_KINDS = {
    "Nodes": "Node",
    "PVs": "PersistentVolume",
    "PVCs": "PersistentVolumeClaim",
    "StorageClasses": "StorageClass",
}


def environment_request(request, case, documents, locations):
    """Validate model justification and derive affected objects from original evidence."""
    if not isinstance(request, dict) or request.get("needed") is not True:
        return None
    if any(not isinstance(request.get(k), str) or not request[k].strip() for k in ("reason", "impact")):
        raise ValueError(
            "Environment fallback requires an application-only failure reason and impact assessment"
        )
    evidence = request.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Environment fallback requires original environment evidence")
    payload = {"case": case, "documents": documents}
    targets, creates = set(), set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "value_json"}:
            raise ValueError("Invalid environment evidence structure")
        path = item["path"]
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
            raise ValueError("Invalid environment evidence path")
        parts = [x.replace("~1", "/").replace("~0", "~") for x in path[1:].split("/")]
        try:
            value = payload
            for part in parts:
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", part):
                        raise ValueError("Invalid evidence index")
                    value = value[int(part)]
                else:
                    value = value[part]
            if digest(value) != digest(json.loads(item["value_json"])):
                raise ValueError("Environment evidence value does not match original input")
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError("Environment evidence does not resolve to original input") from exc
        if len(parts) >= 2 and parts[0] == "documents":
            doc = documents[int(parts[1])]
            if manifest_scope(doc, locations) == "environment":
                targets.add(object_key(doc))
        elif len(parts) >= 3 and parts[:2] == ["case", "ClusterState"] and parts[2] in INVENTORY_KINDS:
            creates.add(INVENTORY_KINDS[parts[2]])
    if not targets and not creates:
        raise ValueError(
            "Cite environment documents or a known environment inventory, not only application evidence"
        )
    return {
        **request,
        "editable_objects": [list(k) for k in sorted(targets)],
        "creatable_kinds": sorted(creates),
    }


def check_repair_scope(candidate, original, locations, phase, request=None):
    before = {object_key(d): d for d in original}
    environment_changes = []
    for doc in candidate:
        key = object_key(doc)
        if doc == before.get(key) or manifest_scope(doc, locations) == "application":
            continue
        if phase == "application":
            raise ValueError(f"Application-first phase cannot modify or create environment object: {key}")
        if request is None or (
            key in before
            and list(key) not in request["editable_objects"]
            or key not in before
            and doc.get("kind") not in request["creatable_kinds"]
        ):
            raise ValueError(f"Environment change is outside the cited evidence: {key}")
        environment_changes.append(
            {
                "kind": key[0],
                "namespace": key[1],
                "name": key[2],
                "action": "modify" if key in before else "create",
            }
        )
    return environment_changes
