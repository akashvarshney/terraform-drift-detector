import json
from pathlib import Path

import pytest
from conftest import POLICY_PATH, write_repo

from drift_agent.main import main
from drift_agent.models import DriftRecord, ScanStatus


def record_for(status: ScanStatus, name: str = "pilot") -> DriftRecord:
    return DriftRecord(
        scan_id=f"scan-{name}",
        correlation_id="corr",
        repository=name,
        status=status,
    )


class StubOrchestrator:
    def __init__(self, records):
        self.records = records

    def scan(self, repo, mode=None):
        return self.records[0]

    def scan_all(self, repositories, mode=None):
        return self.records


def test_scan_exit_codes_and_json(monkeypatch, capsys, tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    cases = (
        (ScanStatus.CLEAN, [], 0),
        (ScanStatus.DRIFT, [], 0),
        (ScanStatus.ERROR, [], 1),
        (ScanStatus.DRIFT, ["--fail-on-drift"], 2),
    )
    for status, flags, expected in cases:
        monkeypatch.setattr(
            "drift_agent.main.build_orchestrator",
            lambda settings, dry_run=None, status=status: StubOrchestrator([record_for(status)]),
        )
        assert main(["scan", "--repo", str(checkout), "--dry-run", *flags]) == expected
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["status"] == status.value


def test_scan_all_reports_counts(monkeypatch, capsys, tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    registry = tmp_path / "repos.yaml"
    registry.write_text(
        f"repos:\n  - {{id: pilot, path: {checkout}}}\n  - {{id: other, path: {checkout}}}\n",
        encoding="utf-8",
    )
    records = [
        record_for(ScanStatus.CLEAN, "pilot"),
        record_for(ScanStatus.DRIFT, "other"),
    ]
    monkeypatch.setattr(
        "drift_agent.main.build_orchestrator",
        lambda settings, dry_run=None: StubOrchestrator(records),
    )

    code = main(["scan-all", "--config", str(registry), "--dry-run"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["counts"] == {"clean": 1, "drift": 1, "error": 0}
    assert [scan["repository"] for scan in payload["scans"]] == ["pilot", "other"]


def test_scan_all_fails_when_any_scan_errors(monkeypatch, capsys, tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    registry = tmp_path / "repos.yaml"
    registry.write_text(f"repos:\n  - {{id: pilot, path: {checkout}}}\n", encoding="utf-8")
    monkeypatch.setattr(
        "drift_agent.main.build_orchestrator",
        lambda settings, dry_run=None: StubOrchestrator([record_for(ScanStatus.ERROR)]),
    )
    assert main(["scan-all", "--config", str(registry), "--dry-run"]) == 1
    capsys.readouterr()


def test_validate_config_success(capsys, tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    registry = tmp_path / "repos.yaml"
    registry.write_text(
        f"repos:\n  - {{id: pilot, path: {checkout}, mode: observe}}\n", encoding="utf-8"
    )

    code = main(["validate-config", "--config", str(registry), "--policy", str(POLICY_PATH)])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["ok"] is True
    assert report["repositories"] == 1
    assert report["policy_rules"] >= 8
    assert report["integrations"]["github_app"] is False


def test_validate_config_reports_problems(capsys, tmp_path: Path):
    code = main(
        [
            "validate-config",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--policy",
            str(tmp_path / "missing-policy.yaml"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["ok"] is False
    assert len(report["problems"]) >= 2


def test_unknown_repository_is_an_error(capsys):
    assert main(["scan", "--repo", "does-not-exist"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_invalid_timeout_flag_is_rejected():
    with pytest.raises(SystemExit):
        main(["scan", "--repo", ".", "--timeout", "0"])


def test_module_entry_point_is_available():
    import subprocess
    import sys

    from conftest import ROOT

    result = subprocess.run(
        [sys.executable, "-m", "drift_agent", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "validate-config" in result.stdout
