"""One checkout, one observe-only scan, one JSON report."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
from uuid import uuid4

from drift_agent.terraform.plan_parser import InvalidPlan, parse_plan
from drift_agent.terraform.runner import TerraformError, TerraformRunner


def _inside(repository: Path, base: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ValueError("Expected a relative path.")
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(repository):
        raise ValueError("Path is outside the checkout.")
    return resolved


def scan(
    repository: Path,
    terraform_root: str = ".",
    workspace: str = "default",
    var_files: list[str] | None = None,
    backend_configs: list[str] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scan_id": str(uuid4()),
        "repository": repository.name,
        "terraform_root": terraform_root,
        "workspace": workspace,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "mode": "observe",
        "status": "error",
        "terraform_exit_code": None,
        "resources": [],
        "outputs": [],
    }
    stage = "prepare"
    try:
        source = repository.resolve(strict=True)
        source_root = _inside(source, source, terraform_root)
        if not source.is_dir() or not source_root.is_dir():
            raise ValueError("Checkout or Terraform root is not a directory.")
        if not any(source_root.glob("*.tf")) and not any(source_root.glob("*.tf.json")):
            raise ValueError("No Terraform configuration found.")
        with tempfile.TemporaryDirectory(prefix="drift-agent-") as temporary:
            checkout = Path(temporary).resolve() / "repo"
            shutil.copytree(
                source, checkout,
                ignore=shutil.ignore_patterns(".git", ".terraform", ".venv", "__pycache__", "*.tfplan"),
            )
            workdir = _inside(checkout, checkout, terraform_root)
            variable_paths = [_inside(checkout, workdir, name) for name in (var_files or [])]
            backend_paths = [_inside(checkout, workdir, name) for name in (backend_configs or [])]
            if any(not path.is_file() for path in variable_paths + backend_paths):
                raise ValueError("Input file does not exist.")
            runner = TerraformRunner(workdir, timeout, workspace)
            runner.init(backend_paths)
            plan_path = Path(temporary) / "drift.tfplan"
            record["terraform_exit_code"] = runner.detect_drift(plan_path, variable_paths)
            if record["terraform_exit_code"] == 0:
                record["status"] = "clean"
            else:
                stage = "parse"
                raw_plan = runner.show(plan_path)
                record.update(parse_plan(json.loads(raw_plan)))
                record["plan_json_sha256"] = hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
                record["status"] = "drift"
    except TerraformError as error:
        if error.stage == "plan":
            record["terraform_exit_code"] = error.exit_code
        record["error"] = {
            "stage": error.stage, "reason": error.reason, "exit_code": error.exit_code,
        }
    except (InvalidPlan, json.JSONDecodeError):
        record["error"] = {"stage": "parse", "reason": "invalid_plan_json"}
    except (OSError, ValueError, shutil.Error):
        record["status"] = "error"
        record["error"] = {"stage": stage, "reason": "invalid_input_or_workspace_io"}
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe-only Terraform drift scan; prints reduced JSON.")
    parser.add_argument("--repo", type=Path, required=True, help="Path to a trusted, existing checkout.")
    parser.add_argument("--terraform-root", default=".", help="Terraform root relative to the checkout.")
    parser.add_argument("--workspace", default="default", help="Existing Terraform workspace to scan.")
    parser.add_argument("--var-file", action="append", default=[], help="File relative to the Terraform root; repeatable.")
    parser.add_argument("--backend-config", action="append", default=[], help="Config file relative to the Terraform root; repeatable.")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum seconds per Terraform command.")
    parser.add_argument("--fail-on-drift", action="store_true", help="Exit 2 on drift; omit for Container Apps Jobs.")
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    record = scan(
        arguments.repo, arguments.terraform_root, arguments.workspace,
        arguments.var_file, arguments.backend_config, arguments.timeout,
    )
    print(json.dumps(record, separators=(",", ":")))
    if record["status"] == "error":
        return 1
    return 2 if arguments.fail_on_drift and record["status"] == "drift" else 0


if __name__ == "__main__":
    raise SystemExit(main())