from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer, OwnerQuotaExceeded


def test_memory_stable_id_replace_remove_and_versions(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    added = memory.add("user", "项目使用 UTC", kind="project", tags=["time"])

    assert added.entry_id.startswith("memory-")
    assert added.version == 1
    replaced = memory.replace(
        added.entry_id,
        "项目使用 UTC，并在边界显示本地时区",
        expected_version=1,
    )
    assert replaced.entry_id == added.entry_id
    assert replaced.version == 2
    assert [record.content for record in memory.all()] == [replaced.content]

    removed = memory.remove(added.entry_id, expected_version=2)
    assert removed.action == "remove"
    assert removed.version == 3
    assert memory.all() == []


def test_memory_batch_is_all_or_nothing(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    memory = JsonlMemory(path)
    first = memory.add("user", "第一条", kind="fact")
    before = path.read_bytes()

    with pytest.raises(KeyError):
        memory.apply_batch(
            [
                {"action": "replace", "entry_id": first.entry_id, "content": "已修改"},
                {"action": "remove", "entry_id": "memory-missing"},
            ]
        )

    assert path.read_bytes() == before
    assert [record.content for record in memory.all()] == ["第一条"]


def test_memory_batch_commits_one_versioned_ledger(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    memory = JsonlMemory(path)
    first = memory.add("user", "旧事实", kind="fact")
    committed = memory.apply_batch(
        [
            {"action": "replace", "entry_id": first.entry_id, "content": "新事实"},
            {"action": "add", "content": "第二条", "kind": "note"},
        ]
    )

    assert [record.action for record in committed] == ["replace", "add"]
    assert {record.content for record in memory.all()} == {"新事实", "第二条"}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert all(row.get("entry_id") for row in rows)


def test_memory_concurrent_writers_do_not_lose_rows(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"

    def write(index: int) -> None:
        JsonlMemory(path).add("user", f"并发记忆 {index}", kind="fact")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    records = JsonlMemory(path).all()
    assert len(records) == 40
    assert len({record.entry_id for record in records}) == 40


def test_memory_expiry_is_structured_and_not_recalled(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "已过期", kind="event", expires_at=time.time() - 1)
    active = memory.add("user", "仍有效", kind="event", expires_at=time.time() + 3600)

    assert [record.entry_id for record in memory.all()] == [active.entry_id]
    assert memory.search("过期") == []


def test_memory_runtime_snapshot_reports_health_without_content_or_path(tmp_path: Path) -> None:
    path = tmp_path / "private-owner" / "memory.jsonl"
    memory = JsonlMemory(path)
    memory.add("user", "private memory content", kind="fact")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken-json\n")

    snapshot = memory.runtime_snapshot()

    assert snapshot["state"] == "available"
    assert snapshot["health"] == "degraded"
    assert snapshot["active_total"] == 1
    assert snapshot["active_by_kind"] == {"fact": 1}
    assert snapshot["load_error_count"] == 1
    assert "private memory content" not in str(snapshot)
    assert "private-owner" not in str(snapshot)


def test_memory_authority_and_daily_mirror_share_owner_quota_admission(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    path = owner / "memory" / "long_term" / "memory.jsonl"
    memory = JsonlMemory(
        path,
        daily_mirror_dir=owner / "memory" / "daily",
        quota_enforcer=OwnerQuotaEnforcer(owner, max_bytes=1),
    )

    with pytest.raises(OwnerQuotaExceeded):
        memory.add("user", "this mutation cannot fit", kind="fact")

    assert not path.exists()
    assert list((owner / "memory" / "daily").glob("*.jsonl")) == []


def test_remember_tool_crud_uses_stable_ids(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    tool = RememberTool(SimpleNamespace(memory=memory, _current_run_params=None))
    added = tool.execute({"content": "项目代号青竹", "kind": "project"})
    entry = json.loads(added.output)["entries"][0]

    listed = tool.execute({"action": "list"})
    assert json.loads(listed.output)["entries"][0]["entry_id"] == entry["entry_id"]
    replaced = tool.execute(
        {
            "action": "replace",
            "entry_id": entry["entry_id"],
            "expected_version": entry["version"],
            "content": "项目代号青竹，账单输出 JSON",
            "kind": "project",
        }
    )
    replacement = json.loads(replaced.output)["entries"][0]
    assert replacement["version"] == 2

    removed = tool.execute(
        {
            "action": "remove",
            "entry_id": entry["entry_id"],
            "expected_version": replacement["version"],
        }
    )
    assert removed.ok
    assert memory.all() == []


def test_separate_owner_and_group_memory_paths_are_blacklisted_by_construction(tmp_path: Path) -> None:
    owner_a = JsonlMemory(tmp_path / "owners" / "a" / "memory" / "memory.jsonl")
    owner_b = JsonlMemory(tmp_path / "owners" / "b" / "memory" / "memory.jsonl")
    group = JsonlMemory(tmp_path / "groups" / "g" / "memory" / "memory.jsonl")
    owner_a.add("user", "A 私有事实", kind="fact")
    owner_b.add("user", "B 私有事实", kind="fact")
    group.add("user", "群组事实", kind="fact")

    assert [record.content for record in owner_a.all()] == ["A 私有事实"]
    assert [record.content for record in owner_b.all()] == ["B 私有事实"]
    assert [record.content for record in group.all()] == ["群组事实"]
