"""Deterministic risk classification.

The model may recommend a risk level, but it can only ever *raise* severity here.
Hard policy rules decide whether a patch may exist at all (design document
sections 5 and 15).
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from drift_agent.models import (
    DriftResource,
    Mode,
    RemediationStatus,
    RiskDecision,
    RiskLevel,
    risk_rank,
)


class PolicyError(ValueError):
    """Raised when the risk policy file is missing or unusable."""


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    level: RiskLevel
    requires_review: bool = True
    auto_allowed: bool = False
    reason: str = ""
    category: str = "general"
    actions_any: list[str] = Field(default_factory=list)
    actions_all: list[str] = Field(default_factory=list)
    attributes_any: list[str] = Field(default_factory=list)
    attributes_all: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _must_constrain(self) -> PolicyRule:
        if not (
            self.actions_any
            or self.actions_all
            or self.attributes_any
            or self.attributes_all
        ):
            raise ValueError(f"policy rule '{self.id}' must constrain actions or attributes")
        if self.level is RiskLevel.BLOCKED:
            self.requires_review = True
            self.auto_allowed = False
        if self.auto_allowed and self.level is not RiskLevel.LOW:
            raise ValueError(f"policy rule '{self.id}' may only auto-allow low risk")
        return self

    def matches(self, resource: DriftResource) -> bool:
        actions = {action.casefold() for action in resource.actions}
        names = [name.casefold() for name in resource.attribute_names]

        if self.actions_any and not actions & {a.casefold() for a in self.actions_any}:
            return False
        if self.actions_all and not {a.casefold() for a in self.actions_all} <= actions:
            return False
        if self.attributes_any and not any(
            fnmatchcase(name, pattern.casefold())
            for name in names
            for pattern in self.attributes_any
        ):
            return False
        return all(
            any(fnmatchcase(name, pattern.casefold()) for name in names)
            for pattern in self.attributes_all
        )


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default: RiskDecision = Field(
        default_factory=lambda: RiskDecision(
            level=RiskLevel.HIGH,
            policy_rule="default",
            requires_review=True,
            auto_allowed=False,
            reason="Unclassified drift defaults to manual review.",
        )
    )
    rules: list[PolicyRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> RiskPolicy:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise PolicyError(f"policy_unreadable: {path}") from error
        try:
            document: Any = yaml.safe_load(raw) or {}
        except yaml.YAMLError as error:
            raise PolicyError(f"policy_invalid_yaml: {path}") from error
        if not isinstance(document, dict):
            raise PolicyError("policy_root_must_be_mapping")
        default = document.get("default", {})
        if not isinstance(default, dict):
            raise PolicyError("policy_default_must_be_mapping")
        return cls(version=int(document.get("version", 1)),
                   default=RiskDecision.model_validate(
                       {"policy_rule": "default", **default}
                   ),
                   rules=document.get("rules", []))

    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self.rules]

    def problems(self) -> list[str]:
        findings: list[str] = []
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                findings.append(f"duplicate policy rule id: {rule.id}")
            seen.add(rule.id)
        if self.default.level is not RiskLevel.HIGH:
            findings.append("default risk should stay high so unknown drift needs review")
        return findings

    def classify(self, resource: DriftResource) -> RiskDecision:
        for rule in self.rules:
            if rule.matches(resource):
                return RiskDecision(
                    level=rule.level,
                    policy_rule=rule.id,
                    requires_review=rule.requires_review,
                    auto_allowed=rule.auto_allowed,
                    reason=rule.reason,
                    categories=[rule.category],
                )
        return self.default.model_copy(deep=True)

    def classify_all(self, resources: list[DriftResource]) -> RiskDecision:
        decisions = [self.classify(resource) for resource in resources]
        return aggregate(decisions)


def aggregate(decisions: list[RiskDecision]) -> RiskDecision:
    """Record-level decision: the severest resource decision wins."""
    if not decisions:
        return RiskDecision(
            level=RiskLevel.HIGH,
            policy_rule="no_resources",
            requires_review=True,
            auto_allowed=False,
            reason="Drift was reported without resource-level changes.",
        )
    severest = max(decisions, key=lambda decision: risk_rank(decision.level))
    categories: list[str] = []
    for decision in decisions:
        for category in decision.categories:
            if category not in categories:
                categories.append(category)
    return RiskDecision(
        level=severest.level,
        policy_rule=severest.policy_rule,
        requires_review=any(decision.requires_review for decision in decisions),
        auto_allowed=all(decision.auto_allowed for decision in decisions)
        and severest.level is RiskLevel.LOW,
        reason=severest.reason,
        categories=categories,
        model_level=severest.model_level,
    )


def apply_model_recommendation(decision: RiskDecision, recommended: RiskLevel) -> RiskDecision:
    """Clamp a model recommendation onto the deterministic decision.

    The model may escalate; it can never relax the policy outcome.
    """
    updated = decision.model_copy(deep=True)
    updated.model_level = recommended
    if risk_rank(recommended) > risk_rank(decision.level):
        updated.level = recommended
        updated.requires_review = True
        updated.auto_allowed = False
        updated.reason = f"{decision.reason} Model escalated to {recommended.value}.".strip()
    return updated


def effective_mode(mode: Mode, decision: RiskDecision | None, auto_allowlist: list[str]) -> Mode:
    """AUTO is inert unless the policy rule and the repository both allow it."""
    if mode is not Mode.AUTO:
        return mode
    if decision is None or not decision.auto_allowed or decision.level is not RiskLevel.LOW:
        return Mode.PR
    if decision.policy_rule not in auto_allowlist:
        return Mode.PR
    return Mode.AUTO


PATCHABLE_LEVELS = frozenset({RiskLevel.LOW, RiskLevel.MEDIUM})


def patch_allowed(decision: RiskDecision) -> bool:
    """High and blocked drift never receive an autonomous patch."""
    return decision.level in PATCHABLE_LEVELS


def remediation_status_for(mode: Mode, decision: RiskDecision) -> RemediationStatus:
    if decision.level is RiskLevel.BLOCKED:
        return RemediationStatus.NEEDS_MANUAL_REVIEW
    if mode is Mode.OBSERVE:
        return RemediationStatus.NOT_APPLICABLE
    return RemediationStatus.PROPOSED
