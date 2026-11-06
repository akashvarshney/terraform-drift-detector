import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from drift_agent.terraform.runner import TerraformError, TerraformRunner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.runner = TerraformRunner(Path.cwd(), timeout=7)

    @patch("drift_agent.terraform.runner.subprocess.run")
    def test_fixed_commands_and_noninteractive_execution(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="{}")
        self.runner.init([Path("backend config.hcl")])
        self.runner.detect_drift(Path("scan.tfplan"), [Path("test vars.tfvars")])
        self.runner.show(Path("scan.tfplan"))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["init", "plan", "show"])
        self.assertIn("-backend-config=backend config.hcl", commands[0])
        self.assertIn("-refresh-only", commands[1])
        self.assertIn("-detailed-exitcode", commands[1])
        self.assertIn("-var-file=test vars.tfvars", commands[1])
        self.assertIn("-lock-timeout=60s", commands[1])
        self.assertIn("-json", commands[2])
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 7)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(run.call_args_list[0].kwargs["stdout"], subprocess.DEVNULL)

    @patch("drift_agent.terraform.runner.subprocess.run")
    def test_plan_exit_codes(self, run):
        for exit_code in (0, 2):
            run.return_value = subprocess.CompletedProcess([], exit_code)
            self.assertEqual(self.runner.detect_drift(Path("plan"), []), exit_code)
        for exit_code in (1, 3):
            run.return_value = subprocess.CompletedProcess([], exit_code, stderr="secret")
            with self.assertRaises(TerraformError) as caught:
                self.runner.detect_drift(Path("plan"), [])
            self.assertEqual(caught.exception.exit_code, exit_code)
            self.assertNotIn("secret", str(caught.exception))

    @patch("drift_agent.terraform.runner.subprocess.run")
    def test_timeout_and_missing_binary(self, run):
        for error, reason in (
            (subprocess.TimeoutExpired("secret command", 7, output="secret"), "timeout"),
            (FileNotFoundError("secret path"), "could_not_start"),
        ):
            run.side_effect = error
            with self.assertRaises(TerraformError) as caught:
                self.runner.init([])
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("secret", str(caught.exception))

    def test_environment_cannot_inject_cli_arguments_or_logging(self):
        with patch.dict(os.environ, {
            "TF_CLI_ARGS_plan": "-target=some.resource", "TF_LOG": "TRACE",
            "TF_LOG_PATH": "raw-secrets.log", "TF_DATA_DIR": "shared-data",
            "ARM_CLIENT_ID": "test-identity", "TF_VAR_region": "test-region",
        }):
            runner = TerraformRunner(Path.cwd(), workspace="pilot")
        self.assertFalse(any(name.startswith(("TF_CLI_ARGS", "TF_LOG")) for name in runner.environment))
        self.assertEqual(runner.environment["TF_DATA_DIR"], str(Path.cwd() / ".terraform"))
        self.assertEqual(runner.environment["TF_WORKSPACE"], "pilot")
        self.assertEqual(runner.environment["ARM_CLIENT_ID"], "test-identity")
        normalized = {key.upper(): value for key, value in runner.environment.items()}
        self.assertEqual(normalized["TF_VAR_REGION"], "test-region")

    def test_apply_is_not_allowed(self):
        with self.assertRaises(ValueError):
            self.runner._run("apply", [], (0,))


if __name__ == "__main__":
    unittest.main()