"""Microsoft Teams webhook adapter."""

from __future__ import annotations

import json
from typing import Any

from drift_agent.http import HttpError, request_json
from drift_agent.settings import TeamsSettings


class TeamsError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class TeamsNotifier:
    name = "teams"

    def __init__(self, settings: TeamsSettings) -> None:
        self.settings = settings

    def send(self, subject: str, payload: dict[str, Any], *, recipient: str | None = None) -> None:
        if not self.settings.webhook_url:
            raise TeamsError("teams_not_configured")
        message = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": subject,
            "title": subject,
            "text": "```\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```",
            "sections": [
                {
                    "facts": [
                        {"name": "Repository", "value": str(payload.get("repository"))},
                        {"name": "Status", "value": str(payload.get("status"))},
                        {"name": "Risk", "value": _risk(payload)},
                        {"name": "Remediation", "value": _remediation(payload)},
                        {"name": "Dry run", "value": str(payload.get("dry_run"))},
                    ]
                }
            ],
        }
        try:
            request_json(
                "POST",
                self.settings.webhook_url,
                payload=message,
                timeout=self.settings.timeout,
            )
        except HttpError as error:
            raise TeamsError("teams_delivery_failed", error.status) from None


def _risk(payload: dict[str, Any]) -> str:
    risk = payload.get("risk")
    return str(risk.get("level")) if isinstance(risk, dict) else "unknown"


def _remediation(payload: dict[str, Any]) -> str:
    remediation = payload.get("remediation")
    if not isinstance(remediation, dict):
        return "unknown"
    url = remediation.get("pr_url")
    return f"{remediation.get('status')} {url}" if url else str(remediation.get("status"))
