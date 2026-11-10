"""Environment-driven configuration.

Every environment-specific value is a variable with a placeholder default, so the
package imports and runs offline without inventing tenant IDs, endpoints or
credentials (design document section 15).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROTECTED_PATHS = [".github/workflows/**", "backend.tf", "providers.tf"]


def _flag(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class FoundrySettings:
    endpoint: str | None = None
    deployment: str | None = None
    api_version: str = "2024-10-21"
    api_key: str | None = None
    timeout: int = 60
    max_repository_context: int = 12000

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.deployment)

    @property
    def chat_url(self) -> str:
        base = (self.endpoint or "").rstrip("/")
        if "/chat/completions" in base:
            return base
        return f"{base}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"


@dataclass(frozen=True)
class AzureSettings:
    tenant_id: str | None = None
    subscription_id: str | None = None
    activity_lookback_minutes: int = 120
    activity_max_records: int = 20

    @property
    def configured(self) -> bool:
        return bool(self.tenant_id)


@dataclass(frozen=True)
class GitHubSettings:
    app_id: str | None = None
    installation_id: str | None = None
    private_key_path: str | None = None
    api_url: str = "https://api.github.com"
    timeout: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key_path)


@dataclass(frozen=True)
class TeamsSettings:
    webhook_url: str | None = None
    timeout: int = 15

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)


@dataclass(frozen=True)
class EmailSettings:
    sender: str | None = None
    graph_url: str = "https://graph.microsoft.com/v1.0"
    timeout: int = 20

    @property
    def configured(self) -> bool:
        return bool(self.sender)


@dataclass(frozen=True)
class Settings:
    registry_path: Path = Path("config/repos.yaml")
    policy_path: Path = Path("config/risk-policy.yaml")
    skill_path: Path = Path("skills/terraform-drift/SKILL.md")
    audit_dir: Path = Path("reports/audit")
    terraform_timeout: int = 600
    max_concurrency: int = 5
    tflint_enabled: bool = False
    dry_run: bool = False
    log_level: str = "INFO"
    foundry: FoundrySettings = field(default_factory=FoundrySettings)
    azure: AzureSettings = field(default_factory=AzureSettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    teams: TeamsSettings = field(default_factory=TeamsSettings)
    email: EmailSettings = field(default_factory=EmailSettings)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = environ if environ is not None else os.environ
        return cls(
            registry_path=Path(env.get("DRIFT_REPOS_CONFIG", "config/repos.yaml")),
            policy_path=Path(env.get("DRIFT_RISK_POLICY", "config/risk-policy.yaml")),
            skill_path=Path(env.get("DRIFT_SKILL_PATH", "skills/terraform-drift/SKILL.md")),
            audit_dir=Path(env.get("DRIFT_AUDIT_DIR", "reports/audit")),
            terraform_timeout=_int(env, "DRIFT_TERRAFORM_TIMEOUT", 600),
            max_concurrency=_int(env, "DRIFT_MAX_CONCURRENCY", 5),
            tflint_enabled=_flag(env, "DRIFT_TFLINT_ENABLED", False),
            dry_run=_flag(env, "DRIFT_DRY_RUN", False),
            log_level=env.get("DRIFT_LOG_LEVEL", "INFO").upper(),
            foundry=FoundrySettings(
                endpoint=env.get("FOUNDRY_ENDPOINT") or None,
                deployment=env.get("FOUNDRY_DEPLOYMENT") or None,
                api_version=env.get("FOUNDRY_API_VERSION", "2024-10-21"),
                api_key=env.get("FOUNDRY_API_KEY") or None,
                timeout=_int(env, "FOUNDRY_TIMEOUT", 60),
            ),
            azure=AzureSettings(
                tenant_id=env.get("AZURE_TENANT_ID") or None,
                subscription_id=env.get("AZURE_SUBSCRIPTION_ID") or None,
                activity_lookback_minutes=_int(env, "AZURE_ACTIVITY_LOOKBACK_MINUTES", 120),
                activity_max_records=_int(env, "AZURE_ACTIVITY_MAX_RECORDS", 20),
            ),
            github=GitHubSettings(
                app_id=env.get("GITHUB_APP_ID") or None,
                installation_id=env.get("GITHUB_APP_INSTALLATION_ID") or None,
                private_key_path=env.get("GITHUB_APP_PRIVATE_KEY_PATH") or None,
                api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            ),
            teams=TeamsSettings(webhook_url=env.get("TEAMS_WEBHOOK_URL") or None),
            email=EmailSettings(sender=env.get("GRAPH_SENDER") or None),
        )

    def missing_credential_notes(self) -> list[str]:
        """Human-readable list of unconfigured integrations; never secrets."""
        notes = []
        if not self.foundry.configured:
            notes.append("FOUNDRY_ENDPOINT / FOUNDRY_DEPLOYMENT unset: model explanation disabled")
        if not self.azure.configured:
            notes.append("AZURE_TENANT_ID unset: Azure Activity correlation disabled")
        if not self.github.configured:
            notes.append("GitHub App variables unset: pull request creation disabled")
        if not self.teams.configured:
            notes.append("TEAMS_WEBHOOK_URL unset: Teams notification disabled")
        return notes

