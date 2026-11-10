"""Azure Activity Log correlation.

Evidence is attached to a drift resource only when the returned record's
resource id matches the queried resource id. An unmatched record is kept with
resource_match=False and never used to attribute a change to a caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from drift_agent.models import ActivityEvidence
from drift_agent.settings import AzureSettings


class ActivityUnavailable(RuntimeError):
    """No credential or SDK available; enrichment is skipped, not failed."""


class ActivityProvider(Protocol):
    name: str

    def lookup(
        self,
        resource_ids: list[str],
        start: datetime,
        end: datetime,
        subscription_id: str | None = None,
    ) -> list[ActivityEvidence]: ...


class NullActivityProvider:
    name = "null"

    def lookup(self, *_args: Any, **_kwargs: Any) -> list[ActivityEvidence]:
        return []


class StaticActivityProvider:
    """Deterministic evidence for tests; marks matches like the real provider."""

    name = "static"

    def __init__(self, records: list[ActivityEvidence] | None = None) -> None:
        self.records = list(records or [])
        self.queries: list[tuple[list[str], datetime, datetime]] = []

    def lookup(
        self,
        resource_ids: list[str],
        start: datetime,
        end: datetime,
        subscription_id: str | None = None,
    ) -> list[ActivityEvidence]:
        self.queries.append((list(resource_ids), start, end))
        wanted = {resource_id.casefold() for resource_id in resource_ids}
        results = []
        for record in self.records:
            matched = bool(record.resource_id and record.resource_id.casefold() in wanted)
            results.append(record.model_copy(update={"resource_match": matched}))
        return results


class MonitorActivityProvider:
    """Reads the Azure Activity Log through azure-mgmt-monitor."""

    name = "azure-monitor"

    def __init__(self, settings: AzureSettings, *, client_factory=None) -> None:
        self.settings = settings
        self._client_factory = client_factory

    def _client(self, subscription_id: str):
        if self._client_factory is not None:
            return self._client_factory(subscription_id)
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.monitor import MonitorManagementClient
        except ImportError:
            raise ActivityUnavailable("azure_sdk_not_installed") from None
        try:
            return MonitorManagementClient(DefaultAzureCredential(), subscription_id)
        except Exception:  # noqa: BLE001 - report availability, never credential detail
            raise ActivityUnavailable("azure_credential_unavailable") from None

    def lookup(
        self,
        resource_ids: list[str],
        start: datetime,
        end: datetime,
        subscription_id: str | None = None,
    ) -> list[ActivityEvidence]:
        subscription = subscription_id or self.settings.subscription_id
        if not subscription:
            raise ActivityUnavailable("azure_subscription_not_configured")
        client = self._client(subscription)
        window = (
            f"eventTimestamp ge '{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}'"
            f" and eventTimestamp le '{end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        )
        evidence: list[ActivityEvidence] = []
        for resource_id in resource_ids:
            if not resource_id.startswith("/"):
                continue
            escaped = resource_id.replace("'", "''")
            try:
                events = client.activity_logs.list(
                    filter=f"{window} and resourceUri eq '{escaped}'"
                )
            except Exception:  # noqa: BLE001 - SDK raises several transport errors
                raise ActivityUnavailable("activity_query_failed") from None
            for event in list(events)[: self.settings.activity_max_records]:
                evidence.append(_to_evidence(event, resource_id, subscription))
        return evidence


def _localized(value: Any) -> str:
    if value is None:
        return ""
    localized = getattr(value, "localized_value", None)
    if localized:
        return str(localized)
    return str(getattr(value, "value", value) or "")


def _to_evidence(event: Any, queried_id: str, subscription: str) -> ActivityEvidence:
    timestamp = getattr(event, "event_timestamp", None)
    if hasattr(timestamp, "isoformat"):
        rendered = timestamp.isoformat()
    else:
        rendered = str(timestamp or "")
    resource_id = str(getattr(event, "resource_id", "") or "")
    return ActivityEvidence(
        operation=_localized(getattr(event, "operation_name", None)),
        caller=str(getattr(event, "caller", "") or "unknown"),
        timestamp=rendered,
        correlation_id=getattr(event, "correlation_id", None),
        resource_id=resource_id or None,
        resource_match=bool(resource_id) and resource_id.casefold() == queried_id.casefold(),
        subscription_id=subscription,
    )


def matched_evidence(records: list[ActivityEvidence]) -> list[ActivityEvidence]:
    """Only matched records may be used to attribute a change."""
    return [record for record in records if record.resource_match]
