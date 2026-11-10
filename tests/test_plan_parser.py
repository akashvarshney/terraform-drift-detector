import json

import pytest

from drift_agent.terraform.plan_parser import InvalidPlan, parse_plan


def make_plan(change=None):
    plan = {"format_version": "1.2", "planned_values": {}}
    if change is not None:
        plan["resource_drift"] = [
            {"address": "module.storage.azurerm_storage_account.this", "change": change}
        ]
    return plan


def test_clean_plan():
    assert parse_plan(make_plan()) == {"resources": [], "outputs": []}


def test_reads_drift_not_proposed_changes():
    plan = make_plan(
        {
            "actions": ["update"],
            "before": {"tags": {"owner": "old-owner"}, "id": "/subscriptions/x/y"},
            "after": {"tags": {"owner": "new-owner"}, "id": "/subscriptions/x/y"},
        }
    )
    plan["resource_changes"] = [{"address": "unrelated", "change": {"actions": ["delete"]}}]
    resources = parse_plan(plan)["resources"]
    assert len(resources) == 1
    assert resources[0]["changed_attributes"] == [{"name": "tags", "sensitive": False}]
    assert resources[0]["resource_id"] == "/subscriptions/x/y"
    rendered = json.dumps(resources)
    assert "old-owner" not in rendered
    assert "new-owner" not in rendered


def test_sensitive_and_unmarked_values_are_never_copied():
    result = parse_plan(
        make_plan(
            {
                "actions": ["update"],
                "before": {"password": "old-secret", "settings": [{"key": "old-value"}]},
                "after": {"password": "new-secret", "settings": [{"key": "new-value"}]},
                "after_sensitive": {"password": True},
            }
        )
    )
    attributes = {item["name"]: item["sensitive"] for item in result["resources"][0]["changed_attributes"]}
    assert attributes == {"password": True, "settings": False}
    for value in ("old-secret", "new-secret", "old-value", "new-value"):
        assert value not in json.dumps(result)


def test_whole_sensitive_object_reports_no_keys():
    result = parse_plan(
        make_plan(
            {
                "actions": ["update"],
                "before_sensitive": True,
                "before": {"secret-key": "secret-value"},
                "after": {},
            }
        )
    )
    assert result["resources"][0]["changed_attributes"] == [
        {"name": "[sensitive]", "sensitive": True}
    ]
    assert "secret" not in json.dumps(result)


def test_delete_create_and_replacement():
    for actions, before, after in (
        (["delete"], {"name": "gone"}, None),
        (["create"], None, {"name": "new"}),
        (["delete", "create"], {"name": "old"}, {"name": "new"}),
    ):
        result = parse_plan(make_plan({"actions": actions, "before": before, "after": after}))
        assert result["resources"][0]["actions"] == actions
        assert result["resources"][0]["changed_attributes"] == [
            {"name": "name", "sensitive": False}
        ]


def test_unknown_and_missing_null_attributes():
    result = parse_plan(
        make_plan(
            {
                "actions": ["update"],
                "before": {},
                "after": {"nullable": None},
                "after_unknown": {"settings": [{"value": True}]},
            }
        )
    )
    names = [item["name"] for item in result["resources"][0]["changed_attributes"]]
    assert names == ["nullable", "settings"]


def test_unknown_whole_object():
    result = parse_plan(
        make_plan({"actions": ["update"], "before": {}, "after": {}, "after_unknown": True})
    )
    assert result["resources"][0]["changed_attributes"] == [
        {"name": "[unknown]", "sensitive": False}
    ]


def test_no_op_is_skipped():
    assert parse_plan(make_plan({"actions": ["no-op"]}))["resources"] == []


def test_output_only_changes_without_values():
    plan = make_plan()
    plan["output_changes"] = {
        "endpoint": {"actions": ["update"], "after": "secret-output", "after_sensitive": True},
        "unchanged": {"actions": ["no-op"]},
    }
    assert parse_plan(plan)["outputs"] == [{"name": "endpoint", "actions": ["update"]}]


def test_rejects_malformed_errored_state_and_unknown_versions():
    for plan in (
        [],
        {},
        {"format_version": "2.0", "planned_values": {}},
        {"format_version": "1.0", "values": {}},
        {**make_plan(), "errored": True},
        {**make_plan(), "resource_drift": {}},
        {**make_plan(), "resource_drift": [{}]},
        make_plan({"actions": ["unexpected"]}),
    ):
        with pytest.raises(InvalidPlan):
            parse_plan(plan)
