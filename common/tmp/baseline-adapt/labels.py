"""Map baseline outputs onto the new R/A/P/S/E taxonomy (scheduling-15-v4).

Two independent tables, both NEW to this project -- neither exists anywhere in the
repos (see tmp/baseline-adapt/README.md for the provenance note that must travel
with any published number derived from them).

1. CONSTRAINT_RULES  -- Z3 constraint label -> category code.
   The repair side emits raw Z3 constraint names. Two DIFFERENT legacy tables
   project those names onto two different code sets:
     * baseline-LLMSecConfig/constraint_fix_pipeline/runner.py:82-104  -> S/R/N/V
     * repair/outputs/Sec_Imp_..._metric.py:12-35                      -> C1..C15
   Both are projections of the same raw labels, so rather than translate
   old-code -> new-code (which first requires knowing which of the two produced a
   file) we rescan the raw label with one new table. That is the only
   lossless route, and it sidesteps the letter collision below.

   ** Letter collision -- the trap this table exists to avoid **
   The legacy codes reuse the letters R and S with unrelated meanings:
     legacy S3 = NODE_NAME (pin a pod to a node)      -> new P1, NOT new S3
     legacy S2 = NODE_SELECTOR                         -> new P1
     legacy R1 = CPU_LIMIT                             -> new R1 (coincidence)
     legacy R3 = POD_LIMIT (max pods)                  -> new R4, NOT new R3
     legacy N1 = HOSTPORT_LIMIT                        -> new E1
   Copying a legacy code through verbatim would silently score several classes as
   the wrong category.

2. FIELD_RULES -- yaml field path (free text) -> category code.
   SlsDetector emits `related_fields` as free-text field paths, not constraint
   names, so it needs a path-based table. `classify_fields` handles the one
   genuinely ambiguous case (a PVC reference can be an invalid reference S1, a
   provisioning-config error S2, a binding error S3 or a topology error S4) by
   reading the model's own `description` text as context.
"""

from __future__ import annotations

import re
from typing import Iterable

# --------------------------------------------------------------------------
# 1. Z3 constraint label -> category code
# --------------------------------------------------------------------------
# Grounded in config/taxonomy.yaml (15 categories). Ordered: first match wins,
# so the more specific storage/label patterns precede the general ones.
CONSTRAINT_RULES: list[tuple[re.Pattern[str], str]] = [
    # --- E: host ports ---
    (re.compile(r"^HOSTPORT_LIMIT_"), "E1"),
    # --- P: placement ---
    (re.compile(r"^POD_ANTI_AFFINITY_"), "P3"),
    (re.compile(r"^POD_AFFINITY_"), "P2"),
    (re.compile(r"^PV_NodeAffinity_"), "S4"),  # PV's node affinity vs placement
    (re.compile(r"^NodeAffinity_"), "P1"),
    (re.compile(r"^NODE_SELECTOR_"), "P1"),
    (re.compile(r"^NODE_NAME_"), "P1"),
    # --- A: taints ---
    (re.compile(r"(?i)taints?_?tolerations_"), "A1"),
    # --- R: capacity ---
    (re.compile(r"^CPU_LIMIT_"), "R1"),
    (re.compile(r"^MEM_LIMIT_"), "R2"),
    (re.compile(r"^POD_LIMIT_"), "R4"),  # max pods per node, not GPUs
    # --- S: storage ---
    (re.compile(r"^REASON_SC_MATCH_"), "S2"),
    (re.compile(r"^PVC_.*_SC_NO_PROVISIONER"), "S2"),
    (re.compile(r"^PVC_.*_SC_NOTFOUND"), "S2"),
    (re.compile(r"^REASON_ACCESS_MATCH_"), "S3"),
    (re.compile(r"^REASON_VMODE_MATCH_"), "S3"),
    (re.compile(r"^REASON_CAP_MATCH_"), "S3"),
    (re.compile(r"^REASON_AVAIL_"), "S3"),
    (re.compile(r"^PVC_.*_NEED_STATIC_OR_DYNAMIC_SATISFY_"), "S3"),
    (re.compile(r"^PVC_.*_FIXED_BIND_"), "S3"),
    (re.compile(r"^PVC_.*_FIXEDPV_.*_NOTFOUND"), "S1"),
    (re.compile(r"^PVC_.*_NOTFOUND_FOR_"), "S1"),
]

# Prefixes carrying no category meaning (mirrors the legacy IGNORED_PREFIXES).
IGNORED_CONSTRAINT_PREFIXES = ("REQ_ASSIGN_",)


def label_to_categories(constraint_label: str) -> set[str]:
    if constraint_label.startswith(IGNORED_CONSTRAINT_PREFIXES):
        return set()
    return {code for rx, code in CONSTRAINT_RULES if rx.search(constraint_label)}


def constraints_to_categories(constraint_labels: Iterable[str]) -> list[str]:
    out: set[str] = set()
    for label in constraint_labels:
        out |= label_to_categories(label)
    return sorted(out)


# --------------------------------------------------------------------------
# 2. yaml field path (free text) -> category code
# --------------------------------------------------------------------------
# Each entry is (regex over "field path + description", code). A field can match
# several entries; the result is the union. Order is irrelevant here because we
# do not stop at the first hit.
FIELD_RULES: list[tuple[re.Pattern[str], str]] = [
    # E -- host ports
    (re.compile(r"hostport|host_port"), "E1"),
    # P -- placement
    (re.compile(r"topologyspread|topology_spread|maxskew|whenunsatisfiable"), "P4"),
    (re.compile(r"podantiaffinity|pod_anti_affinity|anti-?affinity"), "P3"),
    (re.compile(r"podaffinity|pod_affinity"), "P2"),
    (re.compile(r"nodeaffinity|node_affinity|nodeselector|node_selector|nodename|node_name"), "P1"),
    # A -- taints / tolerations
    (re.compile(r"toleration|taint|noschedule|noexecute|node\.kubernetes\.io/"), "A1"),
    # R -- capacity
    (re.compile(r"nvidia\.com/gpu|amd\.com/gpu|gpu"), "R3"),
    (re.compile(r"maxpods|max_pods|pod.?slots?|pod capacity"), "R4"),
    (re.compile(r"\bcsi\b|volumeattachment|csinode"), "R5"),
    (re.compile(r"requests?\.cpu|cpu_request|\bcpu\b"), "R1"),
    (re.compile(r"requests?\.memory|memory_request|\bmemory\b"), "R2"),
    # S -- storage, specific before general
    (re.compile(r"storageclass|storage_class|provisioner|default.?class"), "S2"),
    (re.compile(r"allowedtopologies|volume_binding_mode|volumebindingmode"), "S4"),
    (re.compile(r"persistentvolume\b|persistent_volume|\bpv\b"), "S3"),
]

# A bare PVC reference is genuinely ambiguous between S1/S2/S3/S4; the model's own
# description text disambiguates it.
_PVC_RX = re.compile(r"persistentvolumeclaim|persistent_volume_claim|\bpvc\b|\bclaim\b")
_PV_RX = re.compile(r"persistentvolume\b|persistent_volume|\bpv\b|\bdata-volume\b")
_AFFINITY_RX = re.compile(r"node_?affinity|nodeselectorterms")
_PVC_DESC = [
    # S1: the reference does not resolve -- no such object, or the object lives in
    # another namespace. A PVC is namespace-scoped, so "exists only in <other>"
    # means this pod can never bind it.
    (re.compile(
        r"not found|does not exist|missing|no such|undefined|unbound|invalid reference"
        r"|no pvc|namespace|scoped|only exists|not provided"
    ), "S1"),
    # S2 must NOT key on the bare word "default": these descriptions routinely say
    # "in the default namespace", which would make S2 fire on nearly every PVC case.
    (re.compile(r"storage ?class|provisioner|dynamic provisioning|default storage|default class"), "S2"),
    (re.compile(r"node affinity|topology|zone|node placement|volume node"), "S4"),
    # Binding faults are the default reading of a PVC that will not attach.
    (re.compile(r"bind|attach|access mode|volume mode|capacity|selector|match|eligib"), "S3"),
]


def classify_fields(fields: Iterable[str], description: str = "") -> list[str]:
    """Map SlsDetector's free-text `related_fields` onto category codes.

    Fields are classified one at a time rather than as one joined string. A single
    SlsDetector reason often cites several objects at once -- e.g. a PV's node
    affinity next to a Node's zone label -- and joining them lets a PV's
    `spec.nodeAffinity` vote for P1 (pod placement) while its own file path votes
    for S3. Per-field evaluation keeps each object's evidence separate.
    """
    codes: set[str] = set()
    for field in fields:
        text = str(field).lower()
        # A PersistentVolume's node affinity is storage topology (S4), NOT pod
        # placement (P1): the PV pins itself to a zone, the pod does not choose.
        # Model it before the generic rules so P1 does not also fire.
        pv_affinity = bool(_PV_RX.search(text) and _AFFINITY_RX.search(text))
        for rx, code in FIELD_RULES:
            # A PV's own affinity path is storage topology, not placement (P1) and
            # not a binding fault (S3) -- the generic "mentions a PV" rule fires on
            # the filename alone, so both have to be suppressed here.
            if pv_affinity and code in {"P1", "S3"}:
                continue
            if rx.search(text):
                codes.add(code)
        if pv_affinity:
            codes.add("S4")

    if _PVC_RX.search(" ".join(str(f) for f in fields).lower()) or _PVC_RX.search(description.lower()):
        # The four S diagnoses are mutually exclusive, so take the first match
        # instead of accumulating every keyword the sentence happens to contain.
        desc = description.lower()
        for rx, code in _PVC_DESC:
            if rx.search(desc):
                codes.add(code)
                break
        else:
            codes.add("S3")  # binding faults are the default reading of a stuck PVC
    return sorted(codes)


def classify_reasons(reasons: object) -> list[str]:
    """Apply `classify_fields` to SlsDetector's parsed `reasons` list."""
    codes: set[str] = set()
    if not isinstance(reasons, list):
        return []
    for reason in reasons:
        if not isinstance(reason, dict):
            continue
        fields = reason.get("related_fields") or []
        if isinstance(fields, str):
            fields = [fields]
        codes |= set(classify_fields(fields, str(reason.get("description") or "")))
    return sorted(codes)
