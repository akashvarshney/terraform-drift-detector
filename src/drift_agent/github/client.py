"""GitHub App client.

Design for GitHub App authentication (never a personal access token) while the
whole surface stays mockable: every external mutation goes through these four
methods, so dry runs and tests can use FakeGitHubClient instead.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from drift_agent.http import HttpError, request_json
from drift_agent.models import RepositoryConfig
from drift_agent.settings import GitHubSettings

API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class GitHubUnavailable(RuntimeError):
    """GitHub App is not configured or its private key/crypto is unavailable."""


@dataclass(frozen=True)
class RepoMetadata:
    slug: str
    default_branch: str
    head_sha: str | None = None


@dataclass
class Call:
    operation: str
    payload: dict[str, Any] = field(default_factory=dict)


class GitHubClient(Protocol):
    name: str

    def get_metadata(self, repo: RepositoryConfig) -> RepoMetadata: ...

    def create_branch(self, repo: RepositoryConfig, branch: str, sha: str) -> str: ...

    def commit_files(
        self,
        repo: RepositoryConfig,
        branch: str,
        files: dict[str, str],
        message: str,
    ) -> str: ...

    def create_draft_pr(
        self,
        repo: RepositoryConfig,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str: ...


class FakeGitHubClient:
    """Records intended calls; makes no network request."""

    name = "fake-github"

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def get_metadata(self, repo: RepositoryConfig) -> RepoMetadata:
        self.calls.append(Call("get_metadata", {"repository": repo.slug}))
        return RepoMetadata(slug=repo.slug, default_branch=repo.default_branch, head_sha="0" * 40)

    def create_branch(self, repo: RepositoryConfig, branch: str, sha: str) -> str:
        self.calls.append(Call("create_branch", {"branch": branch, "sha": sha}))
        return branch

    def commit_files(
        self,
        repo: RepositoryConfig,
        branch: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        self.calls.append(
            Call("commit_files", {"branch": branch, "files": sorted(files), "message": message})
        )
        return "1" * 40

    def create_draft_pr(
        self,
        repo: RepositoryConfig,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        self.calls.append(
            Call("create_draft_pr", {"head": head, "base": base, "title": title, "body": body})
        )
        return f"https://github.com/{repo.slug}/pull/1"


class AppGitHubClient:
    """GitHub App installation-token client over the REST API."""

    name = "github-app"

    def __init__(self, settings: GitHubSettings, *, token_provider=None) -> None:
        self.settings = settings
        self._token_provider = token_provider
        self._cached_token: tuple[str, float] | None = None

    # --- authentication ---------------------------------------------------
    def _jwt(self) -> str:
        if not self.settings.app_id:
            raise GitHubUnavailable("github_app_not_configured")
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            raise GitHubUnavailable("cryptography_not_installed") from None
        key_path = self.settings.private_key_path
        if not key_path:
            raise GitHubUnavailable("github_private_key_not_configured")
        try:
            with open(key_path, "rb") as handle:
                private_key = serialization.load_pem_private_key(handle.read(), password=None)
        except (OSError, ValueError):
            raise GitHubUnavailable("github_private_key_unreadable") from None
        now = int(time.time())
        header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = _b64url(
            _json_bytes({"iat": now - 60, "exp": now + 540, "iss": self.settings.app_id})
        )
        signing_input = f"{header}.{payload}".encode()
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64url(signature)}"

    def _installation_token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        if self._cached_token and self._cached_token[1] > time.time() + 60:
            return self._cached_token[0]
        if not self.settings.installation_id:
            raise GitHubUnavailable("github_installation_not_configured")
        try:
            response = request_json(
                "POST",
                f"{self.settings.api_url}/app/installations/{self.settings.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                payload={},
                timeout=self.settings.timeout,
            )
        except HttpError as error:
            raise GitHubError("installation_token_failed", error.status) from None
        token = response.get("token")
        if not token:
            raise GitHubError("installation_token_missing")
        expires = _parse_timestamp(response.get("expires_at"))
        self._cached_token = (token, expires)
        return token

    def _request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        try:
            return request_json(
                method,
                f"{self.settings.api_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._installation_token()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                payload=payload,
                timeout=self.settings.timeout,
            )
        except HttpError as error:
            raise GitHubError("github_request_failed", error.status) from None

    # --- operations -------------------------------------------------------
    def get_metadata(self, repo: RepositoryConfig) -> RepoMetadata:
        slug = repo.slug
        details = self._request("GET", f"/repos/{slug}")
        branch = str(details.get("default_branch") or repo.default_branch)
        head_sha = self._request("GET", f"/repos/{slug}/git/ref/heads/{branch}").get("object", {}).get("sha")
        return RepoMetadata(slug=slug, default_branch=branch, head_sha=head_sha)

    def create_branch(self, repo: RepositoryConfig, branch: str, sha: str) -> str:
        response = self._request(
            "POST", f"/repos/{repo.slug}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
        )
        return str(response.get("ref") or branch)

    def commit_files(
        self,
        repo: RepositoryConfig,
        branch: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        slug = repo.slug
        head = self._request("GET", f"/repos/{slug}/git/ref/heads/{branch}")
        head_sha = (head.get("object") or {}).get("sha")
        if not head_sha:
            raise GitHubError("branch_head_missing")
        commit = self._request("GET", f"/repos/{slug}/git/commits/{head_sha}")
        base_tree = (commit.get("tree") or {}).get("sha")
        tree_entries = []
        for path, content in sorted(files.items()):
            blob = self._request(
                "POST",
                f"/repos/{slug}/git/blobs",
                {
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob.get("sha")}
            )
        tree = self._request(
            "POST",
            f"/repos/{slug}/git/trees",
            {"base_tree": base_tree, "tree": tree_entries},
        )
        created = self._request(
            "POST",
            f"/repos/{slug}/git/commits",
            {"message": message, "tree": tree.get("sha"), "parents": [head_sha]},
        )
        self._request(
            "PATCH",
            f"/repos/{slug}/git/refs/heads/{branch}",
            {"sha": created.get("sha"), "force": False},
        )
        return str(created.get("sha") or "")

    def create_draft_pr(
        self,
        repo: RepositoryConfig,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        response = self._request(
            "POST",
            f"/repos/{repo.slug}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        url = response.get("html_url")
        if not url:
            raise GitHubError("pull_request_url_missing")
        return str(url)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _json_bytes(value: dict[str, Any]) -> bytes:
    import json

    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return time.time() + 3000
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        ).timestamp()
    except ValueError:
        return time.time() + 3000


class DisabledGitHubClient:
    name = "disabled"

    def get_metadata(self, repo: RepositoryConfig) -> RepoMetadata:
        raise GitHubUnavailable("github_app_not_configured")

    def create_branch(self, *_: Any, **__: Any) -> str:
        raise GitHubUnavailable("github_app_not_configured")

    def commit_files(self, *_: Any, **__: Any) -> str:
        raise GitHubUnavailable("github_app_not_configured")

    def create_draft_pr(self, *_: Any, **__: Any) -> str:
        raise GitHubUnavailable("github_app_not_configured")
