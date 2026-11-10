"""Reduce refresh-only plans to metadata without retaining any values.

Emitted per resource: address, resource id (needed for Azure Activity
correlation), actions, and the *names* of changed top-level attributes. No
attribute value, output value or nested payload is ever copied forward.
"""

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


def _sensitive_attribute_names(change: dict[str, Any]) -> list[str]:
    """Per-attribute sensitivity markers, used to flag names without values."""
    names: list[str] = []
    for key in ("before_sensitive", "after_sensitive"):
        marker = change.get(key)
        if isinstance(marker, dict):
            names.extend(name for name, flag in marker.items() if _has_unknown(flag))
    return names


def _changed_attributes(change: dict[str, Any]) -> list[dict[str, Any]]:
    if change.get("before_sensitive") is True or change.get("after_sensitive") is True:
        return [{"name": "[sensitive]", "sensitive": True}]
    before = change.get("before")
    after = change.get("after")
    unknown = change.get("after_unknown", {})
    if before is not None and not isinstance(before, dict):
        raise InvalidPlan("Invalid resource before value.")
    if after is not None and not isinstance(after, dict):
        raise InvalidPlan("Invalid resource after value.")
    if unknown is True:
        return [{"name": "[unknown]", "sensitive": False}]
    if not isinstance(unknown, dict):
        raise InvalidPlan("Invalid resource unknown values.")
    before = before or {}
    after = after or {}
    missing = object()
    sensitive = {name.casefold() for name in _sensitive_attribute_names(change)}
    changed = sorted(
        name
        for name in before.keys() | after.keys() | unknown.keys()
        if before.get(name, missing) != after.get(name, missing)
        or _has_unknown(unknown.get(name))
    )
    return [
        {"name": name, "sensitive": name.casefold() in sensitive} for name in changed
    ]


def _resource_id(change: dict[str, Any]) -> str | None:
    """The Azure resource id, needed to correlate Activity Log evidence.

    Only the `id` attribute is read, and only as an identifier: it is metadata,
    never a plan value used for explanation.
    """
    for key in ("before", "after"):
        candidate = change.get(key)
        if isinstance(candidate, dict):
            value = candidate.get("id")
            if isinstance(value, str) and value.startswith("/"):
                return value
    return None


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
            "resource_id": _resource_id(change),
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
