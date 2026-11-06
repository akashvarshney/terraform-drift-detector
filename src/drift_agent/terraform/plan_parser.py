"""Reduce refresh-only plans without retaining attribute or output values."""

from typing import Any


class InvalidPlan(ValueError):
    pass


def _has_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_unknown(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_unknown(child) for child in value)
    return value is True


def _actions(change: Any) -> list[str]:
    if not isinstance(change, dict):
        raise InvalidPlan("Invalid change object.")
    actions = change.get("actions")
    allowed = {"no-op", "create", "read", "update", "delete", "forget"}
    if not isinstance(actions, list) or not actions:
        raise InvalidPlan("Missing change actions.")
    if any(not isinstance(action, str) or action not in allowed for action in actions):
        raise InvalidPlan("Unsupported change actions.")
    return actions


def _changed_attributes(change: dict[str, Any]) -> list[str]:
    if change.get("before_sensitive") is True or change.get("after_sensitive") is True:
        return ["[sensitive]"]
    before = change.get("before")
    after = change.get("after")
    unknown = change.get("after_unknown", {})
    if before is not None and not isinstance(before, dict):
        raise InvalidPlan("Invalid resource before value.")
    if after is not None and not isinstance(after, dict):
        raise InvalidPlan("Invalid resource after value.")
    if unknown is True:
        return ["[unknown]"]
    if not isinstance(unknown, dict):
        raise InvalidPlan("Invalid resource unknown values.")
    before = before or {}
    after = after or {}
    missing = object()
    return sorted(
        name
        for name in before.keys() | after.keys() | unknown.keys()
        if before.get(name, missing) != after.get(name, missing)
        or _has_unknown(unknown.get(name))
    )


def parse_plan(plan: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(plan, dict):
        raise InvalidPlan("Expected a plan object.")
    version = plan.get("format_version")
    if not isinstance(version, str) or version.split(".")[0] != "1":
        raise InvalidPlan("Unsupported Terraform JSON format version.")
    if "planned_values" not in plan or plan.get("errored", False) is not False:
        raise InvalidPlan("Expected a successful saved plan, not state JSON.")
    drift = plan.get("resource_drift", [])
    outputs = plan.get("output_changes", {})
    if not isinstance(drift, list) or not isinstance(outputs, dict):
        raise InvalidPlan("Invalid plan changes.")

    resources = []
    for resource in drift:
        if not isinstance(resource, dict) or not isinstance(resource.get("address"), str):
            raise InvalidPlan("Missing drift resource address.")
        change = resource.get("change")
        actions = _actions(change)
        if actions == ["no-op"]:
            continue
        resources.append({
            "address": resource["address"],
            "actions": actions,
            "changed_attributes": _changed_attributes(change),
        })

    changed_outputs = []
    for name, change in outputs.items():
        actions = _actions(change)
        if actions != ["no-op"]:
            changed_outputs.append({"name": name, "actions": actions})
    return {
        "resources": sorted(resources, key=lambda resource: resource["address"]),
        "outputs": sorted(changed_outputs, key=lambda output: output["name"]),
    }