"""Audit storage behind a small, blob-compatible abstraction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from drift_agent.models import DriftRecord


class AuditStore(Protocol):
    def write(self, key: str, payload: dict[str, Any]) -> None: ...


class LocalJsonAuditStore:
    """Writes one JSON document per record; usable with Blob/File in production."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write(self, key: str, payload: dict[str, Any]) -> None:
        safe_key = "".join(character for character in key if character.isalnum() or character in "-_.")
        if not safe_key:
            raise ValueError("audit key must contain at least one safe character")
        stamp = datetime.now(UTC).strftime("%Y/%m/%d")
        target = self.directory / stamp
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{safe_key}.json").write_text(
            json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def record(self, record: DriftRecord) -> None:
        self.write(f"{record.scan_id}-{record.repository}", record.model_dump(mode="json"))


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def write(self, key: str, payload: dict[str, Any]) -> None:
        self.entries.append((key, payload))

    def record(self, record: DriftRecord) -> None:
        self.write(record.scan_id, record.model_dump(mode="json"))

    def keys(self) -> list[str]:
        return [key for key, _ in self.entries]


class NullAuditStore:
    def write(self, key: str, payload: dict[str, Any]) -> None:
        return None
