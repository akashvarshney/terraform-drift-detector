import json
from pathlib import Path

import pytest
from conftest import load_plan

from drift_agent.terraform.runner import TerraformError
from drift_agent.validator import Validator


class StubRunner:
    def __init__(self, *, fail_stage=None, post_plan=None):
        self.fail_stage = fail_stage
        self.post_plan = post_plan if post_plan is not None else load_plan("drift_tags.json")
        self.calls: list[str] = []

    def _fail_if(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise TerraformError(stage, "command_failed", 1)

    def fmt(self, check: bool = True) -> None:
        self.calls.append("fmt")
        self._fail_if("fmt")

    def validate(self) -> None:
        self.calls.append("validate")
        self._fail_if("validate")

    def plan(self, plan_path, var_files) -> int:
        self.calls.append("plan")
        self._fail_if("plan")
        return 2

    def show(self, plan_path) -> str:
        self.calls.append("show")
        self._fail_if("show")
        return json.dumps(self.post_plan)


def validate(tmp_path: Path, runner: StubRunner, *, tflint_enabled: bool = False, approved=None):
    return Validator(lambda workdir: runner, tflint_enabled=tflint_enabled).validate(
        workdir=tmp_path,
        var_files=[],
        approved_addresses=approved
        if approved is not None
        else {"module.storage.azurerm_storage_account.this"},
        plan_directory=tmp_path,
    )


def test_passing_validation_runs_every_step(tmp_path: Path):
    runner = StubRunner()
    result = validate(tmp_path, runner)
    assert result.passed is True
    assert result.reject_reason is None
    assert [step.name for step in result.steps] == [
        "terraform_fmt",
        "terraform_validate",
        "tflint",
        "terraform_plan",
        "post_change_plan",
    ]
    assert runner.calls == ["fmt", "fmt", "validate", "plan", "show"]


def test_fmt_failure_rejects(tmp_path: Path):
    result = validate(tmp_path, StubRunner(fail_stage="fmt"))
    assert result.passed is False
    assert result.reject_reason == "terraform_fmt_failed"


def test_validate_failure_rejects(tmp_path: Path):
    result = validate(tmp_path, StubRunner(fail_stage="validate"))
    assert result.reject_reason == "terraform_validate_failed"


def test_plan_failure_rejects(tmp_path: Path):
    result = validate(tmp_path, StubRunner(fail_stage="plan"))
    assert result.reject_reason == "post_plan_failed"


def test_unrelated_post_change_is_rejected(tmp_path: Path):
    post = load_plan("drift_tags.json")
    post["resource_drift"].append(
        {
            "address": "module.network.azurerm_virtual_network.this",
            "change": {"actions": ["update"], "before": {}, "after": {}, "after_unknown": {}},
        }
    )
    result = validate(tmp_path, StubRunner(post_plan=post))
    assert result.passed is False
    assert result.reject_reason == "unrelated_changes_in_post_plan"


def test_destructive_post_change_is_rejected(tmp_path: Path):
    post = {
        "format_version": "1.2",
        "planned_values": {},
        "resource_drift": [
            {
                "address": "module.storage.azurerm_storage_account.this",
                "change": {"actions": ["delete"], "before": {"id": "x"}, "after": None},
            }
        ],
    }
    result = validate(tmp_path, StubRunner(post_plan=post))
    assert result.reject_reason == "destructive_change_in_post_plan"


def test_unparseable_post_plan_is_rejected(tmp_path: Path):
    runner = StubRunner()
    runner.show = lambda plan_path: "not json"  # type: ignore[method-assign]
    result = validate(tmp_path, runner)
    assert result.reject_reason == "post_plan_unparseable"


def test_tflint_is_optional(tmp_path: Path):
    disabled = validate(tmp_path, StubRunner(), tflint_enabled=False)
    assert disabled.steps[2].detail == "not enabled"
    enabled = validate(tmp_path, StubRunner(), tflint_enabled=True)
    assert enabled.steps[2].name == "tflint"
    assert "skipped" in enabled.steps[2].detail or enabled.steps[2].passed


@pytest.mark.parametrize("approved", [set(), {"other.address"}])
def test_approved_address_set_drives_comparison(tmp_path: Path, approved):
    result = validate(tmp_path, StubRunner(), approved=approved)
    assert result.passed is False
