"""Repository registry (design document sections 8 and 15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from drift_agent.models import Mode, RepositoryConfig
from drift_agent.settings import DEFAULT_PROTECTED_PATHS


class RegistryError(ValueError):
    """Raised when the registry file is missing, malformed or inconsistent."""


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    repositories: list[RepositoryConfig] = Field(default_factory=list)

    def get(self, repository_id: str) -> RepositoryConfig | None:
        return next((repo for repo in self.repositories if repo.id == repository_id), None)

    def enabled(self) -> list[RepositoryConfig]:
        return [repo for repo in self.repositories if repo.enabled]

    def ids(self) -> list[str]:
        return [repo.id for repo in self.repositories]

    @classmethod
    def load(cls, path: Path) -> Registry:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RegistryError(f"registry_unreadable: {path}") from error
        try:
            document: Any = yaml.safe_load(raw) or {}
        except yaml.YAMLError as error:
            raise RegistryError(f"registry_invalid_yaml: {path}") from error
        if not isinstance(document, dict):
            raise RegistryError("registry_root_must_be_mapping")
        entries = document.get("repositories", document.get("repos", []))
        if not isinstance(entries, list):
            raise RegistryError("registry_repositories_must_be_a_list")
        try:
            registry = cls(version=int(document.get("version", 1)), repositories=entries)
        except ValidationError as error:
            raise RegistryError(f"registry_invalid_entry: {error.error_count()} problem(s)") from error
        registry.validate_unique_ids()
        return registry

    def validate_unique_ids(self) -> None:
        seen: set[str] = set()
        duplicates: list[str] = []
        for repo in self.repositories:
            if repo.id in seen:
                duplicates.append(repo.id)
            seen.add(repo.id)
        if duplicates:
            raise RegistryError(f"registry_duplicate_ids: {', '.join(sorted(duplicates))}")

    def problems(self, workspace: Path | None = None) -> list[str]:
        """Non-fatal findings surfaced by the validate-config command."""
        findings: list[str] = []
        for repo in self.repositories:
            if repo.mode is Mode.AUTO and not repo.auto_allowlist:
                findings.append(
                    f"{repo.id}: auto mode without an auto_allowlist is downgraded to pr"
                )
            if repo.path and workspace is not None:
                candidate = Path(repo.path)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                if not candidate.is_dir():
                    findings.append(f"{repo.id}: path does not exist: {repo.path}")
            if repo.url is None and repo.mode in {Mode.PR, Mode.AUTO}:
                findings.append(
                    f"{repo.id}: {repo.mode.value} mode needs a GitHub url; run with --dry-run"
                )
        return findings


def ad_hoc_repository(target: Path, mode: Mode = Mode.OBSERVE) -> RepositoryConfig:
    """Treat a local checkout as a one-off registry entry."""
    return RepositoryConfig(
        id=target.resolve().name.lower().replace(" ", "-") or "local",
        path=str(target),
        mode=mode,
        protected_paths=list(DEFAULT_PROTECTED_PATHS),
    )


def resolve_repository(
    target: str,
    registry: Registry | None = None,
    mode: Mode | None = None,
) -> RepositoryConfig:
    """Resolve a registry id, or fall back to an existing checkout path."""
    configured = registry.get(target) if registry is not None else None
    if configured is not None:
        return configured.model_copy(update={"mode": mode}) if mode else configured
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        return ad_hoc_repository(candidate, mode or Mode.OBSERVE)
    raise RegistryError(f"unknown_repository: {target}")
