"""Redaction for anything that leaves the process.

Terraform plan JSON stays the source of truth for drift, but only reduced facts
(addresses, actions, attribute names) are ever forwarded. These helpers are a
second line of defence for log lines, prompts and notifications.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[redacted]"

# Deliberately excludes a bare "key" so key_vault / keystore names survive.
_SENSITIVE_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "connection_string",
    "client_secret",
    "shared_access",
    "access_key",
    "storage_key",
    "account_key",
    "sas",
    "certificate",
    "pfx",
    "api_key",
)

_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:password|passwd|secret|token|api[-_]?key|client[-_]?secret|connection[-_]?string)"
    r"\s*[:=]\s*)\S+"
)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_GITHUB_TOKEN = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})")
_AZURE_SAS = re.compile(r"(?i)([?&]sig=)[^&\s]+")


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def is_sensitive_name(name: str) -> bool:
    """True when an attribute name looks secret-bearing, so only its name is kept."""
    normalized = normalize_name(name)
    return any(token in normalized for token in _SENSITIVE_TOKENS)


def redact_text(text: str) -> str:
    """Scrub credential-shaped substrings from free text."""
    if not text:
        return text
    scrubbed = _PEM.sub(REDACTED, text)
    scrubbed = _BEARER.sub(lambda match: f"{match.group(1)}{REDACTED}", scrubbed)
    scrubbed = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{REDACTED}", scrubbed)
    scrubbed = _GITHUB_TOKEN.sub(REDACTED, scrubbed)
    scrubbed = _AZURE_SAS.sub(lambda match: f"{match.group(1)}{REDACTED}", scrubbed)
    return scrubbed


def redact_mapping(value: Any) -> Any:
    """Replace every scalar value with a placeholder, keeping only keys.

    Used before a plan fragment could ever be prompted, logged or notified.
    """
    if isinstance(value, Mapping):
        return {str(key): redact_mapping(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_mapping(child) for child in value]
    return REDACTED


def hash_payload(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def safe_json(payload: Any) -> str:
    """Serialize for logs with credential-shaped text scrubbed."""
    return redact_text(json.dumps(payload, separators=(",", ":"), default=str))

