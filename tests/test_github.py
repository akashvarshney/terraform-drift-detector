import pytest

from drift_agent.github.client import (
    AppGitHubClient,
    FakeGitHubClient,
    GitHubUnavailable,
)
from drift_agent.models import RepositoryConfig
from drift_agent.settings import GitHubSettings

REPO = RepositoryConfig(id="pilot", url="https://github.com/example-org/pilot.git")


def test_fake_client_records_intended_calls():
    client = FakeGitHubClient()
    metadata = client.get_metadata(REPO)
    assert metadata.slug == "example-org/pilot"
    client.create_branch(REPO, "drift-fix/abc", metadata.head_sha)
    url = client.create_draft_pr(
        REPO, head="drift-fix/abc", base="main", title="t", body="b"
    )
    assert url.endswith("/pull/1")
    assert [call.operation for call in client.calls] == [
        "get_metadata",
        "create_branch",
        "create_draft_pr",
    ]


def test_unconfigured_app_client_is_unavailable():
    client = AppGitHubClient(GitHubSettings())
    with pytest.raises(GitHubUnavailable):
        client.get_metadata(REPO)


def test_missing_private_key_is_unavailable():
    client = AppGitHubClient(
        GitHubSettings(app_id="1", installation_id="2", private_key_path="/nonexistent.pem")
    )
    with pytest.raises(GitHubUnavailable):
        client.get_metadata(REPO)


def test_pr_flow_uses_draft_pull_requests():
    payloads: list[tuple[str, str, dict]] = []

    def fake_request(method, path, payload=None):
        payloads.append((method, path, payload or {}))
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "a" * 40}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "basetree"}}
        if path.endswith("/git/blobs"):
            return {"sha": "blob"}
        if path.endswith("/git/trees"):
            return {"sha": "newtree"}
        if path.endswith("/git/commits") and method == "POST":
            return {"sha": "commit"}
        if path.endswith("/pulls"):
            return {"html_url": "https://github.com/example-org/pilot/pull/7"}
        if path.endswith("/repos/example-org/pilot"):
            return {"default_branch": "main"}
        return {}

    client = AppGitHubClient(
        GitHubSettings(api_url="https://api.example", app_id="1", installation_id="2"),
        token_provider=lambda: "token",
    )
    client._request = fake_request  # type: ignore[method-assign]

    metadata = client.get_metadata(REPO)
    assert metadata.default_branch == "main"
    assert metadata.head_sha == "a" * 40

    sha = client.commit_files(REPO, "main", {"variables.tf": "x = 1\n"}, "message")
    assert sha == "commit"

    url = client.create_draft_pr(
        REPO, head="drift-fix/1", base="main", title="title", body="body"
    )
    assert url == "https://github.com/example-org/pilot/pull/7"

    pull = next(payload for _, path, payload in payloads if path.endswith("/pulls"))
    assert pull["draft"] is True
    assert pull["head"] == "drift-fix/1"
    blob = next(payload for _, path, payload in payloads if path.endswith("/git/blobs"))
    assert blob["encoding"] == "base64"
