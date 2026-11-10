"""Optional Microsoft Graph mail adapter."""

from __future__ import annotations

import json
from typing import Any

from drift_agent.http import HttpError, request_json
from drift_agent.settings import EmailSettings


class EmailError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _graph_token() -> str | None:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        return None
    try:
        return DefaultAzureCredential().get_token("https://graph.microsoft.com/.default").token
    except Exception:  # noqa: BLE001 - missing mail permission degrades to a warning
        return None


class GraphEmailNotifier:
    name = "email"

    def __init__(self, settings: EmailSettings, *, token_provider=None) -> None:
        self.settings = settings
        self._token_provider = token_provider or _graph_token

    def send(self, subject: str, payload: dict[str, Any], *, recipient: str | None = None) -> None:
        if not self.settings.sender or not recipient:
            raise EmailError("email_not_configured")
        token = self._token_provider()
        if not token:
            raise EmailError("email_credential_unavailable")
        message = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "Text",
                    "content": json.dumps(payload, indent=2, sort_keys=True),
                },
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": False,
        }
        try:
            request_json(
                "POST",
                f"{self.settings.graph_url}/users/{self.settings.sender}/sendMail",
                headers={"Authorization": f"Bearer {token}"},
                payload=message,
                timeout=self.settings.timeout,
            )
        except HttpError as error:
            raise EmailError("email_delivery_failed", error.status) from None
