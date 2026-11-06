import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drift_agent.main import main, scan
from drift_agent.terraform.runner import TerraformError


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        (self.repository / "main.tf").write_text("terraform {}", encoding="utf-8")
        self.runner_patch = patch("drift_agent.main.TerraformRunner")
        self.runner_class = self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)
        self.runner = self.runner_class.return_value
        self.runner.detect_drift.return_value = 0

    def test_clean_does_not_show_and_cleans_temporary_workspace(self):
        report = scan(self.repository)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["terraform_exit_code"], 0)
        self.runner.show.assert_not_called()
        workdir = self.runner_class.call_args.args[0]
        self.assertNotEqual(workdir, self.repository)
        self.assertFalse(workdir.exists())
        self.assertEqual(list(self.repository.iterdir()), [self.repository / "main.tf"])

    def test_drift_and_value_free_output(self):
        self.runner.detect_drift.return_value = 2
        self.runner.show.return_value = json.dumps({
            "format_version": "1.2", "planned_values": {},
            "resource_drift": [{"address": "module.test.resource.example", "change": {
                "actions": ["update"], "before": {"password": "old-secret"},
                "after": {"password": "new-secret"},
            }}],
        })
        report = scan(self.repository)
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["terraform_exit_code"], 2)
        self.assertEqual(len(report["plan_json_sha256"]), 64)
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(report["scan_id"])

    def test_terraform_errors_are_distinct_and_cleanup_runs(self):
        for stage in ("init", "plan", "show"):
            with self.subTest(stage=stage):
                self.runner.init.side_effect = None
                self.runner.detect_drift.side_effect = None
                self.runner.show.side_effect = None
                self.runner.detect_drift.return_value = 2
                method = {"init": self.runner.init, "plan": self.runner.detect_drift, "show": self.runner.show}[stage]
                method.side_effect = TerraformError(stage, "command_failed", 1)
                report = scan(self.repository)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["error"]["stage"], stage)
                self.assertFalse(self.runner_class.call_args.args[0].exists())

    def test_invalid_json_is_not_reported_as_clean_or_drift(self):
        self.runner.detect_drift.return_value = 2
        for value in ("not json secret", '{"format_version":"2.0"}'):
            self.runner.show.return_value = value
            report = scan(self.repository)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["error"]["reason"], "invalid_plan_json")
            self.assertNotIn("secret", json.dumps(report))

    def test_rejects_missing_configuration_and_escaping_paths(self):
        for root in ("missing", "..", str(self.repository.resolve())):
            self.assertEqual(scan(self.repository, terraform_root=root)["status"], "error")
        self.assertEqual(scan(self.repository, var_files=["../outside.tfvars"])["status"], "error")
        self.runner.init.assert_not_called()

    def test_nested_root_keeps_sibling_modules_and_input_files(self):
        root = self.repository / "environments" / "pilot"
        root.mkdir(parents=True)
        (root / "main.tf").write_text("terraform {}", encoding="utf-8")
        (root / "pilot.tfvars").write_text("", encoding="utf-8")
        modules = self.repository / "modules"
        modules.mkdir()
        (modules / "main.tf").write_text("terraform {}", encoding="utf-8")

        def inspect_copy(*arguments):
            workdir = self.runner_class.call_args.args[0]
            self.assertTrue((workdir.parent.parent / "modules" / "main.tf").exists())
            self.assertTrue((workdir / "pilot.tfvars").exists())

        self.runner.init.side_effect = inspect_copy
        report = scan(self.repository, "environments/pilot", var_files=["pilot.tfvars"])
        self.assertEqual(report["status"], "clean")

    @patch("drift_agent.main.scan")
    def test_cli_exit_codes_are_job_friendly(self, mocked_scan):
        for status, flags, expected in (
            ("clean", [], 0), ("drift", [], 0), ("error", [], 1),
            ("drift", ["--fail-on-drift"], 2),
        ):
            mocked_scan.return_value = {"status": status}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--repo", str(self.repository), *flags])
            self.assertEqual(result, expected)
            self.assertEqual(json.loads(output.getvalue())["status"], status)


if __name__ == "__main__":
    unittest.main()