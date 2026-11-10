"""Fleet rotation ledger, selection, reporting and the CLI rotation flags."""

import json
from datetime import UTC, datetime, timedelta

from conftest import write_repo

from drift_agent.fleet import (
    FleetState,
    build_report,
    due_repositories,
    render_markdown,
    select_due,
)
from drift_agent.main import main
from drift_agent.models import DriftRecord, RepositoryConfig, RiskDecision, RiskLevel, ScanStatus
from drift_agent.registry import Registry

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def repo(name: str, **kwargs) -> RepositoryConfig:
    return RepositoryConfig(id=name, path=f"/tmp/{name}", **kwargs)


def drift_record(name: str, risk: RiskLevel | None = RiskLevel.MEDIUM) -> DriftRecord:
    record = DriftRecord(
        scan_id=f"scan-{name}",
        correlation_id="corr",
        repository=name,
        status=ScanStatus.DRIFT,
    )
    if risk is not None:
        record.risk = RiskDecision(level=risk, policy_rule="tags-metadata-only")
    return record


def clean_record(name: str) -> DriftRecord:
    return DriftRecord(
        scan_id=f"clean-{name}",
        correlation_id="corr",
        repository=name,
        status=ScanStatus.CLEAN,
    )


def test_record_tracks_drift_streak_and_clears_on_clean():
    state = FleetState()

    first = state.record(drift_record("pilot"), now=NOW)
    assert first.consecutive_drift == 1
    assert first.drifting_since == NOW.isoformat()
    assert first.last_risk is RiskLevel.MEDIUM

    later = NOW + timedelta(days=1)
    second = state.record(drift_record("pilot"), now=later)
    assert second.consecutive_drift == 2
    assert second.scans == 2
    assert second.drifting_since == NOW.isoformat()

    cleared = state.record(clean_record("pilot"), now=later + timedelta(days=1))
    assert cleared.consecutive_drift == 0
    assert cleared.drifting_since is None
    assert cleared.last_risk is None
    assert cleared.last_status is ScanStatus.CLEAN


def test_error_scan_does_not_clear_known_drift():
    state = FleetState()
    state.record(drift_record("pilot"), now=NOW)
    error = DriftRecord(
        scan_id="err",
        correlation_id="corr",
        repository="pilot",
        status=ScanStatus.ERROR,
    )

    entry = state.record(error, now=NOW + timedelta(hours=1))

    assert entry.last_status is ScanStatus.ERROR
    assert entry.consecutive_drift == 1
    assert entry.drifting_since == NOW.isoformat()


def test_selection_prefers_never_scanned_then_oldest():
    state = FleetState()
    repositories = [repo("a"), repo("b"), repo("c"), repo("d")]
    state.record(clean_record("a"), now=NOW - timedelta(days=5))
    state.record(clean_record("b"), now=NOW - timedelta(days=2))

    selected = select_due(state, repositories, limit=3, cooldown_hours=0, now=NOW)

    assert [item.id for item in selected] == ["c", "d", "a"]


def test_cooldown_skips_recent_scans_unless_forced():
    state = FleetState()
    repositories = [repo("a"), repo("b")]
    state.record(clean_record("a"), now=NOW - timedelta(hours=2))

    cooled = due_repositories(state, repositories, cooldown_hours=20, now=NOW)
    assert [item.id for item in cooled] == ["b"]

    forced = due_repositories(state, repositories, cooldown_hours=20, force=True, now=NOW)
    assert [item.id for item in forced] == ["b", "a"]

    elapsed = due_repositories(state, repositories, cooldown_hours=0, now=NOW)
    assert [item.id for item in elapsed] == ["b", "a"]


def test_limit_zero_returns_every_due_repository():
    state = FleetState()

    selected = select_due(state, [repo("a"), repo("b")], limit=0, now=NOW)

    assert [item.id for item in selected] == ["a", "b"]


def test_ledger_round_trips_and_survives_corruption(tmp_path):
    path = tmp_path / "fleet" / "state.json"
    state = FleetState()
    state.record(drift_record("pilot"), now=NOW)
    state.save(path)

    loaded = FleetState.load(path)
    assert loaded.get("pilot").last_status is ScanStatus.DRIFT
    assert loaded.get("pilot").last_risk is RiskLevel.MEDIUM
    assert not list(path.parent.glob(".*.tmp"))

    path.write_text("{not json", encoding="utf-8")
    assert FleetState.load(path).entries == {}
    assert FleetState.load(tmp_path / "missing.json").entries == {}


def test_report_consolidates_coverage_and_drift_age():
    registry = Registry(
        repositories=[
            repo("drifting", azure_subscription_id="sub-a", owner_email="a@example.com"),
            repo("fresh", azure_subscription_id="sub-a", owner_email="a@example.com"),
            repo("never", azure_subscription_id="sub-b", owner_email="b@example.com"),
            repo("stale", azure_subscription_id="sub-b", owner_email="b@example.com"),
        ]
    )
    state = FleetState()
    state.record(drift_record("drifting"), now=NOW - timedelta(days=9))
    state.record(clean_record("fresh"), now=NOW - timedelta(days=1))
    state.record(clean_record("stale"), now=NOW - timedelta(days=10))

    report = build_report(state, registry, batch_size=2, stale_days=7, now=NOW)

    assert report["coverage"]["repositories"] == 4
    assert report["coverage"]["never_scanned"] == 1
    assert report["coverage"]["scanned_recently"] == 1
    assert report["coverage"]["stale"] == 2
    assert report["coverage"]["drifting"] == 1
    assert report["drift"][0]["repository"] == "drifting"
    assert report["drift"][0]["days_drifting"] == 9.0
    assert report["rotation"]["outstanding"] == 3
    assert report["rotation"]["days_to_cover"] == 2
    assert report["rotation"]["next_batch"] == ["never", "stale"]
    assert report["by_subscription"][0]["key"] == "sub-a"
    assert report["by_subscription"][0]["drifting"] == 1


def test_markdown_report_lists_drift_and_next_batch():
    registry = Registry(
        repositories=[repo("drifting", azure_subscription_id="sub-a"), repo("never")]
    )
    state = FleetState()
    state.record(drift_record("drifting"), now=NOW - timedelta(days=3))

    text = render_markdown(build_report(state, registry, batch_size=5, now=NOW))

    assert "# Terraform drift" in text
    assert "| drifting | sub-a |" in text
    assert "Next batch:" in text
    assert "- never" in text


class RecordingOrchestrator:
    """Stands in for the real orchestrator so the rotation can be tested offline."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def scan(self, repository, mode=None):
        self.seen.append(repository.id)
        return DriftRecord(
            scan_id=f"scan-{repository.id}",
            correlation_id="corr",
            repository=repository.id,
            status=ScanStatus.DRIFT,
        )

    def scan_all(self, repositories, mode=None):
        return [self.scan(item, mode) for item in repositories]


def test_scan_all_limit_rotates_across_runs(monkeypatch, capsys, tmp_path):
    checkout = write_repo(tmp_path / "checkout")
    registry = tmp_path / "repos.yaml"
    registry.write_text(
        "repos:\n"
        + "".join(f"  - {{id: repo-{index}, path: {checkout}}}\n" for index in range(3)),
        encoding="utf-8",
    )
    state_path = tmp_path / "fleet" / "state.json"
    stub = RecordingOrchestrator()
    monkeypatch.setattr(
        "drift_agent.main.build_orchestrator", lambda settings, dry_run=None: stub
    )
    arguments = [
        "scan-all",
        "--config",
        str(registry),
        "--fleet-state",
        str(state_path),
        "--limit",
        "2",
        "--dry-run",
    ]

    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert [scan["repository"] for scan in first["scans"]] == ["repo-0", "repo-1"]
    assert first["fleet"]["deferred"] == 1
    assert state_path.is_file()

    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert [scan["repository"] for scan in second["scans"]] == ["repo-2"]

    assert main(arguments) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["status"] == "skipped"
    assert third["scans"] == []

    assert stub.seen == ["repo-0", "repo-1", "repo-2"]


def test_fleet_report_cli_prints_json_and_markdown(capsys, tmp_path):
    checkout = write_repo(tmp_path / "checkout")
    registry = tmp_path / "repos.yaml"
    registry.write_text(
        f"repos:\n  - {{id: pilot, path: {checkout}, azure_subscription_id: sub-a}}\n"
        f"  - {{id: other, path: {checkout}, azure_subscription_id: sub-b}}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state = FleetState()
    state.record(drift_record("pilot"), now=datetime.now(UTC))
    state.save(state_path)
    arguments = [
        "fleet-report",
        "--config",
        str(registry),
        "--fleet-state",
        str(state_path),
    ]

    assert main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["repositories"] == 2
    assert report["coverage"]["never_scanned"] == 1
    assert report["drift"][0]["repository"] == "pilot"

    output = tmp_path / "fleet.md"
    assert main([*arguments, "--format", "markdown", "--output", str(output)]) == 0
    text = capsys.readouterr().out
    assert "# Terraform drift" in text
    assert output.read_text(encoding="utf-8") == text
