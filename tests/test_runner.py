import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from drift_agent.terraform.runner import TerraformError, TerraformRunner


@pytest.fixture
def runner(tmp_path: Path) -> TerraformRunner:
    return TerraformRunner(tmp_path, timeout=7)


@patch("drift_agent.terraform.runner.subprocess.run")
def test_fixed_commands_and_noninteractive_execution(run, runner: TerraformRunner):
    run.return_value = subprocess.CompletedProcess([], 0, stdout="{}")
    runner.init([Path("backend config.hcl")])
    runner.detect_drift(Path("scan.tfplan"), [Path("test vars.tfvars")])
    runner.show(Path("scan.tfplan"))
    runner.fmt(check=True)
    runner.plan(Path("post.tfplan"), [])
    runner.validate()

    commands = [call.args[0] for call in run.call_args_list]
    assert [command[1] for command in commands] == [
        "init", "plan", "show", "fmt", "plan", "validate",
    ]
    assert "-backend-config=backend config.hcl" in commands[0]
    assert "-refresh-only" in commands[1]
    assert "-detailed-exitcode" in commands[1]
    assert "-var-file=test vars.tfvars" in commands[1]
    assert "-lock-timeout=60s" in commands[1]
    assert "-json" in commands[2]
    assert "-check" in commands[3]
    assert "-refresh-only" not in commands[4]
    for call in run.call_args_list:
        assert call.kwargs["shell"] is False
        assert call.kwargs["timeout"] == 7
        assert call.kwargs["stderr"] is subprocess.DEVNULL
        assert call.kwargs["stdin"] is subprocess.DEVNULL
    assert run.call_args_list[0].kwargs["stdout"] is subprocess.DEVNULL
    assert run.call_args_list[2].kwargs["stdout"] is subprocess.PIPE


@patch("drift_agent.terraform.runner.subprocess.run")
def test_plan_exit_codes(run, runner: TerraformRunner):
    for exit_code in (0, 2):
        run.return_value = subprocess.CompletedProcess([], exit_code)
        assert runner.detect_drift(Path("plan"), []) == exit_code
    for exit_code in (1, 3):
        run.return_value = subprocess.CompletedProcess([], exit_code, stderr="secret")
        with pytest.raises(TerraformError) as caught:
            runner.detect_drift(Path("plan"), [])
        assert caught.value.exit_code == exit_code
        assert "secret" not in str(caught.value)


@patch("drift_agent.terraform.runner.subprocess.run")
def test_timeout_and_missing_binary(run, runner: TerraformRunner):
    for error, reason in (
        (subprocess.TimeoutExpired("secret command", 7, output="secret"), "timeout"),
        (FileNotFoundError("secret path"), "could_not_start"),
    ):
        run.side_effect = error
        with pytest.raises(TerraformError) as caught:
            runner.init([])
        assert caught.value.reason == reason
        assert "secret" not in str(caught.value)


def test_environment_cannot_inject_cli_arguments_or_logging(tmp_path: Path):
    with patch.dict(
        os.environ,
        {
            "TF_CLI_ARGS_plan": "-target=some.resource",
            "TF_LOG": "TRACE",
            "TF_LOG_PATH": "raw-secrets.log",
            "TF_DATA_DIR": "shared-data",
            "ARM_CLIENT_ID": "test-identity",
            "TF_VAR_region": "test-region",
        },
    ):
        runner = TerraformRunner(tmp_path, workspace="pilot")
    assert not any(name.startswith(("TF_CLI_ARGS", "TF_LOG")) for name in runner.environment)
    assert runner.environment["TF_DATA_DIR"] == str(tmp_path / ".terraform")
    assert runner.environment["TF_WORKSPACE"] == "pilot"
    assert runner.environment["ARM_CLIENT_ID"] == "test-identity"
    assert runner.environment["TF_VAR_region"] == "test-region"
    assert runner.environment["TF_IN_AUTOMATION"] == "1"
    assert runner.environment["TF_INPUT"] == "0"


def test_apply_is_not_allowed(runner: TerraformRunner):
    for stage in ("apply", "destroy", "import", "state"):
        with pytest.raises(ValueError):
            runner._run(stage, [], (0,))


def test_timeout_must_be_positive(tmp_path: Path):
    with pytest.raises(ValueError):
        TerraformRunner(tmp_path, timeout=0)
