"""Typed data contracts shared by every stage of the drift agent."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Mode(str, Enum):
    """Operating modes from the design document."""

    OBSERVE = "observe"
    SUGGEST = "suggest"
    PR = "pr"
    AUTO = "auto"


class ScanStatus(str, Enum):
    CLEAN = "clean"
    DRIFT = "drift"
    ERROR = "error"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.BLOCKED,
)


def risk_rank(level: RiskLevel) -> int:
    return RISK_ORDER.index(level)


class RemediationStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    DRY_RUN = "dry_run"
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    PR_CREATED = "pr_created"


class RepositoryConfig(BaseModel):
    """One registry entry: where a repository lives and how it may be treated."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    url: str | None = None
    path: str | None = None
    default_branch: str = "main"
    terraform_root: str = "."
    environment: str = "dev"
    owner_email: str | None = None
    teams_target: str | None = None
    azure_subscription_id: str | None = None
    mode: Mode = Mode.OBSERVE
    schedule: str = "daily"
    enabled: bool = True
    var_files: list[str] = Field(default_factory=list)
    backend_configs: list[str] = Field(default_factory=list)
    auto_allowlist: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/**", "backend.tf", "providers.tf"]
    )

    @field_validator("terraform_root")
    @classmethod
    def _relative_root(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or ".." in value.split("/"):
            raise ValueError("terraform_root must be relative and inside the checkout")
        return value

    @field_validator("path")
    @classmethod
    def _no_empty_path(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("path must not be empty")
        return value

    @model_validator(mode="after")
    def _needs_location(self) -> RepositoryConfig:
        if not self.url and not self.path:
            raise ValueError("repository needs a url or a local path")
        return self

    @property
    def slug(self) -> str:
        """owner/name when a GitHub URL is configured, else the repository id."""
        if not self.url:
            return self.id
        cleaned = self.url.removesuffix(".git").rstrip("/")
        parts = cleaned.split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else self.id


class ChangedAttribute(BaseModel):
    name: str
    sensitive: bool = False


class DriftResource(BaseModel):
    address: str
    resource_id: str | None = None
    actions: list[str] = Field(default_factory=list)
    changed_attributes: list[ChangedAttribute] = Field(default_factory=list)
    risk: RiskDecision | None = None

    @property
    def attribute_names(self) -> list[str]:
        return [attribute.name for attribute in self.changed_attributes]


class ActivityEvidence(BaseModel):
    """A single Azure Activity Log record that matched a drift resource."""

    operation: str
    caller: str
    timestamp: str
    correlation_id: str | None = None
    resource_id: str | None = None
    resource_match: bool = False
    subscription_id: str | None = None


class RiskDecision(BaseModel):
    level: RiskLevel = RiskLevel.HIGH
    policy_rule: str = "default"
    requires_review: bool = True
    auto_allowed: bool = False
    reason: str = ""
    categories: list[str] = Field(default_factory=list)
    model_level: RiskLevel | None = None


class AnalysisSource(str, Enum):
    MODEL = "model"
    FALLBACK = "fallback"
    NONE = "none"


class ModelAnalysis(BaseModel):
    """Structured JSON the Foundry model must return."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    probable_cause: str = ""
    risk_recommendation: RiskLevel = RiskLevel.HIGH
    proposed_change: str = ""
    confidence: float = 0.0
    manual_review_reason: str | None = None
    source: AnalysisSource = AnalysisSource.MODEL
    deployment: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @field_validator("confidence")
    @classmethod
    def _bounded(cls, value: float) -> float:
        return min(max(value, 0.0), 1.0)


class ValidationStep(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ValidationResult(BaseModel):
    passed: bool = False
    steps: list[ValidationStep] = Field(default_factory=list)
    reject_reason: str | None = None

    @classmethod
    def rejected(cls, reason: str, steps: list[ValidationStep] | None = None) -> ValidationResult:
        return cls(passed=False, steps=steps or [], reject_reason=reason)


class RemediationResult(BaseModel):
    status: RemediationStatus = RemediationStatus.NOT_APPLICABLE
    patch: str | None = None
    patch_hash: str | None = None
    edited_files: list[str] = Field(default_factory=list)
    branch: str | None = None
    pr_url: str | None = None
    reject_reason: str | None = None
    analysis: ModelAnalysis | None = None
    validation: ValidationResult | None = None
    planned_actions: list[str] = Field(default_factory=list)


class ErrorInfo(BaseModel):
    stage: str
    reason: str
    exit_code: int | None = None


class NotificationResult(BaseModel):
    sent: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class DriftRecord(BaseModel):
    """The audit record described by section 9 of the design document."""

    scan_id: str
    correlation_id: str
    repository: str
    repository_slug: str | None = None
    commit_sha: str | None = None
    terraform_root: str = "."
    environment: str = "dev"
    mode: Mode = Mode.OBSERVE
    dry_run: bool = False
    detected_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: ScanStatus = ScanStatus.ERROR
    terraform_exit_code: int | None = None
    resources: list[DriftResource] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    plan_json_sha256: str | None = None
    azure_activity: list[ActivityEvidence] = Field(default_factory=list)
    risk: RiskDecision | None = None
    remediation: RemediationResult = Field(default_factory=RemediationResult)
    notification: NotificationResult = Field(default_factory=NotificationResult)
    error: ErrorInfo | None = None
    durations_ms: dict[str, int] = Field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Reduced, value-free view safe for logs and notifications."""
        return {
            "scan_id": self.scan_id,
            "repository": self.repository,
            "status": self.status.value,
            "mode": self.mode.value,
            "resources": [resource.address for resource in self.resources],
            "risk": self.risk.level.value if self.risk else None,
            "remediation": self.remediation.status.value,
            "pr_url": self.remediation.pr_url,
        }
