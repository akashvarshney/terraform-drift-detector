"""Post-change validation: the patch must prove itself or be discarded."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from drift_agent.models import ValidationResult, ValidationStep
from drift_agent.terraform.plan_parser import InvalidPlan, parse_plan
from drift_agent.terraform.runner import TerraformError, TerraformRunner

DESTRUCTIVE_ACTIONS = {"delete", "forget"}


class Validator:
    def __init__(
        self,
        runner_factory: Callable[[Path], TerraformRunner],
        *,
        tflint_enabled: bool = False,
    ) -> None:
        self.runner_factory = runner_factory
        self.tflint_enabled = tflint_enabled

    def validate(
        self,
        *,
        workdir: Path,
        var_files: list[Path],
        approved_addresses: set[str],
        plan_directory: Path,
    ) -> ValidationResult:
        steps: list[ValidationStep] = []
        runner = self.runner_factory(workdir)

        try:
            runner.fmt(check=False)
            runner.fmt(check=True)
        except TerraformError as error:
            steps.append(ValidationStep(name="terraform_fmt", passed=False, detail=error.reason))
            return ValidationResult.rejected("terraform_fmt_failed", steps)
        steps.append(ValidationStep(name="terraform_fmt", passed=True))

        try:
            runner.validate()
        except TerraformError as error:
            steps.append(ValidationStep(name="terraform_validate", passed=False, detail=error.reason))
            return ValidationResult.rejected("terraform_validate_failed", steps)
        steps.append(ValidationStep(name="terraform_validate", passed=True))

        steps.append(self._tflint(workdir))

        post_plan = plan_directory / "post-change.tfplan"
        try:
            runner.plan(post_plan, var_files)
        except TerraformError as error:
            steps.append(ValidationStep(name="terraform_plan", passed=False, detail=error.reason))
            return ValidationResult.rejected("post_plan_failed", steps)
        steps.append(ValidationStep(name="terraform_plan", passed=True))

        try:
            parsed = parse_plan(json.loads(runner.show(post_plan)))
        except (TerraformError, json.JSONDecodeError, InvalidPlan):
            steps.append(
                ValidationStep(name="post_change_plan", passed=False, detail="unparseable")
            )
            return ValidationResult.rejected("post_plan_unparseable", steps)

        addresses = [resource["address"] for resource in parsed["resources"]]
        unexpected = [address for address in addresses if address not in approved_addresses]
        if unexpected:
            steps.append(
                ValidationStep(
                    name="post_change_plan", passed=False, detail=f"unexpected: {unexpected[0]}"
                )
            )
            return ValidationResult.rejected("unrelated_changes_in_post_plan", steps)

        destructive = [
            resource["address"]
            for resource in parsed["resources"]
            if DESTRUCTIVE_ACTIONS & set(resource["actions"])
        ]
        if destructive:
            steps.append(
                ValidationStep(
                    name="post_change_plan", passed=False, detail=f"destructive: {destructive[0]}"
                )
            )
            return ValidationResult.rejected("destructive_change_in_post_plan", steps)

        steps.append(
            ValidationStep(
                name="post_change_plan", passed=True, detail=f"{len(addresses)} approved change(s)"
            )
        )
        return ValidationResult(passed=True, steps=steps)

    def _tflint(self, workdir: Path) -> ValidationStep:
        if not self.tflint_enabled:
            return ValidationStep(name="tflint", passed=True, detail="not enabled")
        binary = shutil.which("tflint")
        if not binary:
            return ValidationStep(name="tflint", passed=True, detail="tflint not installed; skipped")
        try:
            result = subprocess.run(
                [binary, "--no-color"],
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ValidationStep(name="tflint", passed=False, detail="tflint_failed_to_run")
        return ValidationStep(
            name="tflint",
            passed=result.returncode == 0,
            detail="" if result.returncode == 0 else f"exit {result.returncode}",
        )
