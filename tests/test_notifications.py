import json

import pytest

from drift_agent.models import (
    DriftRecord,
    DriftResource,
    RemediationStatus,
    RiskDecision,
    RiskLevel,
    ScanStatus,
)
from drift_agent.notifications import (
    CompositeNotifier,
    RecordingNotifier,
    notification_payload,
    notification_subject,
)
from drift_agent.notifications.email import EmailError, GraphEmailNotifier
from drift_agent.notifications.teams import TeamsError, TeamsNotifier
from drift_agent.settings import EmailSettings, TeamsSettings


def record() -> DriftRecord:
    return DriftRecord(
        scan_id="scan-1",
        correlation_id="corr-1",
        repository="pilot",
        status=ScanStatus.DRIFT,
        risk=RiskDecision(level=RiskLevel.LOW, policy_rule="tags-metadata-only"),
        resources=[
            DriftResource(
                address="module.storage.azurerm_storage_account.this",
                actions=["update"],
                changed_attributes=[{"name": "tags"}],
            )
        ],
        remediation={"status": RemediationStatus.VALIDATED, "pr_url": "https://example/pull/1"},
    )


def test_payload_contains_metadata_only():
    payload = notification_payload(record())
    rendered = json.dumps(payload)
    assert payload["resources"][0]["changed_attributes"] == ["tags"]
    assert payload["remediation"]["pr_url"] == "https://example/pull/1"
    assert "CC-200" not in rendered


def test_subject_names_repository_and_risk():
    subject = notification_subject(record())
    assert "pilot" in subject and "low" in subject


def test_teams_requires_a_webhook():
    with pytest.raises(TeamsError):
        TeamsNotifier(TeamsSettings()).send("s", {}, recipient=None)


def test_teams_posts_a_message_card(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers=None, payload=None, timeout=15, accepted=(200,)):
        captured.update(url=url, payload=payload)
        return {}

    import drift_agent.notifications.teams as module

    monkeypatch.setattr(module, "request_json", fake_request)
    TeamsNotifier(TeamsSettings(webhook_url="https://example.webhook")).send(
        "subject", notification_payload(record()), recipient=None
    )

    assert captured["payload"]["@type"] == "MessageCard"
    facts = {item["name"]: item["value"] for item in captured["payload"]["sections"][0]["facts"]}
    assert facts["Repository"] == "pilot"
    assert facts["Risk"] == "low"


def test_email_requires_sender_and_recipient():
    with pytest.raises(EmailError):
        GraphEmailNotifier(EmailSettings(), token_provider=lambda: "t").send("s", {}, recipient="a@b")
    with pytest.raises(EmailError):
        GraphEmailNotifier(EmailSettings(sender="s@b"), token_provider=lambda: "t").send(
            "s", {}, recipient=None
        )


def test_composite_fans_out():
    first, second = RecordingNotifier(), RecordingNotifier()
    CompositeNotifier([first, second]).send("subject", {"a": 1}, recipient="owner@example.com")
    assert first.messages == second.messages == [("subject", {"a": 1}, "owner@example.com")]
