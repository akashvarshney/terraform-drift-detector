"""Fixed, noninteractive Terraform commands for detection and validation.

There is no generic shell bridge: only these reviewed stages exist, and each one
is invoked with a fixed argument shape. Raw stdout/stderr never reaches logs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ALLOWED_STAGES = frozenset({"init", "plan", "show", "fmt", "validate"})


class TerraformError(RuntimeError):
    def __init__(self, stage: str, reason: str, exit_code: int | None = None):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code


class TerraformRunner:
    def __init__(self, workdir: Path, timeout: int = 600, workspace: str = "default"):
        if timeout <= 0:
            raise ValueError("Timeout must be positive.")
        self.workdir = workdir
        self.timeout = timeout
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("TF_CLI_ARGS", "TF_LOG"))
        }
        self.environment.update({
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "TF_DATA_DIR": str(workdir / ".terraform"),
            "TF_WORKSPACE": workspace,
        })

    def _run(
        self,
        stage: str,
        arguments: list[str],
        accepted: tuple[int, ...],
        capture: bool = False,
    ) -> subprocess.CompletedProcess:
        if stage not in ALLOWED_STAGES:
            raise ValueError("Terraform command is not allowed.")
        try:
            result = subprocess.run(
                ["terraform", stage, *arguments],
                cwd=self.workdir,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TerraformError(stage, "timeout") from None
        except OSError:
            raise TerraformError(stage, "could_not_start") from None
        if result.returncode not in accepted:
            raise TerraformError(stage, "command_failed", result.returncode)
        return result

    def init(self, backend_configs: list[Path], upgrade: bool = False) -> None:
        arguments = ["-input=false", "-no-color",
                     *(f"-backend-config={path}" for path in backend_configs)]
        if upgrade:
            arguments.append("-upgrade")
        self._run("init", arguments, (0,))

    def detect_drift(self, plan_path: Path, var_files: list[Path]) -> int:
        """Refresh-only plan; 0 means clean, 2 means drift was found."""
        result = self._run("plan", [
            "-refresh-only", "-detailed-exitcode", "-input=false", "-no-color",
            "-lock-timeout=60s", f"-out={plan_path}",
            *(f"-var-file={path}" for path in var_files),
        ], (0, 2))
        return result.returncode

    def plan(self, plan_path: Path, var_files: list[Path]) -> int:
        """Ordinary plan used to prove a remediation is the only change."""
        result = self._run("plan", [
            "-input=false", "-no-color", "-lock-timeout=60s", f"-out={plan_path}",
            *(f"-var-file={path}" for path in var_files),
        ], (0, 2))
        return result.returncode

    def show(self, plan_path: Path) -> str:
        return self._run("show", ["-json", "-no-color", str(plan_path)], (0,), capture=True).stdout

    def fmt(self, check: bool = True) -> None:
        """`terraform fmt`; check mode fails on unformatted files."""
        arguments = ["-no-color", "-recursive"]
        if check:
            arguments.append("-check")
        self._run("fmt", arguments, (0,))

    def validate(self) -> None:
        self._run("validate", ["-no-color"], (0,))
