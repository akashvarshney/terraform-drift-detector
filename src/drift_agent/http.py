"""Minimal JSON-over-HTTPS helper shared by the service adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    def __init__(self, status: int | None, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: int = 30,
    accepted: tuple[int, ...] = (200, 201, 202, 204),
) -> dict[str, Any]:
    """Perform one JSON request. Never surfaces the response body in errors."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise HttpError(error.code, "http_error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HttpError(None, "connection_failed") from None
    if status not in accepted:
        raise HttpError(status, "unexpected_status")
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise HttpError(status, "invalid_json_response") from None
    return parsed if isinstance(parsed, dict) else {"result": parsed}
