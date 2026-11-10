"""Structured JSON logging.

Every event carries the correlation fields required by the design document
(scan_id, repository, commit, phase, outcome, duration, token usage) and passes
through redaction. Raw Terraform stdout/stderr and plan values are never logged.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from drift_agent.redaction import redact_text

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "drift_payload", None)
        if payload is None:
            payload = {"level": record.levelname, "message": redact_text(record.getMessage())}
        return json.dumps(payload, separators=(",", ":"), default=str, sort_keys=True)


class _DynamicStderrHandler(logging.Handler):
    """Write to the *current* sys.stderr.

    Binding the stream at import time breaks whenever the process stream is
    replaced (captured output, a wrapper, a restarted handler).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr
            if stream is None:
                return
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream must never fail a scan
            self.handleError(record)


def configure_logging(level: str = "INFO", stream: Any = None) -> None:
    global _CONFIGURED
    logger = logging.getLogger("drift_agent")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream) if stream is not None else _DynamicStderrHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    _CONFIGURED = True


class EventLogger:
    """Emits one JSON object per event on stderr, keeping stdout for reports."""

    def __init__(self, component: str = "drift_agent") -> None:
        if not _CONFIGURED:
            configure_logging()
        self._logger = logging.getLogger("drift_agent")
        self.component = component

    def event(self, event: str, /, level: int = logging.INFO, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": self.component,
            "event": event,
            "level": logging.getLevelName(level),
        }
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, str):
                payload[key] = redact_text(value)
            elif isinstance(value, (int, float, bool, list, dict)):
                payload[key] = value
            else:
                payload[key] = redact_text(str(value))
        self._logger.log(level, json.dumps(payload, default=str), extra={"drift_payload": payload})

    def warning(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.WARNING, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.ERROR, **fields)
