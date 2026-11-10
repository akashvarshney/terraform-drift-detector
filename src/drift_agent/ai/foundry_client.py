"""Existing Azure AI Foundry model adapter.

The model explains and proposes; it never decides. Only reduced drift facts,
repository guidance and the skill text are prompted — no plan values.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from drift_agent.http import HttpError, request_json
from drift_agent.models import AnalysisSource, ModelAnalysis, RiskLevel
from drift_agent.redaction import redact_text
from drift_agent.settings import FoundrySettings


class FoundryUnavailable(RuntimeError):
    """The integration is not configured or its SDK/credential is missing."""


class ModelError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


SYSTEM_PROMPT = """You are a Terraform drift remediation assistant. You are NOT the
drift detector and you have no authority over policy.

Terraform's plan JSON is the only source of truth for drift. You receive reduced
facts: resource addresses, actions and changed attribute names. You never receive
attribute values, output values or secrets, and you must not ask for them.

Hard rules:
- Never propose terraform apply, plan -target, state surgery or provider/backend edits.
- If the repository uses a module for a resource type, never propose a standalone
  azurerm resource for that type.
- Never autonomously remediate deletion, replacement, RBAC, managed identity,
  networking, firewall, private endpoint, public access or encryption changes.
- The application may only ever raise your recommended risk level, never lower it.
- If evidence is insufficient, set a low confidence and give manual_review_reason.

Reply with JSON only:
{"summary": str, "probable_cause": str, "risk_recommendation": "low|medium|high|blocked",
 "proposed_change": str, "confidence": number, "manual_review_reason": str|null}
"""


class FoundryClient(Protocol):
    name: str

    def analyze(
        self,
        *,
        drift_facts: dict[str, Any],
        skill: str,
        repository_context: str,
    ) -> ModelAnalysis: ...


def build_system_prompt(skill: str) -> str:
    return f"{SYSTEM_PROMPT}\n\n--- Repository skill ---\n{skill.strip()}\n"


def build_user_message(drift_facts: dict[str, Any], repository_context: str) -> str:
    """Compose the user turn from already-reduced facts."""
    return json.dumps(
        {
            "drift_facts": drift_facts,
            "repository_context": redact_text(repository_context),
            "instructions": (
                "Return the JSON object only. proposed_change must be a unified diff "
                "against the current checkout, or an empty string when no safe change exists."
            ),
        },
        indent=2,
        sort_keys=True,
    )


def parse_analysis(
    content: str,
    *,
    deployment: str | None = None,
    usage: dict[str, Any] | None = None,
) -> ModelAnalysis:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        raise ModelError("invalid_json_response") from None
    if not isinstance(document, dict):
        raise ModelError("invalid_response_shape")
    try:
        analysis = ModelAnalysis.model_validate(document)
    except ValueError:
        raise ModelError("invalid_response_schema") from None
    analysis.source = AnalysisSource.MODEL
    analysis.deployment = deployment
    if usage:
        analysis.prompt_tokens = usage.get("prompt_tokens")
        analysis.completion_tokens = usage.get("completion_tokens")
    return analysis


class HttpFoundryClient:
    """Calls an existing Foundry deployment over the OpenAI-compatible API."""

    name = "foundry"

    def __init__(self, settings: FoundrySettings, *, bearer_provider=None) -> None:
        self.settings = settings
        self._bearer_provider = bearer_provider or _default_bearer

    def _headers(self) -> dict[str, str]:
        if self.settings.api_key:
            return {"api-key": self.settings.api_key}
        token = self._bearer_provider()
        if not token:
            raise FoundryUnavailable("foundry_credential_unavailable")
        return {"Authorization": f"Bearer {token}"}

    def analyze(
        self,
        *,
        drift_facts: dict[str, Any],
        skill: str,
        repository_context: str,
    ) -> ModelAnalysis:
        if not self.settings.configured:
            raise FoundryUnavailable("foundry_not_configured")
        payload = {
            "messages": [
                {"role": "system", "content": build_system_prompt(skill)},
                {"role": "user", "content": build_user_message(drift_facts, repository_context)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            response = request_json(
                "POST",
                self.settings.chat_url,
                headers=self._headers(),
                payload=payload,
                timeout=self.settings.timeout,
            )
        except HttpError as error:
            raise ModelError("foundry_request_failed", error.status) from None
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("invalid_response_shape") from None
        if not isinstance(content, str):
            raise ModelError("invalid_response_shape")
        return parse_analysis(
            content, deployment=self.settings.deployment, usage=response.get("usage") or {}
        )


def _default_bearer() -> str | None:
    """Managed identity / workload identity token when azure-identity exists."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        return None
    try:
        credential = DefaultAzureCredential()
        return credential.get_token("https://ai.azure.com/.default").token
    except Exception:  # noqa: BLE001 - credential chain failure must not leak details
        return None


class FakeFoundryClient:
    """Deterministic stand-in for tests and dry runs."""

    name = "fake-foundry"

    def __init__(self, analysis: ModelAnalysis | dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        if isinstance(analysis, ModelAnalysis):
            self._analysis = analysis
        elif isinstance(analysis, dict):
            self._analysis = ModelAnalysis.model_validate(analysis)
        else:
            self._analysis = None

    def analyze(
        self,
        *,
        drift_facts: dict[str, Any],
        skill: str,
        repository_context: str,
    ) -> ModelAnalysis:
        self.calls.append(
            {"drift_facts": drift_facts, "skill": skill, "repository_context": repository_context}
        )
        if self._analysis is not None:
            return self._analysis.model_copy(deep=True)
        return ModelAnalysis(
            summary="Drift reported for the affected resources.",
            probable_cause="unknown",
            risk_recommendation=RiskLevel.MEDIUM,
            proposed_change="",
            confidence=0.85,
            manual_review_reason=None,
            source=AnalysisSource.MODEL,
            deployment="fake",
        )


class DisabledFoundryClient:
    """Explicitly unavailable model access; the scan still completes."""

    name = "disabled"

    def analyze(self, **_: Any) -> ModelAnalysis:
        raise FoundryUnavailable("foundry_not_configured")
