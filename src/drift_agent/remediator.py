"""Repository-aware remediation with deterministic guards.

The model proposes a unified diff; this module decides whether it is even
allowed to exist, then applies it all-or-nothing inside the disposable checkout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from drift_agent.models import (
    Mode,
    ModelAnalysis,
    RemediationResult,
    RemediationStatus,
)
from drift_agent.redaction import hash_payload

DEFAULT_MAX_FILES = 3
DEFAULT_MAX_CHANGED_LINES = 60
DEFAULT_MIN_CONFIDENCE = 0.6

# Always refused, independent of registry configuration.
HARD_BLOCKED_PATTERNS = (
    ".github/workflows/*",
    ".github/workflows/**",
    "backend.tf",
    "providers.tf",
    "versions.tf",
    ".terraform.lock.hcl",
    "*.tfbackend",
    "*.pem",
    "*.key",
)
_ADDED_STANDALONE_RESOURCE = re.compile(r'^\+\s*resource\s+"azurerm_[a-z0-9_]+"')
_ADDED_PROVIDER_OR_BACKEND = re.compile(
    r"^\+\s*(required_providers\b|backend\s+\"|provider\s+\")", re.MULTILINE
)
_HUNK = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))?"
                   r" \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


class PatchRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _FilePatch:
    path: str
    hunks: list[tuple[int, int, list[str]]]
    create: bool


@dataclass(frozen=True)
class PatchScope:
    files: tuple[str, ...]
    changed_lines: int


def _clean_header_path(value: str) -> str:
    """Strip the a/ b/ prefixes and any trailing timestamp."""
    path = value.strip().split("\t")[0].split(" ")[0]
    if path in {"/dev/null", "dev/null"}:
        return "/dev/null"
    return path[2:] if path.startswith(("a/", "b/")) else path


def is_file_header(lines: list[str], index: int) -> bool:
    return (
        lines[index].startswith("--- ")
        and index + 1 < len(lines)
        and lines[index + 1].startswith("+++ ")
    )


def parse_patch(patch: str) -> list[_FilePatch]:
    lines = patch.splitlines()
    files: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        if not is_file_header(lines, index):
            index += 1
            continue
        old_path = _clean_header_path(lines[index][4:])
        new_path = _clean_header_path(lines[index + 1][4:])
        if new_path == "/dev/null":
            raise PatchRejected("patch_deletes_file_blocked")
        path = new_path if new_path != "/dev/null" else old_path
        create = old_path == "/dev/null"
        index += 2
        hunks: list[tuple[int, int, list[str]]] = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK.match(lines[index])
            if not match:
                raise PatchRejected("invalid_patch_hunk")
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or 1)
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].startswith("@@") and not is_file_header(lines, index):
                body.append(lines[index])
                index += 1
            hunks.append((old_start, old_count, body))
        if not hunks:
            raise PatchRejected("empty_patch_hunk")
        files.append(_FilePatch(path=path, hunks=hunks, create=create))
    if not files:
        raise PatchRejected("empty_patch")
    return files


def patch_scope(patch: str) -> PatchScope:
    files = parse_patch(patch)
    changed = sum(
        1
        for line in patch.splitlines()
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    )
    return PatchScope(files=tuple(item.path for item in files), changed_lines=changed)


def changed_line_count(patch: str) -> int:
    return patch_scope(patch).changed_lines


def repository_uses_modules(workdir: Path) -> bool:
    for path in workdir.rglob("*.tf"):
        try:
            if re.search(r'^\s*module\s+"', path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
                return True
        except OSError:
            continue
    return False


def added_standalone_azure_resources(patch: str) -> list[str]:
    return [
        line.strip() for line in patch.splitlines()
        if _ADDED_STANDALONE_RESOURCE.match(line)
    ]


def _apply_hunks(original: list[str], hunks: list[tuple[int, int, list[str]]]) -> list[str]:
    result: list[str] = []
    cursor = 0
    for old_start, old_count, body in hunks:
        target = old_start - 1 if old_count > 0 else old_start
        if target < cursor:
            raise PatchRejected("patch_hunk_overlap")
        if target > len(original):
            raise PatchRejected("patch_context_mismatch")
        result.extend(original[cursor:target])
        cursor = target
        for line in body:
            if line.startswith("\\"):
                continue
            if line == "":
                if cursor < len(original) and original[cursor] == "":
                    result.append("")
                    cursor += 1
                continue
            marker, text = line[0], line[1:]
            if marker == "+":
                result.append(text)
            elif marker in {"-", " "}:
                if cursor >= len(original) or original[cursor] != text:
                    raise PatchRejected("patch_context_mismatch")
                if marker == " ":
                    result.append(text)
                cursor += 1
            else:
                raise PatchRejected("invalid_patch_line")
    result.extend(original[cursor:])
    return result


def apply_unified_diff(workdir: Path, patch: str) -> list[str]:
    """Apply every file in memory first, then write: never a partial edit."""
    root = workdir.resolve()
    planned: dict[Path, str] = {}
    for file_patch in parse_patch(patch):
        if not file_patch.path or file_patch.path.startswith("/"):
            raise PatchRejected("patch_path_outside_checkout")
        target = (root / file_patch.path).resolve()
        if not target.is_relative_to(root):
            raise PatchRejected("patch_path_outside_checkout")
        if target.is_dir():
            raise PatchRejected("patch_targets_directory")
        if file_patch.create:
            original_text = ""
            original = []
        else:
            if not target.exists():
                raise PatchRejected("patch_target_missing")
            original_text = target.read_text(encoding="utf-8", errors="replace")
            original = original_text.splitlines()
        patched = _apply_hunks(original, file_patch.hunks)
        trailing = original_text.endswith("\n") if original_text else True
        planned[target] = "\n".join(patched) + ("\n" if trailing and patched else "")
    for target, content in planned.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return [str(path.relative_to(root)) for path in sorted(planned)]


def _path_blocked(path: str, protected_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    patterns = (*HARD_BLOCKED_PATTERNS, *(pattern.replace("\\", "/") for pattern in protected_paths))
    return any(
        fnmatchcase(normalized, pattern) or fnmatchcase(normalized, pattern.lstrip("./"))
        for pattern in patterns
    )


class Remediator:
    """Guards, mode gating and patch application."""

    def __init__(
        self,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.max_files = max_files
        self.max_changed_lines = max_changed_lines
        self.min_confidence = min_confidence

    def evaluate(
        self,
        *,
        workdir: Path,
        analysis: ModelAnalysis | None,
        protected_paths: list[str],
        mode: Mode,
        dry_run: bool,
    ) -> RemediationResult:
        if mode is Mode.OBSERVE:
            return RemediationResult(
                status=RemediationStatus.NOT_APPLICABLE,
                analysis=analysis,
                reject_reason="observe_mode_makes_no_code_changes",
            )
        if analysis is None:
            return RemediationResult(
                status=RemediationStatus.NEEDS_MANUAL_REVIEW,
                reject_reason="model_unavailable",
            )
        patch = (analysis.proposed_change or "").strip()
        if analysis.manual_review_reason and not patch:
            return RemediationResult(
                status=RemediationStatus.NEEDS_MANUAL_REVIEW,
                reject_reason=analysis.manual_review_reason,
                analysis=analysis,
            )
        if not patch:
            return RemediationResult(
                status=RemediationStatus.NEEDS_MANUAL_REVIEW,
                reject_reason="no_safe_change_proposed",
                analysis=analysis,
            )
        if analysis.confidence < self.min_confidence:
            return RemediationResult(
                status=RemediationStatus.NEEDS_MANUAL_REVIEW,
                reject_reason="model_confidence_too_low",
                patch=patch,
                patch_hash=hash_payload(patch),
                analysis=analysis,
            )
        try:
            scope = self._guard(workdir, patch, protected_paths)
        except PatchRejected as rejection:
            return RemediationResult(
                status=RemediationStatus.REJECTED,
                reject_reason=rejection.reason,
                patch=patch,
                patch_hash=hash_payload(patch),
                analysis=analysis,
            )
        planned_actions = ["apply_patch", "terraform_fmt", "terraform_validate", "terraform_plan"]
        if dry_run:
            return RemediationResult(
                status=RemediationStatus.DRY_RUN,
                patch=patch,
                patch_hash=hash_payload(patch),
                edited_files=list(scope.files),
                planned_actions=planned_actions,
                analysis=analysis,
            )
        try:
            edited = apply_unified_diff(workdir, patch)
        except PatchRejected as rejection:
            return RemediationResult(
                status=RemediationStatus.REJECTED,
                reject_reason=rejection.reason,
                patch=patch,
                patch_hash=hash_payload(patch),
                analysis=analysis,
            )
        return RemediationResult(
            status=RemediationStatus.PROPOSED,
            patch=patch,
            patch_hash=hash_payload(patch),
            edited_files=edited,
            planned_actions=planned_actions,
            analysis=analysis,
        )

    def _guard(self, workdir: Path, patch: str, protected_paths: list[str]) -> PatchScope:
        scope = patch_scope(patch)
        for path in scope.files:
            if not path or path.startswith("/") or ".." in Path(path).parts:
                raise PatchRejected("patch_path_outside_checkout")
        blocked = sorted(path for path in scope.files if _path_blocked(path, protected_paths))
        if blocked:
            raise PatchRejected(f"protected_path_blocked: {blocked[0]}")
        if len(scope.files) > self.max_files:
            raise PatchRejected("patch_scope_too_large_files")
        if scope.changed_lines > self.max_changed_lines:
            raise PatchRejected("patch_scope_too_large_lines")
        if _ADDED_PROVIDER_OR_BACKEND.search(patch):
            raise PatchRejected("provider_or_backend_edit_blocked")
        if added_standalone_azure_resources(patch) and repository_uses_modules(workdir):
            raise PatchRejected("standalone_resource_where_module_expected")
        return scope
