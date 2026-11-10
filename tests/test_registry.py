from pathlib import Path

import pytest
from conftest import ROOT, write_repo

from drift_agent.models import Mode
from drift_agent.registry import (
    Registry,
    RegistryError,
    ad_hoc_repository,
    resolve_repository,
)

EXAMPLE = ROOT / "config" / "repos.example.yaml"


def test_loads_example_registry():
    registry = Registry.load(EXAMPLE)
    assert [repo.id for repo in registry.repositories] == ["storage-prod", "network-dev"]
    assert registry.get("storage-prod").terraform_root == "environments/prod"
    assert registry.get("storage-prod").protected_paths
    assert registry.get("missing") is None
    assert len(registry.enabled()) == 2


def test_duplicate_ids_are_rejected(tmp_path: Path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        "repos:\n"
        "  - {id: one, path: /tmp}\n"
        "  - {id: one, path: /tmp}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError):
        Registry.load(path)


def test_invalid_entry_and_mode_are_rejected(tmp_path: Path):
    for body in ("repos:\n  - {id: 'Bad Id', path: /tmp}\n", "repos:\n  - {id: one, mode: nope, path: /tmp}\n"):
        path = tmp_path / "repos.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(RegistryError):
            Registry.load(path)


def test_entry_needs_a_location():
    from pydantic import ValidationError

    from drift_agent.models import RepositoryConfig

    with pytest.raises(ValidationError):
        RepositoryConfig(id="lonely")


def test_terraform_root_cannot_escape_checkout():
    from pydantic import ValidationError

    from drift_agent.models import RepositoryConfig

    for root in ("/etc", "../outside"):
        with pytest.raises(ValidationError):
            RepositoryConfig(id="x", path="/tmp", terraform_root=root)


def test_resolve_by_id_preserves_registry_mode(tmp_path: Path):
    path = tmp_path / "repos.yaml"
    path.write_text("repos:\n  - {id: one, path: /tmp, mode: pr}\n", encoding="utf-8")
    registry = Registry.load(path)
    assert resolve_repository("one", registry).mode is Mode.PR
    assert resolve_repository("one", registry, Mode.OBSERVE).mode is Mode.OBSERVE


def test_resolve_by_path_creates_ad_hoc_entry(tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    repo = resolve_repository(str(checkout))
    assert repo.path == str(checkout)
    assert repo.mode is Mode.OBSERVE
    assert repo.protected_paths
    with pytest.raises(RegistryError):
        resolve_repository(str(tmp_path / "nope"))


def test_problems_report_auto_mode_without_allowlist(tmp_path: Path):
    checkout = write_repo(tmp_path / "checkout")
    entry = ad_hoc_repository(checkout)
    registry = Registry(repositories=[entry.model_copy(update={"mode": Mode.AUTO})])
    problems = registry.problems()
    assert any("auto mode" in problem for problem in problems)
    assert any("needs a GitHub url" in problem for problem in problems)


def test_problems_report_missing_path(tmp_path: Path):
    path = tmp_path / "repos.yaml"
    path.write_text("repos:\n  - {id: gone, path: does-not-exist}\n", encoding="utf-8")
    problems = Registry.load(path).problems(workspace=tmp_path)
    assert problems and "does not exist" in problems[0]
