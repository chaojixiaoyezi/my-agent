"""固定source snapshot flush不重搜、不提前clear，默认公开flush不变。"""

import copy

import pytest

from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.agent.memory_store import jsonl


def memory(tmp_path):
    result = jsonl.JsonlMemory(tmp_path / "owner" / "memory.jsonl")
    record = result.add("fact", "temporary fact")
    result._pending_access = {record.entry_id: 1.0, "inactive": 2.0}
    return result, record


def test_snapshot_flush_missing_actual_scope_keeps_all_pending_and_original_file(tmp_path):
    mem, _record = memory(tmp_path)
    before, pending = mem.path.read_bytes(), copy.copy(mem._pending_access)
    with pytest.raises(GlobalWriteCommitUnavailable):
        mem.flush_access_events_from_source()
    assert mem.path.read_bytes() == before and mem._pending_access == pending


def test_empty_snapshot_zero_writes_and_no_source_needed(tmp_path):
    mem = jsonl.JsonlMemory(tmp_path / "memory.jsonl")
    assert mem.flush_access_events_from_source() == 0 and not mem.path.exists()


def test_flush_once_deletes_only_equal_snapshot_including_inactive(tmp_path, monkeypatch):
    mem, record = memory(tmp_path)
    original, calls = jsonl.write_private_text_file_atomic_unlocked, []

    def write(path, text, **options):
        calls.append((path, options))
        assert mem._pending_access == {record.entry_id: 1.0, "inactive": 2.0}
        mem._pending_access[record.entry_id] = 3.0
        return original(path, text, **options)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", write)
    with global_write_commit_scope(lambda operation: operation()):
        assert mem.flush_access_events_from_source() == 1
    assert calls == [(mem.path, {"replace_once": True})]
    assert mem._pending_access == {record.entry_id: 3.0}
    assert mem.all()[0].last_accessed_at >= 1.0
    assert mem.all()[0].version == record.version and mem.all()[0].content == record.content


@pytest.mark.parametrize("stage", ["lock_birth", "writer"])
def test_failure_preserves_snapshot_without_second_writer_or_compensation(tmp_path, stage):
    mem, _record = memory(tmp_path)
    before, pending, calls = mem.path.read_bytes(), dict(mem._pending_access), []
    error = OSError("synthetic write failure")

    def commit(operation):
        calls.append(True)
        if len(calls) == (1 if stage == "lock_birth" else 2):
            raise error
        return operation()

    with global_write_commit_scope(commit):
        with pytest.raises(OSError) as caught:
            mem.flush_access_events_from_source()
    assert caught.value is error and mem.path.read_bytes() == before
    assert mem._pending_access == pending and len(calls) == (1 if stage == "lock_birth" else 2)


def test_success_followed_by_source_close_cleans_only_snapshot_in_memory(tmp_path):
    mem, _record = memory(tmp_path)
    writes = []

    def commit(operation):
        result = operation()
        writes.append(True)
        return result

    with global_write_commit_scope(commit):
        assert mem.flush_access_events_from_source() == 1
    assert len(writes) == 2 and not mem._pending_access
    with pytest.raises(GlobalWriteCommitUnavailable):
        # 新信号不能借已关闭scope，已成功文件不做补偿。
        mem._pending_access["inactive"] = 5.0
        mem.flush_access_events_from_source()
    assert mem._pending_access == {"inactive": 5.0}


@pytest.mark.parametrize("broken", ["missing", "json"])
def test_flush_strict_authority_failure_preserves_pending(tmp_path, broken):
    mem, _record = memory(tmp_path)
    if broken == "missing":
        mem.path.unlink()
    else:
        mem.path.write_text("{invalid-json\n")
    before = mem.path.read_bytes() if mem.path.exists() else None
    pending = dict(mem._pending_access)
    with global_write_commit_scope(lambda operation: operation()):
        with pytest.raises(ValueError, match="MEMORY_AUTHORITY_UNREADABLE"):
            mem.flush_access_events_from_source()
    assert (mem.path.read_bytes() if mem.path.exists() else None) == before
    assert mem._pending_access == pending
