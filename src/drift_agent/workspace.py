"""Checkout preparation and path containment.

The worker never scans a checkout in place: it copies the tree to a temporary
directory so Terraform writes and remediation edits cannot touch the source.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

COPY_IGNORES = (".git", ".terraform", ".venv", "__pycache__", "*.tfplan", "*.tfstate.backup")


class InvalidInput(ValueError):
    """Unsafe or unusable input, reported by reason only (never by value)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@contextmanager
def temporary_checkout(source: Path) -> Iterator[Path]:
    """Yield a disposable copy of the checkout, removed on exit."""
    with tempfile.TemporaryDirectory(prefix="drift-agent-") as temporary:
        checkout = Path(temporary).resolve() / "repo"
        shutil.copytree(source, checkout, ignore=shutil.ignore_patterns(*COPY_IGNORES))
        yield checkout


def resolve_inside(repository: Path, base: Path, relative: str) -> Path:
    """Resolve a user-supplied relative path, refusing to escape the checkout."""
    candidate = Path(relative)
    if candidate.is_absolute():
        raise InvalidInput("absolute_path_not_allowed")
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(repository.resolve()):
        raise InvalidInput("path_outside_checkout")
    return resolved


def terraform_root_of(checkout: Path, terraform_root: str) -> Path:
    root = resolve_inside(checkout, checkout, terraform_root)
    if not root.is_dir():
        raise InvalidInput("terraform_root_not_a_directory")
    if not any(root.glob("*.tf")) and not any(root.glob("*.tf.json")):
        raise InvalidInput("no_terraform_configuration_found")
    return root


def input_files(checkout: Path, root: Path, names: list[str]) -> list[Path]:
    paths = [resolve_inside(checkout, root, name) for name in names]
    if any(not path.is_file() for path in paths):
        raise InvalidInput("input_file_missing")
    return paths


def head_sha(checkout: Path) -> str | None:
    """Commit SHA of the source checkout, when it is a Git work tree."""
    if not (checkout / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None
