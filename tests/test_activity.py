from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from drift_agent.azure.activity import (
    ActivityUnavailable,
    MonitorActivityProvider,
    StaticActivityProvider,
    matched_evidence,
)
from drift_agent.models import ActivityEvidence
from drift_agent.settings import AzureSettings

RESOURCE = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st"
START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=2)


def evidence(resource_id: str | None) -> ActivityEvidence:
    return ActivityEvidence(
        operation="Microsoft.Storage/storageAccounts/write",
        caller="someone@example.com",
        timestamp="2026-01-01T00:30:00Z",
        resource_id=resource_id,
    )


def test_static_provider_marks_only_matching_resources():
    provider = StaticActivityProvider([evidence(RESOURCE), evidence("/subscriptions/other/thing")])
    records = provider.lookup([RESOURCE], START, END)
    assert [record.resource_match for record in records] == [True, False]
    assert provider.queries[0][0] == [RESOURCE]


def test_matched_evidence_filters_unmatched_records():
    matched = evidence(RESOURCE).model_copy(update={"resource_match": True})
    unmatched = evidence("/elsewhere")
    assert matched_evidence([matched, unmatched]) == [matched]


def test_missing_subscription_is_unavailable():
    provider = MonitorActivityProvider(AzureSettings(tenant_id="t"))
    with pytest.raises(ActivityUnavailable):
        provider.lookup([RESOURCE], START, END)


def test_monitor_provider_maps_events_and_sets_match():
    event = SimpleNamespace(
        operation_name=SimpleNamespace(localized_value="Write Storage Account"),
        caller="alice@example.com",
        event_timestamp=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        correlation_id="corr-1",
        resource_id=RESOURCE,
    )

    class ActivityLogs:
        def list(self, filter):
            assert "resourceUri eq" in filter
            return [event]

    client = SimpleNamespace(activity_logs=ActivityLogs())
    provider = MonitorActivityProvider(
        AzureSettings(tenant_id="t", subscription_id="s"), client_factory=lambda _s: client
    )

    records = provider.lookup([RESOURCE], START, END)

    assert len(records) == 1
    assert records[0].operation == "Write Storage Account"
    assert records[0].caller == "alice@example.com"
    assert records[0].resource_match is True
    assert records[0].correlation_id == "corr-1"


def test_query_failure_is_reported_as_unavailable():
    class ActivityLogs:
        def list(self, filter):
            raise RuntimeError("boom")

    client = SimpleNamespace(activity_logs=ActivityLogs())
    provider = MonitorActivityProvider(
        AzureSettings(tenant_id="t", subscription_id="s"), client_factory=lambda _s: client
    )
    with pytest.raises(ActivityUnavailable):
        provider.lookup([RESOURCE], START, END)


def test_non_arm_resource_ids_are_skipped():
    class ActivityLogs:
        def list(self, filter):
            return []

    client = SimpleNamespace(activity_logs=ActivityLogs())
    provider = MonitorActivityProvider(
        AzureSettings(tenant_id="t", subscription_id="s"), client_factory=lambda _s: client
    )
    assert provider.lookup(["not-an-arm-id"], START, END) == []
