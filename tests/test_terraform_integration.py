"""Real Terraform smoke tests; skipped when the binary is unavailable."""

import json
import shutil
from pathlib import Path

import pytest
from conftest import POLICY_PATH

from drift_agent.ai.foundry_client import DisabledFoundryClient
from drift_agent.audit.store import NullAuditStore
from drift_agent.azure.activity import NullActivityProvider
from drift_agent.github.client import DisabledGitHubClient
from drift_agent.models import Mode, RepositoryConfig, ScanStatus
from drift_agent.orchestrator import DriftOrchestrator
from drift_agent.policy import RiskPolicy
from drift_agent.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(shutil.which("terraform") is None, reason="Terraform is not installed")
def test_refresh_only_outcomes_without_cloud_access(tmp_path: Path):
    policy = RiskPolicy.load(POLICY_PATH)
    settings = Settings(audit_dir=tmp_path / "audit", terraform_timeout=60, dry_run=True)
    orchestrator = DriftOrchestrator(
        settings=settings,
        policy=policy,
        audit=NullAuditStore(),
        foundry=DisabledFoundryClient(),
        activity=NullActivityProvider(),
        github=DisabledGitHubClient(),
        skill="",
        dry_run=True,
    )

    for fixture, expected_status, expected_exit in (
        ("clean", ScanStatus.CLEAN, 0),
        ("output_change", ScanStatus.DRIFT, 2),
        ("missing_variable", ScanStatus.ERROR, 1),
    ):
        repository = FIXTURES / fixture
        before = sorted(path.relative_to(repository) for path in repository.rglob("*"))
        record = orchestrator.scan(
            RepositoryConfig(id=fixture, path=str(repository), mode=Mode.OBSERVE)
        )
        after = sorted(path.relative_to(repository) for path in repository.rglob("*"))

        assert record.status is expected_status, record.model_dump()
        assert record.terraform_exit_code == expected_exit
        assert before == after
        assert "fixture-only-sensitive-value" not in json.dumps(record.model_dump(mode="json"))
        if expected_status is ScanStatus.DRIFT:
            assert record.outputs == [{"name": "sample", "actions": ["create"]}]
