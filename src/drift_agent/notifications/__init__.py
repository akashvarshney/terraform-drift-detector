"""Notification adapters and the value-free payload they carry."""

from __future__ import annotations

from typing import Any, Protocol

from drift_agent.models import DriftRecord


class Notifier(Protocol):
    name: str

    def send(self, subject: str, payload: dict[str, Any], *, recipient: str | None = None) -> None: ...


class CompositeNotifier:
    name = "composite"

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, subject: str, payload: dict[str, Any], *, recipient: str | None = None) -> None:
        for notifier in self.notifiers:
            notifier.send(subject, payload, recipient=recipient)


class RecordingNotifier:
    """Test double: records payloads, sends nothing."""

    name = "recording"

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any], str | None]] = []

    def send(self, subject: str, payload: dict[str, Any], *, recipient: str | None = None) -> None:
        self.messages.append((subject, payload, recipient))


def notification_payload(record: DriftRecord) -> dict[str, Any]:
    """Only reduced facts: no attribute values, no plan JSON, no secrets."""
    return {
        "scan_id": record.scan_id,
        "repository": record.repository,
        "environment": record.environment,
        "detected_at": record.detected_at,
        "mode": record.mode.value,
        "dry_run": record.dry_run,
        "status": record.status.value,
        "resources": [
            {
                "address": resource.address,
                "actions": resource.actions,
                "changed_attributes": resource.attribute_names,
            }
            for resource in record.resources[:10]
        ],
        "risk": record.risk.model_dump(mode="json") if record.risk else None,
        "evidence": [
            {"operation": item.operation, "caller": item.caller, "timestamp": item.timestamp}
            for item in record.azure_activity
            if item.resource_match
        ],
        "remediation": {
            "status": record.remediation.status.value,
            "patch_hash": record.remediation.patch_hash,
            "pr_url": record.remediation.pr_url,
            "reject_reason": record.remediation.reject_reason,
        },
    }


def notification_subject(record: DriftRecord) -> str:
    risk = record.risk.level.value if record.risk else "unknown"
    return f"Terraform drift in {record.repository} ({risk} risk, {record.remediation.status.value})"
