"""The orchestrated scan state machine.

scan -> detect -> enrich -> classify -> explain -> remediate -> validate
-> publish (draft PR) -> notify -> audit

Each external integration degrades to a recorded reason instead of failing the
scan: a missing credential must never hide a drift finding.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from drift_agent.ai.foundry_client import FoundryClient, FoundryUnavailable, ModelError
from drift_agent.audit.store import AuditStore
from drift_agent.azure.activity import ActivityProvider, ActivityUnavailable
from drift_agent.github.client import GitHubClient, GitHubError, GitHubUnavailable
from drift_agent.models import (
    DriftRecord,
    DriftResource,
    ErrorInfo,
    Mode,
    ModelAnalysis,
    RemediationStatus,
    RiskDecision,
    RiskLevel,
    ScanStatus,
)
from drift_agent.notifications import Notifier, notification_payload, notification_subject
from drift_agent.observability import EventLogger
from drift_agent.policy import (
    RiskPolicy,
    aggregate,
    apply_model_recommendation,
    effective_mode,
    patch_allowed,
)
from drift_agent.redaction import hash_payload, redact_text
from drift_agent.remediator import Remediator
from drift_agent.settings import Settings
from drift_agent.terraform.plan_parser import InvalidPlan, parse_plan
from drift_agent.terraform.runner import TerraformError, TerraformRunner
from drift_agent.validator import Validator
from drift_agent.workspace import (
    InvalidInput,
    head_sha,
    input_files,
    temporary_checkout,
    terraform_root_of,
)

PR_BODY_LIMIT = 8000


class DriftOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        policy: RiskPolicy,
        audit: AuditStore,
        foundry: FoundryClient,
        activity: ActivityProvider,
        github: GitHubClient,
        notifier: Notifier | None = None,
        skill: str = "",
        dry_run: bool | None = None,
        remediator: Remediator | None = None,
        validator: Validator | None = None,
        runner_factory: Callable[[Path], TerraformRunner] | None = None,
        logger: EventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.audit = audit
        self.foundry = foundry
        self.activity = activity
        self.github = github
        self.notifier = notifier
        self.skill = skill
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.logger = logger or EventLogger("orchestrator")
        self.runner_factory = runner_factory or (
            lambda workdir: TerraformRunner(workdir, settings.terraform_timeout)
        )
        self.remediator = remediator or Remediator()
        self.validator = validator or Validator(
            self.runner_factory, tflint_enabled=settings.tflint_enabled
        )

    # ------------------------------------------------------------------ scan
    def scan(self, repo, mode: Mode | None = None) -> DriftRecord:
        requested_mode = mode or repo.mode
        record = DriftRecord(
            scan_id=str(uuid4()),
            correlation_id=str(uuid4()),
            repository=repo.id,
            repository_slug=repo.slug,
            terraform_root=repo.terraform_root,
            environment=repo.environment,
            mode=requested_mode,
            dry_run=self.dry_run,
        )
        phase = "prepare"
        started = time.perf_counter()
        try:
            source = Path(repo.path).expanduser() if repo.path else None
            if source is None or not source.is_dir():
                raise InvalidInput("checkout_not_found")
            record.commit_sha = head_sha(source)
            self._event("scan_started", record)

            with temporary_checkout(source) as checkout:
                phase = "init"
                root = terraform_root_of(checkout, repo.terraform_root)
                variables = input_files(checkout, root, repo.var_files)
                backends = input_files(checkout, root, repo.backend_configs)
                runner = self.runner_factory(root)
                runner.init(backends)

                phase = "detect"
                plan_path = checkout.parent / "drift.tfplan"
                exit_code = runner.detect_drift(plan_path, variables)
                record.terraform_exit_code = exit_code
                if exit_code == 0:
                    record.status = ScanStatus.CLEAN
                    self._event("scan_clean", record)
                else:
                    record.status = ScanStatus.DRIFT
                    phase = "parse"
                    self._parse(record, runner.show(plan_path))
                    phase = "enrich"
                    self._enrich(record, repo)
                    phase = "classify"
                    self._classify(record)
                    phase = "explain"
                    analysis = self._explain(record, root)
                    record.remediation.analysis = analysis
                    effective = effective_mode(requested_mode, record.risk, repo.auto_allowlist)
                    phase = "remediate"
                    if record.risk is not None and not patch_allowed(record.risk):
                        record.remediation.status = RemediationStatus.NEEDS_MANUAL_REVIEW
                        record.remediation.reject_reason = "risk_requires_manual_review"
                    else:
                        record.remediation = self.remediator.evaluate(
                            workdir=root,
                            analysis=analysis,
                            protected_paths=repo.protected_paths,
                            mode=effective,
                            dry_run=self.dry_run,
                        )
                    if record.remediation.status is RemediationStatus.PROPOSED:
                        phase = "validate"
                        record.remediation.validation = self.validator.validate(
                            workdir=root,
                            var_files=variables,
                            approved_addresses={item.address for item in record.resources}
                            or {output["name"] for output in record.outputs},
                            plan_directory=checkout.parent,
                        )
                        record.remediation.status = (
                            RemediationStatus.VALIDATED
                            if record.remediation.validation.passed
                            else RemediationStatus.REJECTED
                        )
                        if not record.remediation.validation.passed:
                            record.remediation.reject_reason = (
                                record.remediation.validation.reject_reason
                            )
                    if record.remediation.status is RemediationStatus.VALIDATED:
                        phase = "publish"
                        self._publish(record, repo, effective, root)
                    if record.remediation.status is RemediationStatus.DRY_RUN:
                        record.remediation.planned_actions.append("create_draft_pr")
                        record.remediation.planned_actions.append("notify")
                    phase = "notify"
                    self._notify(record, repo)
        except TerraformError as error:
            if error.stage == "plan":
                record.terraform_exit_code = error.exit_code
            record.status = ScanStatus.ERROR
            record.error = ErrorInfo(
                stage=error.stage, reason=error.reason, exit_code=error.exit_code
            )
        except (InvalidPlan, json.JSONDecodeError):
            record.status = ScanStatus.ERROR
            record.error = ErrorInfo(stage="parse", reason="invalid_plan_json")
        except InvalidInput as error:
            record.status = ScanStatus.ERROR
            record.error = ErrorInfo(stage=phase, reason=error.reason)
        except (OSError, shutil.Error):
            record.status = ScanStatus.ERROR
            record.error = ErrorInfo(stage=phase, reason="workspace_io_error")
        except Exception as error:  # noqa: BLE001 - a worker reports, it does not crash the job
            record.status = ScanStatus.ERROR
            record.error = ErrorInfo(stage=phase, reason="unexpected_error")
            self.logger.error(
                "scan_unexpected_error", scan_id=record.scan_id, phase=phase,
                error_type=type(error).__name__,
            )
        finally:
            record.durations_ms["total"] = int((time.perf_counter() - started) * 1000)
            self._audit(record)
            self._event("scan_finished", record)
        return record

    def scan_all(self, repositories: Sequence, mode: Mode | None = None) -> list[DriftRecord]:
        if not repositories:
            return []
        workers = max(1, min(self.settings.max_concurrency, len(repositories)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self.scan, repo, mode) for repo in repositories]
            return [future.result() for future in futures]

    # --------------------------------------------------------------- stages
    def _parse(self, record: DriftRecord, raw_plan: str) -> None:
        record.plan_json_sha256 = hash_payload(raw_plan)
        parsed = parse_plan(json.loads(raw_plan))
        record.resources = [
            DriftResource.model_validate(resource) for resource in parsed["resources"]
        ]
        record.outputs = parsed["outputs"]

    def _enrich(self, record: DriftRecord, repo) -> None:
        resource_ids = [item.resource_id for item in record.resources if item.resource_id]
        if not resource_ids:
            return
        end = datetime.now(UTC)
        start = end - timedelta(minutes=self.settings.azure.activity_lookback_minutes)
        try:
            record.azure_activity = self.activity.lookup(
                resource_ids, start, end, repo.azure_subscription_id
            )
        except ActivityUnavailable as error:
            self.logger.warning(
                "activity_correlation_skipped", scan_id=record.scan_id, reason=str(error)
            )

    def _classify(self, record: DriftRecord) -> None:
        decisions: list[RiskDecision] = []
        for resource in record.resources:
            resource.risk = self.policy.classify(resource)
            decisions.append(resource.risk)
        if decisions:
            record.risk = aggregate(decisions)
        else:
            record.risk = RiskDecision(
                level=RiskLevel.MEDIUM,
                policy_rule="outputs_only",
                requires_review=True,
                auto_allowed=False,
                reason="Only Terraform outputs changed; review the output consumers.",
            )

    def _explain(self, record: DriftRecord, root: Path) -> ModelAnalysis | None:
        """Ask the model for an explanation only for drift, never for clean scans."""
        facts = self._drift_facts(record)
        context = self._repository_context(root)
        try:
            analysis = self.foundry.analyze(
                drift_facts=facts, skill=self.skill, repository_context=context
            )
        except FoundryUnavailable as error:
            self.logger.warning(
                "model_explanation_skipped", scan_id=record.scan_id, reason=str(error)
            )
            return None
        except ModelError as error:
            self.logger.warning(
                "model_explanation_failed", scan_id=record.scan_id, reason=error.reason
            )
            return None
        if analysis.risk_recommendation is not None and record.risk is not None:
            record.risk = apply_model_recommendation(record.risk, analysis.risk_recommendation)
        return analysis

    def _publish(self, record: DriftRecord, repo, effective: Mode, root: Path) -> None:
        if effective not in {Mode.PR, Mode.AUTO}:
            record.remediation.reject_reason = "publish_requires_pr_or_auto_mode"
            return
        if self.dry_run:
            record.remediation.status = RemediationStatus.DRY_RUN
            record.remediation.planned_actions = [
                "create_branch",
                "commit_files",
                "create_draft_pr",
            ]
            return
        if not repo.url:
            record.remediation.reject_reason = "github_url_not_configured"
            return
        branch = f"drift-fix/{record.scan_id[:8]}"
        try:
            metadata = self.github.get_metadata(repo)
            self.github.create_branch(repo, branch, metadata.head_sha or record.commit_sha or "")
            files = self._edited_contents(root, record.remediation.edited_files)
            self.github.commit_files(
                repo,
                branch,
                files,
                f"Propose drift remediation for {', '.join(record.remediation.edited_files)}",
            )
            url = self.github.create_draft_pr(
                repo,
                head=branch,
                base=metadata.default_branch or repo.default_branch,
                title=f"Drift remediation: {record.repository} ({record.risk.level.value} risk)",
                body=self._pull_request_body(record),
            )
        except (GitHubError, GitHubUnavailable) as error:
            record.remediation.reject_reason = f"github_publish_failed: {getattr(error, 'reason', 'unavailable')}"
            self.logger.warning(
                "publish_failed", scan_id=record.scan_id, reason=record.remediation.reject_reason
            )
            return
        record.remediation.branch = branch
        record.remediation.pr_url = url
        record.remediation.status = RemediationStatus.PR_CREATED
        self._event("draft_pr_created", record)

    def _notify(self, record: DriftRecord, repo) -> None:
        if self.notifier is None or self.dry_run:
            return
        if record.status not in {ScanStatus.DRIFT, ScanStatus.ERROR}:
            return
        subject = notification_subject(record)
        payload = notification_payload(record)
        try:
            self.notifier.send(subject, payload, recipient=repo.owner_email)
        except Exception as error:  # noqa: BLE001 - notification failure must not fail the scan
            record.notification.failed = [str(getattr(error, "reason", self.notifier.name))]
            self.logger.warning(
                "notification_failed", scan_id=record.scan_id, reason=record.notification.failed[0]
            )
            return
        record.notification.sent = [self.notifier.name]

    # --------------------------------------------------------------- helpers
    def _drift_facts(self, record: DriftRecord) -> dict[str, Any]:
        """Reduced, value-free facts; this is the entire model input."""
        return {
            "repository": record.repository,
            "environment": record.environment,
            "mode": record.mode.value,
            "terraform_exit_code": record.terraform_exit_code,
            "resources": [
                {
                    "address": resource.address,
                    "actions": resource.actions,
                    "changed_attributes": [
                        {"name": attribute.name, "sensitive": attribute.sensitive}
                        for attribute in resource.changed_attributes
                    ],
                    "risk": resource.risk.level.value if resource.risk else None,
                }
                for resource in record.resources
            ],
            "outputs": record.outputs,
            "activity_evidence": [
                {
                    "operation": item.operation,
                    "caller": item.caller,
                    "timestamp": item.timestamp,
                    "resource_match": item.resource_match,
                }
                for item in record.azure_activity
            ],
            "policy": record.risk.model_dump(mode="json") if record.risk else None,
        }

    def _repository_context(self, root: Path) -> str:
        """Terraform root configuration only: no tfvars, state or plan files."""
        budget = self.settings.foundry.max_repository_context
        chunks: list[str] = []
        total = 0
        for path in sorted(root.glob("*.tf")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunk = f"### {path.name}\n{redact_text(text)}"
            chunks.append(chunk)
            total += len(chunk)
            if total >= budget:
                break
        return "\n\n".join(chunks)[:budget]

    def _edited_contents(self, root: Path, edited_files: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        base = root.resolve()
        for name in edited_files:
            path = (base / name).resolve()
            if path.is_file() and path.is_relative_to(base):
                contents[name] = path.read_text(encoding="utf-8", errors="replace")
        return contents

    def _pull_request_body(self, record: DriftRecord) -> str:
        facts = self._drift_facts(record)
        validation = (
            [step.model_dump() for step in record.remediation.validation.steps]
            if record.remediation.validation
            else []
        )
        summary = {
            "scan_id": record.scan_id,
            "commit_sha": record.commit_sha,
            "risk": record.risk.model_dump(mode="json") if record.risk else None,
            "drift": facts["resources"],
            "outputs": facts["outputs"],
            "evidence": facts["activity_evidence"],
            "validation": validation,
            "model_summary": record.remediation.analysis.summary
            if record.remediation.analysis
            else None,
            "model_confidence": record.remediation.analysis.confidence
            if record.remediation.analysis
            else None,
        }
        body = (
            "Draft drift remediation generated by terraform-drift-agent.\n\n"
            "```json\n" + json.dumps(summary, indent=2, sort_keys=True) + "\n```\n\n"
            "No attribute values, plan JSON or secrets are included. "
            "Review before merging; the agent never applies changes.\n"
        )
        return body[:PR_BODY_LIMIT]

    def _audit(self, record: DriftRecord) -> None:
        try:
            writer = getattr(self.audit, "record", None)
            if callable(writer):
                writer(record)
            else:
                self.audit.write(record.scan_id, record.model_dump(mode="json"))
        except Exception as error:  # noqa: BLE001 - audit failure must not fail the scan
            self.logger.error(
                "audit_write_failed", scan_id=record.scan_id, error_type=type(error).__name__
            )

    def _event(self, event: str, record: DriftRecord) -> None:
        self.logger.event(
            event,
            scan_id=record.scan_id,
            repository=record.repository,
            commit=record.commit_sha,
            mode=record.mode.value,
            status=record.status.value,
            risk=record.risk.level.value if record.risk else None,
            remediation=record.remediation.status.value,
            duration_ms=record.durations_ms.get("total"),
            dry_run=record.dry_run,
        )
