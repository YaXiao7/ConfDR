"""Extended resource accounting shared by collection and candidate reconstruction."""

import re
from decimal import Decimal, InvalidOperation


def extended_quantity(value) -> int:
    try:
        text = str(value)
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKMGTPE]|[KMGTPE]i|m)?", text)
        if match:
            suffix = match[2] or ""
            factors = {"": 1, "m": Decimal("0.001"), "k": 1000, "K": 1000}
            factors.update({unit: 1000**power for power, unit in enumerate("MGTPE", 2)})
            factors.update({unit + "i": 1024**power for power, unit in enumerate("KMGTPE", 1)})
            amount = Decimal(match[1]) * factors[suffix]
        else:
            amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid extended resource quantity: {value!r}") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValueError(f"Extended resources require nonnegative integer units: {value!r}")
    return int(amount)


def extended_requests(spec: dict) -> dict[str, int]:
    containers = [*spec.get("containers", []), *spec.get("initContainers", [])]
    names = {
        name
        for container in containers
        for field in ("requests", "limits")
        for name in (container.get("resources") or {}).get(field, {})
        if "/" in name
    } | {name for name in spec.get("overhead", {}) if "/" in name}

    def request(container, name):
        resources = container.get("resources") or {}
        req, limits = resources.get("requests", {}), resources.get("limits", {})
        value = extended_quantity(req.get(name, limits.get(name, 0)))
        if name in req and name in limits and value != extended_quantity(limits[name]):
            raise ValueError(f"Extended resource requests and limits must agree: {name}")
        return value

    result = {}
    for name in sorted(names):
        running = sum(request(c, name) for c in spec.get("containers", []))
        sidecars, peak = 0, 0
        for c in spec.get("initContainers", []):
            amount = request(c, name)
            if c.get("restartPolicy") == "Always":
                sidecars += amount
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + amount)
        result[name] = max(running + sidecars, peak) + extended_quantity(
            spec.get("overhead", {}).get(name, 0)
        )
    return result


def populate_resources(world, case):
    pods = case["WaitingDeployPods"]
    nodes = case["ClusterState"]["Nodes"]
    configured = set(case.get("gpu_resource_names", []))
    world.domain_missing["Resource"] = [
        f"WaitingDeployPods/{p}/extended_requests"
        for p, pod in pods.items()
        if "extended_requests" not in pod
    ]
    names = (
        configured
        | {r for p in pods.values() for r in p.get("extended_requests", {})}
        | {r for n in nodes.values() for r in n.get("extended_free", {})}
    )
    world.domains["Resource"] = sorted(names)
    for r in names:
        world.values["is_gpu_resource", r] = (
            r in configured
            or r in {"nvidia.com/gpu", "amd.com/gpu", "gpu.intel.com/i915", "gpu.intel.com/xe"}
            or r.startswith("nvidia.com/mig-")
        )
        for p, pod in pods.items():
            world.values["resource_request", p, r] = (
                extended_quantity(pod["extended_requests"].get(r, 0)) if "extended_requests" in pod else None
            )
        for n, node in nodes.items():
            world.values["resource_free", n, r] = (
                extended_quantity(node["extended_free"].get(r, 0)) if "extended_free" in node else None
            )
