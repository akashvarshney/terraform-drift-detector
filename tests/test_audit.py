import json
from pathlib import Path

from drift_agent.audit.store import InMemoryAuditStore, LocalJsonAuditStore, NullAuditStore


def test_local_store_writes_dated_json(tmp_path: Path):
    store = LocalJsonAuditStore(tmp_path)
    store.write("scan-1", {"scan_id": "scan-1"})
    files = list(tmp_path.rglob("scan-1.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["scan_id"] == "scan-1"


def test_unsafe_keys_cannot_escape_the_directory(tmp_path: Path):
    store = LocalJsonAuditStore(tmp_path / "audit")
    store.write("../../evil", {"ok": True})
    written = list((tmp_path / "audit").rglob("*.json"))
    assert len(written) == 1
    assert written[0].resolve().is_relative_to((tmp_path / "audit").resolve())


def test_memory_and_null_stores():
    memory = InMemoryAuditStore()
    memory.write("one", {"a": 1})
    memory.write("two", {"b": 2})
    assert memory.keys() == ["one", "two"]
    assert NullAuditStore().write("ignored", {}) is None
