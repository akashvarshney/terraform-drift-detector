import json
import unittest

from drift_agent.terraform.plan_parser import InvalidPlan, parse_plan


def make_plan(change=None):
    plan = {"format_version": "1.2", "planned_values": {}}
    if change is not None:
        plan["resource_drift"] = [{
            "address": "module.storage.azurerm_storage_account.this",
            "change": change,
        }]
    return plan


class PlanParserTests(unittest.TestCase):
    def test_clean_plan(self):
        self.assertEqual(parse_plan(make_plan()), {"resources": [], "outputs": []})

    def test_reads_drift_not_proposed_resource_changes(self):
        plan = make_plan({
            "actions": ["update"],
            "before": {"tags": {"owner": "old-owner"}, "id": "resource-id"},
            "after": {"tags": {"owner": "new-owner"}, "id": "resource-id"},
        })
        plan["resource_changes"] = [{"address": "unrelated", "change": {"actions": ["delete"]}}]
        resources = parse_plan(plan)["resources"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["changed_attributes"], ["tags"])
        self.assertNotIn("owner", json.dumps(resources))
        self.assertNotIn("resource-id", json.dumps(resources))

    def test_sensitive_and_unmarked_values_are_never_copied(self):
        plan = make_plan({
            "actions": ["update"],
            "before": {"password": "old-secret", "settings": [{"key": "old-value"}]},
            "after": {"password": "new-secret", "settings": [{"key": "new-value"}]},
            "after_sensitive": {"password": True},
        })
        result = parse_plan(plan)
        self.assertEqual(result["resources"][0]["changed_attributes"], ["password", "settings"])
        for value in ("old-secret", "new-secret", "old-value", "new-value"):
            self.assertNotIn(value, json.dumps(result))

    def test_whole_sensitive_object_does_not_expose_keys(self):
        result = parse_plan(make_plan({
            "actions": ["update"], "before_sensitive": True,
            "before": {"secret-key": "secret-value"}, "after": {},
        }))
        self.assertEqual(result["resources"][0]["changed_attributes"], ["[sensitive]"])
        self.assertNotIn("secret", json.dumps(result))

    def test_delete_create_and_replacement(self):
        for actions, before, after in (
            (["delete"], {"name": "gone"}, None),
            (["create"], None, {"name": "new"}),
            (["delete", "create"], {"name": "old"}, {"name": "new"}),
        ):
            with self.subTest(actions=actions):
                result = parse_plan(make_plan({"actions": actions, "before": before, "after": after}))
                self.assertEqual(result["resources"][0]["actions"], actions)
                self.assertEqual(result["resources"][0]["changed_attributes"], ["name"])

    def test_unknown_and_missing_null_attributes(self):
        result = parse_plan(make_plan({
            "actions": ["update"], "before": {}, "after": {"nullable": None},
            "after_unknown": {"settings": [{"value": True}]},
        }))
        self.assertEqual(result["resources"][0]["changed_attributes"], ["nullable", "settings"])

    def test_no_op_is_skipped(self):
        self.assertEqual(parse_plan(make_plan({"actions": ["no-op"]}))["resources"], [])

    def test_output_only_changes_without_values(self):
        plan = make_plan()
        plan["output_changes"] = {
            "endpoint": {"actions": ["update"], "after": "secret-output", "after_sensitive": True},
            "unchanged": {"actions": ["no-op"]},
        }
        self.assertEqual(parse_plan(plan)["outputs"], [{"name": "endpoint", "actions": ["update"]}])

    def test_rejects_malformed_errored_state_and_unknown_versions(self):
        for plan in (
            [], {}, {"format_version": "2.0", "planned_values": {}},
            {"format_version": "1.0", "values": {}},
            {**make_plan(), "errored": True},
            {**make_plan(), "resource_drift": {}},
            {**make_plan(), "resource_drift": [{}]},
            make_plan({"actions": ["unexpected"]}),
        ):
            with self.subTest(plan=plan), self.assertRaises(InvalidPlan):
                parse_plan(plan)


if __name__ == "__main__":
    unittest.main()