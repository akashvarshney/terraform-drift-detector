import json

from conftest import TAG_PATCH, FakeRunner, load_plan, make_orchestrator, make_repo

from drift_agent.ai.foundry_client import DisabledFoundryClient, FakeFoundryClient
from drift_agent.audit.store import InMemoryAuditStore
from drift_agent.github.client import FakeGitHubClient, GitHubUnavailable
from drift_agent.models import Mode, RemediationStatus, RiskLevel, ScanStatus
from drift_agent.notifications import RecordingNotifier


def test_clean_scan_makes_no_model_or_notification_calls(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(exit_code=0)
    foundry = FakeFoundryClient()
    notifier = RecordingNotifier()
    audit = InMemoryAuditStore()
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=foundry, notifier=notifier, audit=audit
    )

    record = orchestrator.scan(repo)

    assert record.status is ScanStatus.CLEAN
    assert record.terraform_exit_code == 0
    assert foundry.calls == []
    assert notifier.messages == []
    assert runner.calls == ["init", "detect_drift"]
    assert len(audit.entries) == 1


def test_drift_is_classified_explained_and_recorded(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    foundry = FakeFoundryClient(
        {
            "summary": "tags drifted",
            "probable_cause": "manual tag edit",
            "risk_recommendation": "low",
            "proposed_change": "",
            "confidence": 0.7,
            "manual_review_reason": None,
        }
    )
    audit = InMemoryAuditStore()
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=foundry, audit=audit
    )

    record = orchestrator.scan(repo, Mode.OBSERVE)

    assert record.status is ScanStatus.DRIFT
    assert record.terraform_exit_code == 2
    assert record.risk.level is RiskLevel.LOW
    assert record.risk.model_level is RiskLevel.LOW
    assert record.remediation.status is RemediationStatus.NOT_APPLICABLE
    assert len(foundry.calls) == 1
    assert foundry.calls[0]["drift_facts"]["resources"][0]["changed_attributes"] == [
        {"name": "tags", "sensitive": False}
    ]
    assert record.plan_json_sha256 and len(record.plan_json_sha256) == 64
    assert audit.keys() == [record.scan_id]


def test_sensitive_values_never_reach_the_record(tmp_path, policy):
    repo = make_repo(tmp_path)
    plan = load_plan("drift_tags.json")
    plan["resource_drift"][0]["change"]["after"]["tags"]["owner"] = "top-secret-value"
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(plan))
    orchestrator = make_orchestrator(policy=policy, tmp_path=tmp_path, runner=runner)

    record = orchestrator.scan(repo, Mode.OBSERVE)

    rendered = json.dumps(record.model_dump(mode="json"))
    for value in ("top-secret-value", "platform-team", "CC-200"):
        assert value not in rendered


def test_high_risk_drift_gets_no_patch(tmp_path, policy):
    repo = make_repo(tmp_path, mode=Mode.PR)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_network.json")))
    foundry = FakeFoundryClient(
        {
            "summary": "public access enabled",
            "probable_cause": "portal change",
            "risk_recommendation": "medium",
            "proposed_change": TAG_PATCH,
            "confidence": 0.95,
            "manual_review_reason": None,
        }
    )
    github = FakeGitHubClient()
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=foundry, github=github
    )

    record = orchestrator.scan(repo, Mode.PR)

    assert record.risk.level is RiskLevel.HIGH
    assert record.remediation.status is RemediationStatus.NEEDS_MANUAL_REVIEW
    assert record.remediation.reject_reason == "risk_requires_manual_review"
    assert record.remediation.patch is None
    assert github.calls == []


def test_dry_run_never_calls_github_or_notifications(tmp_path, policy):
    repo = make_repo(tmp_path, mode=Mode.PR)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    foundry = FakeFoundryClient(
        {
            "summary": "tags drifted",
            "probable_cause": "manual tag edit",
            "risk_recommendation": "low",
            "proposed_change": TAG_PATCH,
            "confidence": 0.9,
            "manual_review_reason": None,
        }
    )
    github = FakeGitHubClient()
    notifier = RecordingNotifier()
    orchestrator = make_orchestrator(
        policy=policy,
        tmp_path=tmp_path,
        runner=runner,
        foundry=foundry,
        github=github,
        notifier=notifier,
        dry_run=True,
    )

    record = orchestrator.scan(repo, Mode.PR)

    assert record.status is ScanStatus.DRIFT
    assert record.dry_run is True
    assert record.remediation.status is RemediationStatus.DRY_RUN
    assert "create_draft_pr" in record.remediation.planned_actions
    assert github.calls == []
    assert notifier.messages == []


def test_drift_notification_carries_no_values(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    notifier = RecordingNotifier()
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, notifier=notifier
    )

    orchestrator.scan(repo, Mode.OBSERVE)

    assert len(notifier.messages) == 1
    subject, payload, _recipient = notifier.messages[0]
    assert "pilot" in subject
    assert payload["resources"][0]["address"] == "module.storage.azurerm_storage_account.this"
    rendered = json.dumps(payload)
    assert "platform-team" not in rendered
    assert "CC-200" not in rendered


def test_model_may_escalate_and_the_patch_is_withheld(tmp_path, policy):
    repo = make_repo(tmp_path, mode=Mode.PR)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    foundry = FakeFoundryClient(
        {
            "summary": "suspicious tag change",
            "probable_cause": "unknown actor",
            "risk_recommendation": "high",
            "proposed_change": TAG_PATCH,
            "confidence": 0.9,
            "manual_review_reason": None,
        }
    )
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=foundry
    )

    record = orchestrator.scan(repo, Mode.PR)

    assert record.risk.level is RiskLevel.HIGH
    assert record.risk.model_level is RiskLevel.HIGH
    assert record.remediation.status is RemediationStatus.NEEDS_MANUAL_REVIEW


def test_model_failure_does_not_fail_the_scan(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=DisabledFoundryClient()
    )

    record = orchestrator.scan(repo, Mode.PR)

    assert record.status is ScanStatus.DRIFT
    assert record.risk is not None
    assert record.remediation.status is RemediationStatus.NEEDS_MANUAL_REVIEW
    assert record.remediation.reject_reason == "model_unavailable"


def test_terraform_failure_is_reported_as_error(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(fail_stage="plan")
    audit = InMemoryAuditStore()
    orchestrator = make_orchestrator(policy=policy, tmp_path=tmp_path, runner=runner, audit=audit)

    record = orchestrator.scan(repo)

    assert record.status is ScanStatus.ERROR
    assert record.error.stage == "plan"
    assert record.error.exit_code == 1
    assert audit.entries


def test_invalid_plan_json_is_an_error(tmp_path, policy):
    repo = make_repo(tmp_path)
    runner = FakeRunner(exit_code=2, plan_json="not json")
    orchestrator = make_orchestrator(policy=policy, tmp_path=tmp_path, runner=runner)

    record = orchestrator.scan(repo)

    assert record.status is ScanStatus.ERROR
    assert record.error.reason == "invalid_plan_json"


def test_missing_checkout_is_an_error(tmp_path, policy):
    from drift_agent.models import RepositoryConfig

    repo = RepositoryConfig(id="absent", path=str(tmp_path / "nothing-here"))
    orchestrator = make_orchestrator(policy=policy, tmp_path=tmp_path)

    record = orchestrator.scan(repo)

    assert record.status is ScanStatus.ERROR
    assert record.error.reason == "checkout_not_found"


def test_scan_all_preserves_order(tmp_path, policy):
    import dataclasses

    first = make_repo(tmp_path / "a")
    second = make_repo(tmp_path / "b").model_copy(update={"id": "second"})
    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, max_concurrency=2
    )
    orchestrator.settings = dataclasses.replace(orchestrator.settings, max_concurrency=2)

    records = orchestrator.scan_all([first, second])

    assert [record.repository for record in records] == ["pilot", "second"]
    assert all(record.status is ScanStatus.CLEAN for record in records)


def test_publish_failure_keeps_the_validated_patch(tmp_path, policy):
    repo = make_repo(tmp_path, mode=Mode.PR)
    repo = repo.model_copy(update={"url": "https://github.com/example-org/pilot.git"})
    runner = FakeRunner(exit_code=2, plan_json=json.dumps(load_plan("drift_tags.json")))
    foundry = FakeFoundryClient(
        {
            "summary": "tags drifted",
            "probable_cause": "manual tag edit",
            "risk_recommendation": "low",
            "proposed_change": TAG_PATCH,
            "confidence": 0.9,
            "manual_review_reason": None,
        }
    )

    class FailingGitHub(FakeGitHubClient):
        def get_metadata(self, repo):
            raise GitHubUnavailable("github_app_not_configured")

    orchestrator = make_orchestrator(
        policy=policy, tmp_path=tmp_path, runner=runner, foundry=foundry, github=FailingGitHub()
    )

    record = orchestrator.scan(repo, Mode.PR)

    assert record.status is ScanStatus.DRIFT
    assert record.remediation.status is RemediationStatus.VALIDATED
    assert record.remediation.reject_reason.startswith("github_publish_failed")
