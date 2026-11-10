from pathlib import Path

import pytest
from conftest import TAG_PATCH, write_repo

from drift_agent.models import Mode, ModelAnalysis, RemediationStatus, RiskLevel
from drift_agent.remediator import (
    PatchRejected,
    Remediator,
    apply_unified_diff,
    changed_line_count,
    repository_uses_modules,
)

MODULE_PATCH = (
    "--- a/storage.tf\n"
    "+++ b/storage.tf\n"
    "@@ -1,4 +1,4 @@\n"
    ' module "storage" {\n'
    '   source = "./modules/storage"\n'
    '-  owner  = "old-owner"\n'
    '+  owner  = "platform"\n'
    " }\n"
)


def analysis(patch: str | None = TAG_PATCH, *, confidence: float = 0.9) -> ModelAnalysis:
    return ModelAnalysis(
        summary="change",
        probable_cause="manual edit",
        risk_recommendation=RiskLevel.LOW,
        proposed_change=patch or "",
        confidence=confidence,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return write_repo(tmp_path / "checkout")


def test_patch_scope_counts_real_changes():
    assert changed_line_count(TAG_PATCH) == 2


def test_apply_patch_rewrites_file(workdir: Path):
    edited = apply_unified_diff(workdir, TAG_PATCH)
    assert edited == ["variables.tf"]
    assert (workdir / "variables.tf").read_text(encoding="utf-8") == (
        'variable "owner" {\n  default = "platform"\n}\n'
    )


def test_context_mismatch_is_rejected_and_file_untouched(workdir: Path):
    broken = TAG_PATCH.replace(' default = "old-owner"', ' default = "does-not-exist"')
    with pytest.raises(PatchRejected) as caught:
        apply_unified_diff(workdir, broken)
    assert caught.value.reason == "patch_context_mismatch"
    assert 'default = "old-owner"' in (workdir / "variables.tf").read_text(encoding="utf-8")


def test_escape_and_deletion_are_rejected(workdir: Path):
    for patch, reason in (
        (
            "--- a/../evil.tf\n+++ b/../evil.tf\n@@ -1 +1 @@\n-x\n+y\n",
            "patch_path_outside_checkout",
        ),
        (
            "--- a/variables.tf\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-a\n-b\n-c\n",
            "patch_deletes_file_blocked",
        ),
    ):
        result = Remediator().evaluate(
            workdir=workdir,
            analysis=analysis(patch),
            protected_paths=[],
            mode=Mode.PR,
            dry_run=False,
        )
        assert result.status is RemediationStatus.REJECTED
        assert result.reject_reason == reason


def test_protected_paths_are_blocked(workdir: Path):
    patch = (
        "--- a/providers.tf\n"
        "+++ b/providers.tf\n"
        "@@ -1,1 +1,2 @@\n"
        ' provider "azurerm" {\n'
        "+  skip_provider_registration = true\n"
    )
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(patch),
        protected_paths=[".github/workflows/**"],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.REJECTED
    assert result.reject_reason.startswith("protected_path_blocked")


def test_registry_protected_paths_are_honoured(workdir: Path):
    (workdir / "custom.tf").write_text("# custom\n", encoding="utf-8")
    patch = "--- a/custom.tf\n+++ b/custom.tf\n@@ -1 +1,2 @@\n # custom\n+x = 1\n"
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(patch),
        protected_paths=["custom.tf"],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.REJECTED


def test_standalone_resource_is_refused_when_modules_are_used(tmp_path: Path):
    workdir = write_repo(tmp_path / "checkout", module=True)
    assert repository_uses_modules(workdir) is True
    patch = (
        "--- a/main.tf\n"
        "+++ b/main.tf\n"
        "@@ -1,3 +1,5 @@\n"
        " terraform {\n"
        '   required_version = ">= 1.5.0"\n'
        " }\n"
        '+\n+resource "azurerm_storage_account" "standalone" {\n+}\n'
    )
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(patch),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.REJECTED
    assert result.reject_reason == "standalone_resource_where_module_expected"


def test_module_preserving_patch_is_accepted(tmp_path: Path):
    workdir = write_repo(tmp_path / "checkout", module=True)
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(MODULE_PATCH),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.PROPOSED
    assert result.edited_files == ["storage.tf"]
    assert 'owner  = "platform"' in (workdir / "storage.tf").read_text(encoding="utf-8")


def test_provider_configuration_edits_are_blocked(workdir: Path):
    patch = (
        "--- a/main.tf\n"
        "+++ b/main.tf\n"
        "@@ -1,3 +1,5 @@\n"
        " terraform {\n"
        '   required_version = ">= 1.5.0"\n'
        " }\n"
        '+\n+provider "azurerm" {\n+  features {}\n+}\n'
    )
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(patch),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.REJECTED
    assert result.reject_reason == "provider_or_backend_edit_blocked"


def test_large_scope_is_rejected(workdir: Path):
    patch = "".join(
        f"--- a/file{index}.tf\n+++ b/file{index}.tf\n@@ -1 +1,2 @@\n # f\n+x = {index}\n"
        for index in range(4)
    )
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(patch),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=False,
    )
    assert result.status is RemediationStatus.REJECTED
    assert result.reject_reason == "patch_scope_too_large_files"


def test_dry_run_plans_without_touching_files(workdir: Path):
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=True,
    )
    assert result.status is RemediationStatus.DRY_RUN
    assert "create_draft_pr" not in result.planned_actions
    assert result.patch_hash
    assert 'default = "old-owner"' in (workdir / "variables.tf").read_text(encoding="utf-8")


def test_observe_mode_makes_no_changes(workdir: Path):
    result = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(),
        protected_paths=[],
        mode=Mode.OBSERVE,
        dry_run=False,
    )
    assert result.status is RemediationStatus.NOT_APPLICABLE


def test_missing_patch_and_low_confidence_need_review(workdir: Path):
    missing = Remediator().evaluate(
        workdir=workdir, analysis=analysis(None), protected_paths=[], mode=Mode.PR, dry_run=False
    )
    assert missing.status is RemediationStatus.NEEDS_MANUAL_REVIEW
    assert missing.reject_reason == "no_safe_change_proposed"

    unsure = Remediator().evaluate(
        workdir=workdir,
        analysis=analysis(confidence=0.2),
        protected_paths=[],
        mode=Mode.PR,
        dry_run=False,
    )
    assert unsure.status is RemediationStatus.NEEDS_MANUAL_REVIEW
    assert unsure.reject_reason == "model_confidence_too_low"


def test_manual_review_reason_is_preserved(workdir: Path):
    unsure = ModelAnalysis(
        summary="unclear",
        probable_cause="unknown",
        risk_recommendation=RiskLevel.HIGH,
        proposed_change="",
        confidence=0.9,
        manual_review_reason="insufficient evidence",
    )
    result = Remediator().evaluate(
        workdir=workdir, analysis=unsure, protected_paths=[], mode=Mode.PR, dry_run=False
    )
    assert result.reject_reason == "insufficient evidence"
