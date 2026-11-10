"""Command line interface: scan, scan-all and validate-config."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from drift_agent.ai.foundry_client import DisabledFoundryClient, HttpFoundryClient
from drift_agent.audit.store import LocalJsonAuditStore, NullAuditStore
from drift_agent.azure.activity import MonitorActivityProvider, NullActivityProvider
from drift_agent.github.client import AppGitHubClient, DisabledGitHubClient
from drift_agent.models import DriftRecord, Mode, ScanStatus
from drift_agent.notifications import CompositeNotifier
from drift_agent.notifications.email import GraphEmailNotifier
from drift_agent.notifications.teams import TeamsNotifier
from drift_agent.observability import EventLogger, configure_logging
from drift_agent.orchestrator import DriftOrchestrator
from drift_agent.policy import PolicyError, RiskPolicy
from drift_agent.registry import Registry, RegistryError, resolve_repository
from drift_agent.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    # Shared so the global options are accepted before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="Repository registry YAML (default: config/repos.yaml).")
    common.add_argument("--policy", help="Risk policy YAML (default: config/risk-policy.yaml).")
    common.add_argument("--audit-dir", help="Audit output directory (default: reports/audit).")
    common.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR.")
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="Never mutate anything external: no branches, commits, PRs or notifications.",
    )
    common.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit 2 when drift is found (local/CI use; omit for Container Apps Jobs).",
    )

    parser = argparse.ArgumentParser(
        prog="drift-agent",
        description="Observe-only-by-default Terraform drift agent; prints reduced JSON.",
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan", parents=[common], help="Scan one repository by registry id or path."
    )
    scan.add_argument("--repo", required=True, help="Registry repository id or a checkout path.")
    scan.add_argument("--mode", choices=[mode.value for mode in Mode], default=None)
    scan.add_argument("--timeout", type=int, default=None, help="Maximum seconds per Terraform command.")

    scan_all = subparsers.add_parser(
        "scan-all", parents=[common], help="Scan every enabled registry repository."
    )
    scan_all.add_argument("--mode", choices=[mode.value for mode in Mode], default=None)
    scan_all.add_argument("--max-concurrency", type=int, default=None)

    subparsers.add_parser(
        "validate-config",
        parents=[common],
        help="Validate registry, policy and integration configuration.",
    )
    return parser


def _settings(arguments: argparse.Namespace) -> Settings:
    base = Settings.from_env()
    updates: dict[str, Any] = {}
    if arguments.config:
        updates["registry_path"] = Path(arguments.config)
    if arguments.policy:
        updates["policy_path"] = Path(arguments.policy)
    if arguments.audit_dir:
        updates["audit_dir"] = Path(arguments.audit_dir)
    if arguments.log_level:
        updates["log_level"] = arguments.log_level.upper()
    if arguments.dry_run:
        updates["dry_run"] = True
    if getattr(arguments, "max_concurrency", None):
        updates["max_concurrency"] = max(1, arguments.max_concurrency)
    if getattr(arguments, "timeout", None) is not None:
        if arguments.timeout <= 0:
            raise SystemExit(_timeout_error())
        updates["terraform_timeout"] = arguments.timeout
    from dataclasses import replace

    return replace(base, **updates)


def _timeout_error() -> int:
    print(json.dumps({"status": "error", "reason": "--timeout must be positive"}))
    return 2


def _load_registry(settings: Settings, required: bool) -> Registry | None:
    try:
        return Registry.load(settings.registry_path)
    except RegistryError:
        if required:
            raise
        return None


def _skill_text(settings: Settings) -> str:
    try:
        return settings.skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _notifier(settings: Settings):
    adapters = []
    if settings.teams.configured:
        adapters.append(TeamsNotifier(settings.teams))
    if settings.email.configured:
        adapters.append(GraphEmailNotifier(settings.email))
    return CompositeNotifier(adapters) if adapters else None


def build_orchestrator(settings: Settings, *, dry_run: bool | None = None) -> DriftOrchestrator:
    policy = RiskPolicy.load(settings.policy_path)
    foundry = (
        HttpFoundryClient(settings.foundry)
        if settings.foundry.configured
        else DisabledFoundryClient()
    )
    activity = (
        MonitorActivityProvider(settings.azure)
        if settings.azure.configured
        else NullActivityProvider()
    )
    github = (
        AppGitHubClient(settings.github) if settings.github.configured else DisabledGitHubClient()
    )
    audit = LocalJsonAuditStore(settings.audit_dir) if settings.audit_dir else NullAuditStore()
    return DriftOrchestrator(
        settings=settings,
        policy=policy,
        audit=audit,
        foundry=foundry,
        activity=activity,
        github=github,
        notifier=_notifier(settings),
        skill=_skill_text(settings),
        dry_run=settings.dry_run if dry_run is None else dry_run,
    )


def _emit(record: DriftRecord) -> None:
    print(json.dumps(record.model_dump(mode="json"), separators=(",", ":"), default=str))


def _exit_code(records: Sequence[DriftRecord], fail_on_drift: bool) -> int:
    if any(record.status is ScanStatus.ERROR for record in records):
        return 1
    if fail_on_drift and any(record.status is ScanStatus.DRIFT for record in records):
        return 2
    return 0


def _validate_config(arguments: argparse.Namespace) -> int:
    settings = _settings(arguments)
    problems: list[str] = []
    registry: Registry | None = None
    try:
        registry = Registry.load(settings.registry_path)
        problems.extend(registry.problems(workspace=Path.cwd()))
    except RegistryError as error:
        problems.append(str(error))
    policy: RiskPolicy | None = None
    try:
        policy = RiskPolicy.load(settings.policy_path)
        problems.extend(policy.problems())
    except PolicyError as error:
        problems.append(str(error))
    if not settings.skill_path.is_file():
        problems.append(f"skill file missing: {settings.skill_path}")
    report = {
        "ok": not problems,
        "repositories": len(registry.repositories) if registry else 0,
        "enabled": len(registry.enabled()) if registry else 0,
        "policy_rules": len(policy.rules) if policy else 0,
        "problems": problems,
        "integrations": {
            "foundry": settings.foundry.configured,
            "azure_activity": settings.azure.configured,
            "github_app": settings.github.configured,
            "teams": settings.teams.configured,
            "email": settings.email.configured,
        },
        "notes": settings.missing_credential_notes(),
        "dry_run": settings.dry_run,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    settings = _settings(arguments)
    configure_logging(settings.log_level)
    logger = EventLogger("cli")

    if arguments.command == "validate-config":
        return _validate_config(arguments)

    mode = Mode(arguments.mode) if getattr(arguments, "mode", None) else None
    try:
        if arguments.command == "scan":
            registry = _load_registry(settings, required=False)
            repo = resolve_repository(arguments.repo, registry, mode)
            orchestrator = build_orchestrator(settings)
            record = orchestrator.scan(repo, mode)
            _emit(record)
            return _exit_code([record], arguments.fail_on_drift)

        registry = _load_registry(settings, required=True)
        repositories = registry.enabled() if registry else []
        orchestrator = build_orchestrator(settings)
        records = orchestrator.scan_all(repositories, mode)
        print(
            json.dumps(
                {
                    "scans": [record.model_dump(mode="json") for record in records],
                    "counts": {
                        "clean": sum(1 for r in records if r.status is ScanStatus.CLEAN),
                        "drift": sum(1 for r in records if r.status is ScanStatus.DRIFT),
                        "error": sum(1 for r in records if r.status is ScanStatus.ERROR),
                    },
                },
                separators=(",", ":"),
                default=str,
            )
        )
        return _exit_code(records, arguments.fail_on_drift)
    except (RegistryError, PolicyError) as error:
        logger.error("configuration_error", reason=str(error))
        print(json.dumps({"status": "error", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
