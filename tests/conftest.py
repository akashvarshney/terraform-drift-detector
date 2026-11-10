"""Shared fixtures and fakes for the test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drift_agent.ai.foundry_client import FakeFoundryClient
from drift_agent.audit.store import InMemoryAuditStore
from drift_agent.azure.activity import NullActivityProvider
from drift_agent.github.client import FakeGitHubClient
from drift_agent.models import Mode, RepositoryConfig
from drift_agent.orchestrator import DriftOrchestrator
from drift_agent.policy import RiskPolicy
from drift_agent.settings import Settings
from drift_agent.terraform.runner import TerraformError

ROOT = Path(__file__).resolve().parents[1]
PLAN_FIXTURES = ROOT / "tests" / "fixtures" / "plans"
POLICY_PATH = ROOT / "config" / "risk-policy.yaml"


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy.load(POLICY_PATH)


def load_plan(name: str) -> dict:
    return json.loads((PLAN_FIXTURES / name).read_text(encoding="utf-8"))


def write_repo(path: Path, *, module: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "main.tf").write_text(
        'terraform {\n  required_version = ">= 1.5.0"\n}\n', encoding="utf-8"
    )
    (path / "variables.tf").write_text(
        'variable "owner" {\n  default = "old-owner"\n}\n', encoding="utf-8"
    )
    if module:
        (path / "storage.tf").write_text(
            'module "storage" {\n  source = "./modules/storage"\n  owner  = "old-owner"\n}\n',
            encoding="utf-8",
        )
    return path


def make_repo(tmp_path: Path, *, module: bool = False, mode: Mode = Mode.OBSERVE) -> RepositoryConfig:
    checkout = write_repo(tmp_path / "checkout", module=module)
    return RepositoryConfig(id="pilot", path=str(checkout), mode=mode)


class FakeRunner:
    """Records Terraform calls and replays configured outcomes."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        plan_json: str = "{}",
        fail_stage: str | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.plan_json = plan_json
        self.fail_stage = fail_stage
        self.calls: list[str] = []

    def _fail_if(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise TerraformError(stage, "command_failed", 1)

    def init(self, backend_configs, upgrade: bool = False) -> None:
        self.calls.append("init")
        self._fail_if("init")

    def detect_drift(self, plan_path, var_files) -> int:
        self.calls.append("detect_drift")
        self._fail_if("plan")
        return self.exit_code

    def show(self, plan_path) -> str:
        self.calls.append("show")
        self._fail_if("show")
        return self.plan_json

    def plan(self, plan_path, var_files) -> int:
        self.calls.append("plan")
        self._fail_if("post_plan")
        return 2

    def fmt(self, check: bool = True) -> None:
        self.calls.append("fmt")
        self._fail_if("fmt")

    def validate(self) -> None:
        self.calls.append("validate")
        self._fail_if("validate")


def make_orchestrator(
    *,
    policy: RiskPolicy,
    tmp_path: Path,
    runner: FakeRunner | None = None,
    foundry=None,
    github=None,
    notifier=None,
    activity=None,
    dry_run: bool = False,
    audit=None,
    max_concurrency: int = 1,
) -> DriftOrchestrator:
    settings = Settings(
        audit_dir=tmp_path / "audit",
        dry_run=dry_run,
        max_concurrency=max_concurrency,
    )
    terraform = runner or FakeRunner()
    return DriftOrchestrator(
        settings=settings,
        policy=policy,
        audit=audit if audit is not None else InMemoryAuditStore(),
        foundry=foundry if foundry is not None else FakeFoundryClient(),
        activity=activity if activity is not None else NullActivityProvider(),
        github=github if github is not None else FakeGitHubClient(),
        notifier=notifier,
        skill="skill text",
        dry_run=dry_run,
        runner_factory=lambda workdir: terraform,
    )


TAG_PATCH = (
    "--- a/variables.tf\n"
    "+++ b/variables.tf\n"
    "@@ -1,3 +1,3 @@\n"
    ' variable "owner" {\n'
    '-  default = "old-owner"\n'
    '+  default = "platform"\n'
    " }\n"
)
