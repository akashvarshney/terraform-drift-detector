import pytest
from conftest import POLICY_PATH
from pydantic import ValidationError

from drift_agent.models import DriftResource, Mode, RiskDecision, RiskLevel
from drift_agent.policy import (
    PolicyRule,
    RiskPolicy,
    aggregate,
    apply_model_recommendation,
    effective_mode,
    patch_allowed,
)


def resource(*, actions=None, attributes=(), resource_id=None) -> DriftResource:
    return DriftResource(
        address="module.storage.azurerm_storage_account.this",
        resource_id=resource_id,
        actions=list(actions or ["update"]),
        changed_attributes=[{"name": name} for name in attributes],
    )


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy.load(POLICY_PATH)


def test_shipped_policy_is_consistent(policy: RiskPolicy):
    assert policy.problems() == []
    assert "tags-metadata-only" in policy.rule_ids()
    assert policy.default.level is RiskLevel.HIGH


def test_tag_only_drift_is_low_and_auto_eligible(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["tags"]))
    assert decision.level is RiskLevel.LOW
    assert decision.auto_allowed is True
    assert decision.requires_review is False
    assert decision.policy_rule == "tags-metadata-only"


def test_network_exposure_is_high_and_needs_review(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["public_network_access_enabled"]))
    assert decision.level is RiskLevel.HIGH
    assert decision.requires_review is True
    assert patch_allowed(decision) is False


def test_identity_rbac_is_high(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["identity"]))
    assert decision.level is RiskLevel.HIGH
    assert decision.categories == ["identity"]


def test_deletion_is_blocked_and_never_auto(policy: RiskPolicy):
    decision = policy.classify(resource(actions=["delete"], attributes=["tags"]))
    assert decision.level is RiskLevel.BLOCKED
    assert decision.requires_review is True
    assert decision.auto_allowed is False
    assert patch_allowed(decision) is False


def test_replacement_is_blocked(policy: RiskPolicy):
    decision = policy.classify(resource(actions=["delete", "create"], attributes=["name"]))
    assert decision.level is RiskLevel.BLOCKED
    assert decision.policy_rule == "resource-replacement"


def test_unknown_attribute_falls_back_to_high_default(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["something_new"]))
    assert decision.level is RiskLevel.HIGH
    assert decision.policy_rule == "default"


def test_simple_module_input_update_is_medium(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["owner"]))
    assert decision.level is RiskLevel.MEDIUM
    assert patch_allowed(decision) is True


def test_model_may_escalate_but_never_relax(policy: RiskPolicy):
    decision = policy.classify(resource(attributes=["tags"]))
    escalated = apply_model_recommendation(decision, RiskLevel.HIGH)
    assert escalated.level is RiskLevel.HIGH
    assert escalated.requires_review is True
    assert escalated.auto_allowed is False

    relaxed = apply_model_recommendation(escalated, RiskLevel.LOW)
    assert relaxed.level is RiskLevel.HIGH
    assert relaxed.model_level is RiskLevel.LOW


def test_aggregate_takes_the_severest_decision():
    policy = RiskPolicy.load(POLICY_PATH)
    decisions = [
        policy.classify(resource(attributes=["tags"])),
        policy.classify(resource(attributes=["public_network_access_enabled"])),
    ]
    combined = aggregate(decisions)
    assert combined.level is RiskLevel.HIGH
    assert combined.requires_review is True
    assert combined.auto_allowed is False


def test_aggregate_without_resources_requires_review():
    combined = aggregate([])
    assert combined.level is RiskLevel.HIGH
    assert combined.requires_review is True


def test_auto_mode_requires_allowlist_and_low_risk(policy: RiskPolicy):
    low = policy.classify(resource(attributes=["tags"]))
    high = policy.classify(resource(attributes=["public_network_access_enabled"]))
    assert effective_mode(Mode.AUTO, low, ["tags-metadata-only"]) is Mode.AUTO
    assert effective_mode(Mode.AUTO, low, []) is Mode.PR
    assert effective_mode(Mode.AUTO, high, ["tags-metadata-only"]) is Mode.PR
    assert effective_mode(Mode.OBSERVE, low, ["tags-metadata-only"]) is Mode.OBSERVE


def test_rules_must_constrain_and_respect_auto_rules():
    with pytest.raises(ValidationError):
        PolicyRule(id="anything", level=RiskLevel.LOW)
    with pytest.raises(ValidationError):
        PolicyRule(id="auto-high", level=RiskLevel.HIGH, auto_allowed=True, attributes_any=["x"])
    blocked = PolicyRule(
        id="blocked", level=RiskLevel.BLOCKED, auto_allowed=True, attributes_any=["x"]
    )
    assert blocked.auto_allowed is False and blocked.requires_review is True


def test_glob_and_all_matching():
    rule = PolicyRule(id="tags", level=RiskLevel.LOW, attributes_all=["tag*", "owner"])
    assert rule.matches(resource(attributes=["tags", "owner"])) is True
    assert rule.matches(resource(attributes=["tags"])) is False
    assert rule.matches(resource(attributes=[])) is False

    any_rule = PolicyRule(id="any", level=RiskLevel.LOW, attributes_any=["*network*"])
    assert any_rule.matches(resource(attributes=["virtual_network_name"])) is True
    assert any_rule.matches(resource(attributes=["tags"])) is False


def test_default_decision_is_not_shared_between_calls(policy: RiskPolicy):
    first = policy.classify(resource(attributes=["unexpected"]))
    first.categories.append("mutated")
    second = policy.classify(resource(attributes=["unexpected"]))
    assert second.categories == []
    assert isinstance(policy.default, RiskDecision)
