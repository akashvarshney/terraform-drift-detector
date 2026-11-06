"""Fixed, noninteractive Terraform commands for detection only."""

import os
from pathlib import Path
import subprocess


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

    def _run(self, stage: str, arguments: list[str], accepted: tuple[int, ...]) -> subprocess.CompletedProcess:
        if stage not in {"init", "plan", "show"}:
            raise ValueError("Terraform command is not allowed.")
        try:
            result = subprocess.run(
                ["terraform", stage, *arguments],
                cwd=self.workdir,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if stage == "show" else subprocess.DEVNULL,
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

    def init(self, backend_configs: list[Path]) -> None:
        self._run("init", [
            "-input=false", "-no-color",
            *(f"-backend-config={path}" for path in backend_configs),
        ], (0,))

    def detect_drift(self, plan_path: Path, var_files: list[Path]) -> int:
        result = self._run("plan", [
            "-refresh-only", "-detailed-exitcode", "-input=false", "-no-color",
            "-lock-timeout=60s", f"-out={plan_path}",
            *(f"-var-file={path}" for path in var_files),
        ], (0, 2))
        return result.returncode

    def show(self, plan_path: Path) -> str:
        return self._run("show", ["-json", "-no-color", str(plan_path)], (0,)).stdout