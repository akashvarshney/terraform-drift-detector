"""Fleet rotation: which repositories are due next and what the fleet looks like.

Scanning a whole fleet in one sweep is a throughput problem. Scanning a small
slice a day and still covering everything is a bookkeeping problem. This module
keeps that bookkeeping: a small JSON ledger of the last scan per repository plus
its drift streak, a fair least-recently-scanned selection, and a report that
answers "what is drifting, since when, and what is next?".

The ledger is deliberately separate from the audit records: audit JSON is the
immutable per-scan evidence, while this is derived, small and cheap to rewrite
after every repository so an interrupted run still advances the rotation.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from drift_agent.models import DriftRecord, RepositoryConfig, RiskLevel, ScanStatus

LEDGER_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FleetEntry(BaseModel):
    """Everything the rotation needs to know about one repository."""

    model_config = ConfigDict(extra="ignore")

    repository: str
    last_scan_id: str | None = None
    last_scanned_at: str | None = None
    last_status: ScanStatus | None = None
    last_risk: RiskLevel | None = None
    last_pr_url: str | None = None
    scans: int = 0
    consecutive_drift: int = 0
    drifting_since: str | None = None


class FleetState(BaseModel):
    """The rotation ledger; one document, rewritten as each repository finishes."""

    model_config = ConfigDict(extra="ignore")

    version: int = LEDGER_VERSION
    updated_at: str = Field(default_factory=lambda: _now().isoformat())
    entries: dict[str, FleetEntry] = Field(default_factory=dict)

    def get(self, repository_id: str) -> FleetEntry | None:
        return self.entries.get(repository_id)

    def record(self, record: DriftRecord, now: datetime | None = None) -> FleetEntry:
        """Fold one finished scan into the ledger.

        A clean scan closes the drift streak. A drift scan opens or extends it.
        An errored scan proves nothing about drift, so the streak is left alone:
        a broken checkout must not silently mark a known drift as resolved.
        """
        stamp = now or _now()
        entry = self.entries.get(record.repository) or FleetEntry(repository=record.repository)
        entry.last_scan_id = record.scan_id
        entry.last_scanned_at = stamp.isoformat()
        entry.last_status = record.status
        entry.scans += 1
        if record.status is ScanStatus.DRIFT:
            entry.consecutive_drift += 1
            entry.last_risk = record.risk.level if record.risk else None
            entry.drifting_since = entry.drifting_since or stamp.isoformat()
        elif record.status is ScanStatus.CLEAN:
            entry.consecutive_drift = 0
            entry.drifting_since = None
            entry.last_risk = None
        if record.remediation.pr_url:
            entry.last_pr_url = record.remediation.pr_url
        self.entries[record.repository] = entry
        self.updated_at = stamp.isoformat()
        return entry

    @classmethod
    def load(cls, path: Path) -> FleetState:
        """A missing or unreadable ledger starts empty; bookkeeping never blocks a scan."""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return cls()
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(document, dict):
            return cls()
        try:
            return cls.model_validate(document)
        except ValidationError:
            return cls()

    def save(self, path: Path) -> None:
        """Write atomically so a killed job cannot leave a truncated ledger."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


def due_repositories(
    state: FleetState,
    repositories: Sequence[RepositoryConfig],
    *,
    cooldown_hours: float = 20.0,
    force: bool = False,
    now: datetime | None = None,
) -> list[RepositoryConfig]:
    """Rotation order: never scanned first, then least recently scanned.

    ``cooldown_hours`` keeps a second run on the same day from re-scanning the
    repositories the first run just covered, which is what makes "5 today, 5
    tomorrow" work without touching the registry between runs.
    """
    moment = now or _now()
    cooldown = timedelta(hours=max(0.0, cooldown_hours))
    decorated: list[tuple[bool, datetime, str, RepositoryConfig]] = []
    for repo in repositories:
        entry = state.entries.get(repo.id)
        seen = _parse_timestamp(entry.last_scanned_at) if entry else None
        if seen is not None and not force and moment - seen < cooldown:
            continue
        decorated.append(
            (seen is not None, seen or datetime.min.replace(tzinfo=UTC), repo.id, repo)
        )
    decorated.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in decorated]


def select_due(
    state: FleetState,
    repositories: Sequence[RepositoryConfig],
    *,
    limit: int,
    cooldown_hours: float = 20.0,
    force: bool = False,
    now: datetime | None = None,
) -> list[RepositoryConfig]:
    """The next slice of the rotation; ``limit <= 0`` means everything that is due."""
    due = due_repositories(
        state, repositories, cooldown_hours=cooldown_hours, force=force, now=now
    )
    return due if limit <= 0 else due[:limit]


def _group(
    rows: list[tuple[RepositoryConfig, FleetEntry | None, datetime | None, float | None]],
    key: Callable[[RepositoryConfig], str | None],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for repo, entry, seen, _age in rows:
        name = key(repo) or "unassigned"
        group = groups.setdefault(
            name, {"key": name, "repositories": 0, "drifting": 0, "never_scanned": 0}
        )
        group["repositories"] += 1
        if entry is not None and entry.last_status is ScanStatus.DRIFT:
            group["drifting"] += 1
        if seen is None:
            group["never_scanned"] += 1
    return sorted(groups.values(), key=lambda item: (-item["drifting"], item["key"]))


def build_report(
    state: FleetState,
    registry: Any,
    *,
    batch_size: int = 10,
    stale_days: float = 7.0,
    next_count: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consolidated fleet view: coverage, drift with age, and the next batch."""
    moment = now or _now()
    batch = max(1, batch_size)
    rows: list[tuple[RepositoryConfig, FleetEntry | None, datetime | None, float | None]] = []
    never: list[str] = []
    stale: list[dict[str, Any]] = []
    for repo in registry.enabled():
        entry = state.entries.get(repo.id)
        seen = _parse_timestamp(entry.last_scanned_at) if entry else None
        age = None if seen is None else round((moment - seen).total_seconds() / 86400, 2)
        rows.append((repo, entry, seen, age))
        if seen is None:
            never.append(repo.id)
        elif age is not None and age >= stale_days:
            stale.append(
                {
                    "repository": repo.id,
                    "last_scanned_at": entry.last_scanned_at if entry else None,
                    "days": age,
                }
            )

    drifting: list[dict[str, Any]] = []
    errored: list[dict[str, Any]] = []
    for repo, entry, _seen, _age in rows:
        if entry is None:
            continue
        if entry.last_status is ScanStatus.DRIFT:
            since = _parse_timestamp(entry.drifting_since)
            drifting.append(
                {
                    "repository": repo.id,
                    "subscription_id": repo.azure_subscription_id,
                    "environment": repo.environment,
                    "owner_email": repo.owner_email,
                    "risk": entry.last_risk.value if entry.last_risk else None,
                    "consecutive_drift": entry.consecutive_drift,
                    "drifting_since": entry.drifting_since,
                    "days_drifting": (
                        None if since is None else round((moment - since).total_seconds() / 86400, 2)
                    ),
                    "last_scanned_at": entry.last_scanned_at,
                    "pr_url": entry.last_pr_url,
                }
            )
        elif entry.last_status is ScanStatus.ERROR:
            errored.append(
                {"repository": repo.id, "last_scanned_at": entry.last_scanned_at}
            )
    drifting.sort(key=lambda item: (-(item["days_drifting"] or 0), item["repository"]))
    stale.sort(key=lambda item: (-item["days"], item["repository"]))

    outstanding = len(never) + len(stale)
    rotation = due_repositories(state, registry.enabled(), cooldown_hours=0.0, now=moment)
    next_batch = [repo.id for repo in rotation][: next_count if next_count else batch]
    scanned_recently = sum(1 for row in rows if row[3] is not None and row[3] < stale_days)

    return {
        "generated_at": moment.isoformat(),
        "coverage": {
            "repositories": len(rows),
            "scanned_ever": len(rows) - len(never),
            "never_scanned": len(never),
            "scanned_recently": scanned_recently,
            "stale": len(stale),
            "stale_days": stale_days,
            "drifting": len(drifting),
            "errored": len(errored),
        },
        "rotation": {
            "batch_size": batch,
            "outstanding": outstanding,
            "days_to_cover": math.ceil(outstanding / batch) if outstanding else 0,
            "next_batch": next_batch,
        },
        "by_subscription": _group(rows, lambda repo: repo.azure_subscription_id),
        "by_owner": _group(rows, lambda repo: repo.owner_email),
        "drift": drifting,
        "never_scanned": never,
        "stale": stale,
        "errored": errored,
    }


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = ["—" if value is None or value == "" else str(value) for value in row]
        output.append("| " + " | ".join(cells) + " |")
    return output


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable fleet report; safe to attach to a ticket or commit to a branch."""
    coverage = report["coverage"]
    rotation = report["rotation"]
    lines = [
        "# Terraform drift — fleet report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Coverage",
        "",
    ]
    lines += _table(
        ["Metric", "Value"],
        [
            ["Repositories (enabled)", coverage["repositories"]],
            ["Scanned at least once", coverage["scanned_ever"]],
            ["Never scanned", coverage["never_scanned"]],
            [f"Scanned in the last {coverage['stale_days']:g} days", coverage["scanned_recently"]],
            ["Stale", coverage["stale"]],
            ["Currently drifting", coverage["drifting"]],
            ["Last scan errored", coverage["errored"]],
        ],
    )
    lines += [
        "",
        "## Rotation",
        "",
        (
            f"Batch size {rotation['batch_size']} per run, {rotation['outstanding']} repositories "
            f"outstanding, about {rotation['days_to_cover']} run(s) to full coverage."
        ),
        "",
    ]
    if rotation["next_batch"]:
        lines += ["Next batch:", ""] + [f"- {name}" for name in rotation["next_batch"]] + [""]
    if report["drift"]:
        lines += ["## Drift", ""]
        lines += _table(
            ["Repository", "Subscription", "Env", "Risk", "Days", "Last scan", "PR"],
            [
                [
                    item["repository"],
                    item["subscription_id"],
                    item["environment"],
                    item["risk"],
                    item["days_drifting"],
                    item["last_scanned_at"],
                    item["pr_url"],
                ]
                for item in report["drift"]
            ],
        )
        lines.append("")
    if report["by_subscription"]:
        lines += ["## By subscription", ""]
        lines += _table(
            ["Subscription", "Repositories", "Drifting", "Never scanned"],
            [
                [group["key"], group["repositories"], group["drifting"], group["never_scanned"]]
                for group in report["by_subscription"]
            ],
        )
        lines.append("")
    if report["stale"]:
        lines += ["## Stale", ""]
        lines += _table(
            ["Repository", "Last scan", "Days"],
            [
                [item["repository"], item["last_scanned_at"], item["days"]]
                for item in report["stale"]
            ],
        )
        lines.append("")
    if report["never_scanned"]:
        lines += ["## Never scanned", ""]
        lines += [f"- {name}" for name in report["never_scanned"]] + [""]
    if report["errored"]:
        lines += ["## Last scan errored", ""]
        lines += [
            f"- {item['repository']} ({item['last_scanned_at']})" for item in report["errored"]
        ] + [""]
    return "\n".join(lines).rstrip() + "\n"
