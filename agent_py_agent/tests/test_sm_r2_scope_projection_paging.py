"""P1-05 回归测试：JsonlMemory.scope_pairs 多页有界续扫、降级收敛、失效重启与向后兼容。

全部基于 tmp_path 与隔离环境运行，不修改真机 ~/.my-agent。
"""

from __future__ import annotations

import copy
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.memory_store import _jsonl_indexing as indexing_module
from agent_py_agent.agent.memory_store import jsonl as jsonl_module
from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.jsonl import (
    _AUTHORITY_SCAN_MAX_BYTES,
    _AUTHORITY_SCAN_MAX_RECORDS,
    JsonlMemory,
    MemoryRecord,
)
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    long_term_record_matches_scope,
    runtime_long_term_scope,
)


def _seed_migrated_records(
    tmp_path: Path,
    count: int,
    *,
    scope_by_index,
    content_fn=None,
    expires_by_index=None,
) -> tuple[JsonlMemory, list[MemoryRecord]]:
    memory = JsonlMemory(tmp_path / "memory" / "long_term.jsonl")
    records: list[MemoryRecord] = []
    for index in range(count):
        scope_type, scope_key = scope_by_index(index)
        content = content_fn(index) if content_fn is not None else f"f{index}"
        expires_at = expires_by_index(index) if expires_by_index is not None else 0.0
        records.append(
            MemoryRecord(
                role="user",
                entry_id=f"mem-{index:04d}",
                kind="fact",
                content=content,
                created_at=float(index + 1),
                updated_at=float(index + 1),
                last_accessed_at=float(index + 1),
                expires_at=expires_at,
                attributes={
                    "origin": "user_explicit",
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                },
            )
        )
    memory.path.write_text("".join(row.to_json() + "\n" for row in records), encoding="utf-8")
    memory.migrate_tiers()
    return memory, records


def test_scope_pairs_1001_records_two_pages_degrades_then_completes_and_warm_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _scope_for(index: int) -> tuple[str, str]:
        if index < 999:
            return ("company", "company:acme") if index % 2 else ("project", "project:seg0")
        if index == 999:
            return ("project", "project:seg0_end")
        return ("project", "project:seg1_tail")

    memory, _records = _seed_migrated_records(tmp_path, 1001, scope_by_index=_scope_for)
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert [seg["count"] for seg in manifest["segments"]] == [1000, 1]

    scan_logs: list[tuple[object, dict[str, object]]] = []
    orig_scan = indexing_module.bounded_authority_records

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        scan_logs.append((continuation, cov))
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    # 第 1 次调用：达到单页预算即中断并降级，绝不对外发布残缺 project 范围。
    degraded_scope = runtime_long_term_scope(memory, task_id="base_task")
    assert ("project", "project:base_task") in degraded_scope.keys
    assert ("project", "project:seg0") not in degraded_scope.keys
    assert ("project", "project:seg1_tail") not in degraded_scope.keys
    assert len(scan_logs) == 1
    assert scan_logs[0][0] is None
    assert scan_logs[0][1]["partial"] is True
    assert scan_logs[0][1]["scanned_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
    assert scan_logs[0][1]["continuation"]["cursor"] == [1, 0]
    assert isinstance(memory._scope_pairs_cache, OrderedDict)
    assert len(memory._scope_pairs_cache["project"]) == 4

    # 在新实例上直接验证第 1 次抛错语义，第 2 次续扫完成全集返回。
    memory2 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    completed_pairs = memory2.scope_pairs("project")
    expected_pairs = (
        ("project", "project:seg0"),
        ("project", "project:seg0_end"),
        ("project", "project:seg1_tail"),
    )
    assert completed_pairs == expected_pairs
    assert scan_logs[-1][0] == {"operation_id": manifest["operation_id"], "cursor": [1, 0]}
    assert scan_logs[-1][1]["partial"] is False

    # 第 3 次调用：命中热缓存，0 次权威扫描、0 次分片 stat、禁止 deepcopy/all()。
    scans_before_warm = len(scan_logs)
    stat_calls: list[str] = []
    orig_stat = Path.stat

    def spy_stat(self, *args, **kwargs):
        stat_calls.append(str(self))
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(
        jsonl_module,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm hit")),
    )
    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm hit")),
    )
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda _self: (_ for _ in ()).throw(AssertionError("all() forbidden on warm hit")),
    )

    warm_pairs = memory2.scope_pairs("project")
    assert warm_pairs is completed_pairs
    assert len(scan_logs) == scans_before_warm
    assert not any("long_term.jsonl.generations" in path for path in stat_calls)


def test_scope_pairs_3001_records_four_segments_converges_and_preserves_scope_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _scope_for(index: int) -> tuple[str, str]:
        if index == 0:
            return ("project", "project:seg0")
        if index == 1500:
            return ("project", "project:seg1")
        if index == 2500:
            return ("project", "project:seg2")
        if index == 3000:
            return ("project", "project:seg3_tail")
        if index % 5 == 0:
            return ("global", "global")
        return ("company", "company:other")

    memory, records = _seed_migrated_records(tmp_path, 3001, scope_by_index=_scope_for)
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert [seg["count"] for seg in manifest["segments"]] == [1000, 1000, 1000, 1]

    from agent_py_agent.agent.common import nofollow_fs

    page_stats: list[tuple[object, dict[str, object], list[tuple[str, ...]]]] = []
    orig_scan = indexing_module.bounded_authority_records
    orig_read = nofollow_fs.read_bytes_beneath
    current_reads: list[tuple[str, ...]] = []

    def spy_read(root, parts, *args, **kwargs):
        current_reads.append(tuple(parts))
        return orig_read(root, parts, *args, **kwargs)

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        current_reads.clear()
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        page_stats.append((continuation, cov, list(current_reads)))
        return rows, cov

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", spy_read)
    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    for expected_cursor in ([1, 0], [2, 0], [3, 0]):
        with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
            memory.scope_pairs("project")
        assert page_stats[-1][1]["partial"] is True
        assert page_stats[-1][1]["continuation"]["cursor"] == expected_cursor

    pairs = memory.scope_pairs("project")
    assert pairs == (
        ("project", "project:seg0"),
        ("project", "project:seg1"),
        ("project", "project:seg2"),
        ("project", "project:seg3_tail"),
    )
    assert len(page_stats) == 4
    assert page_stats[-1][1]["partial"] is False
    seg_names = [Path(seg["path"]).name for seg in manifest["segments"]]
    for page_idx, (_cont, cov, reads) in enumerate(page_stats):
        assert cov["examined_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
        assert cov["scanned_records"] <= _AUTHORITY_SCAN_MAX_RECORDS
        assert cov["scanned_bytes"] <= _AUTHORITY_SCAN_MAX_BYTES
        read_leafs = {parts[-1] for parts in reads if parts}
        for prior_name in seg_names[:page_idx]:
            assert prior_name not in read_leafs
        assert seg_names[page_idx] in read_leafs

    # 普通对话恢复全部跨分片项目召回，同时严守其它 scope 边界与直接按 ID 读。
    ordinary_scope = runtime_long_term_scope(memory)
    assert long_term_record_matches_scope(records[0], ordinary_scope)
    assert long_term_record_matches_scope(records[3000], ordinary_scope)
    assert long_term_record_matches_scope(records[5], ordinary_scope)
    assert not long_term_record_matches_scope(records[1], ordinary_scope)

    explicit_scope = MemoryRecallScope.from_runtime(task_id="only_here")
    assert explicit_scope.allows("project", "project:only_here")
    assert not explicit_scope.allows("project", "project:seg3_tail")

    frozen_ctx = BusinessExecutionContext(
        "owner", "space-A", "biz-alpha", "task-A", 1, None, (), (), "business-context.v1"
    )
    frozen_scope = runtime_long_term_scope(memory, task_id="task-A", business_context=frozen_ctx)
    assert frozen_scope.allows("project", "project:biz-alpha")
    assert frozen_scope.allows("project", "project:task-A")
    assert frozen_scope.allows("global", "global")
    assert frozen_scope.allows("personal", "personal")
    assert not frozen_scope.allows("project", "project:seg0")
    assert not frozen_scope.allows("project", "project:seg3_tail")

    while memory.rebuild_read_index(batch_size=1000)["status"] != "complete":
        pass
    loaded_tail = memory.read_ids([records[3000].entry_id])
    assert [row.entry_id for row in loaded_tail] == [records[3000].entry_id]


def test_mid_segment_offset_resumption_and_byte_limit_never_move_cursor_backwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 使用 40 字符内容触发 active/cold 分层，产生非 1000 整除的分片边界从而覆盖分片内偏移续扫。
    memory, _records = _seed_migrated_records(
        tmp_path,
        2501,
        scope_by_index=lambda i: ("project", f"project:seg_{i // 700}"),
        content_fn=lambda i: f"payload-{i:04d}-" + ("x" * 28),
    )
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None
    assert len(manifest["segments"]) >= 3

    cursors: list[list[int] | None] = []
    orig_scan = indexing_module.bounded_authority_records

    def spy_scan(mem, *, max_records, max_bytes, continuation=None):
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        cont = cov.get("continuation")
        cursors.append(cont["cursor"] if cont else None)
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", spy_scan)

    for _ in range(2):
        with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
            memory.scope_pairs("project")
    assert cursors[0] is not None and cursors[0][1] > 0
    assert cursors[1] is not None and tuple(cursors[1]) > tuple(cursors[0])

    pairs = memory.scope_pairs("project")
    assert pairs == tuple(("project", f"project:seg_{idx}") for idx in range(4))
    assert cursors[-1] is None

    # 字节上限触发在到达 resume_off 之前时，cursor 不得倒退到 [seg, 0]。
    seg_path = memory.path.parent / manifest["segments"][0]["path"]
    seg_bytes = seg_path.stat().st_size
    rows, cov = bounded_authority_records(
        memory,
        max_records=100,
        max_bytes=seg_bytes // 2,
        continuation={"operation_id": manifest["operation_id"], "cursor": [0, 5]},
    )
    assert rows == []
    assert cov["partial"] is True
    assert cov["continuation"]["cursor"] == [0, 5]


def test_scope_pairs_invalidates_partial_progress_on_delete_generation_change_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = 1_750_000_000.0
    fake_clock = [t0]
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])

    def _scope_for(index: int) -> tuple[str, str]:
        if index == 0:
            return ("project", "project:doomed")
        if index == 1:
            return ("project", "project:ephemeral")
        if index == 1002:
            return ("project", "project:survivor")
        return ("project", "project:steady")

    def _expiry_for(index: int) -> float:
        return t0 + 50.0 if index == 1 else 0.0

    memory, records = _seed_migrated_records(
        tmp_path,
        1003,
        scope_by_index=_scope_for,
        expires_by_index=_expiry_for,
    )

    # 1. 页间 canonical 删除使旧部分进度失效并从新代起点重扫。
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    assert "project" in memory._scope_pairs_cache

    memory.remove(records[0].entry_id)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    pairs_after_delete = memory.scope_pairs("project")
    assert ("project", "project:doomed") not in pairs_after_delete
    assert pairs_after_delete == (
        ("project", "project:ephemeral"),
        ("project", "project:steady"),
        ("project", "project:survivor"),
    )

    # 2. 页间时钟跨过最近 expires_at 时，旧部分进度立即失效，不再带出过期项目。
    sub_dir = tmp_path / "expiry_case"
    sub_records: list[MemoryRecord] = []
    # active: 500 条非过期 (~12KB) + 1 条临期 (t0+50)；cold: 1001 条非过期 (~25KB)，确保首页命中临期记录后仍跨页。
    for idx in range(1502):
        if idx < 500:
            s_key, exp, ts = "project:steady", 0.0, 10_000.0 + idx
        elif idx == 500:
            s_key, exp, ts = "project:ephemeral", t0 + 50.0, 20_000.0
        else:
            s_key, exp, ts = ("project:survivor" if idx == 1501 else "project:steady"), 0.0, float(idx)
        sub_records.append(
            MemoryRecord(
                role="user",
                entry_id=f"exp-{idx:04d}",
                kind="fact",
                content=f"exp-{idx:04d}-" + ("v" * 16),
                created_at=ts,
                updated_at=ts,
                last_accessed_at=ts,
                expires_at=exp,
                attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": s_key},
            )
        )
    memory2 = JsonlMemory(sub_dir / "memory" / "long_term.jsonl")
    memory2.path.write_text("".join(row.to_json() + "\n" for row in sub_records), encoding="utf-8")
    memory2.migrate_tiers()

    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    cached_partial = memory2._scope_pairs_cache["project"]
    assert len(cached_partial) == 4
    assert cached_partial[1] == t0 + 50.0
    assert ("project", "project:ephemeral") in cached_partial[2]

    fake_clock[0] = t0 + 51.0
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory2.scope_pairs("project")
    assert ("project", "project:ephemeral") not in memory2._scope_pairs_cache["project"][2]
    pairs_after_expiry = memory2.scope_pairs("project")
    assert ("project", "project:ephemeral") not in pairs_after_expiry
    assert pairs_after_expiry == (
        ("project", "project:steady"),
        ("project", "project:survivor"),
    )

    # 3. 页间新增写入（换代）同样使旧部分进度失效，不会遗漏新增 scope。
    memory3 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory3.scope_pairs("project")
    memory3.add(
        "user",
        "h",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:added_midway"},
    )
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory3.scope_pairs("project")
    pairs_after_add = memory3.scope_pairs("project")
    assert ("project", "project:added_midway") in pairs_after_add
    assert ("project", "project:survivor") in pairs_after_add

    # 4. 续扫末页完成瞬间若发生并发修订变更，不得混用两代结果，必须清缓存并抛不可用。
    memory4 = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory4.scope_pairs("project")
    assert len(memory4._scope_pairs_cache["project"]) == 4

    writer = JsonlMemory(memory.path)
    orig_scan = indexing_module.bounded_authority_records
    bumped = False

    def interleave_on_resume(mem, *, max_records, max_bytes, continuation=None):
        nonlocal bumped
        rows, cov = orig_scan(mem, max_records=max_records, max_bytes=max_bytes, continuation=continuation)
        if continuation is not None and not bumped:
            bumped = True
            writer.add(
                "user",
                "w",
                kind="fact",
                attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:concurrent"},
            )
        return rows, cov

    monkeypatch.setattr(indexing_module, "bounded_authority_records", interleave_on_resume)
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory4.scope_pairs("project")
    assert bumped is True
    assert "project" not in memory4._scope_pairs_cache


def test_bounded_authority_records_backward_compatible_and_stale_continuation(tmp_path: Path) -> None:
    memory, records = _seed_migrated_records(
        tmp_path,
        6,
        scope_by_index=lambda i: ("project", f"project:p{i}"),
    )
    manifest = MemoryGenerationStore(memory.path).manifest()
    assert manifest is not None

    # 1. 不传 continuation（旧调用方兼容）
    direct_rows, direct_cov = bounded_authority_records(memory, max_records=4, max_bytes=1024 * 1024)
    assert [row.entry_id for row in direct_rows] == [row.entry_id for row in records[:4]]
    assert direct_cov["partial"] is True
    assert direct_cov["continuation"] == {"operation_id": manifest["operation_id"], "cursor": [0, 4]}

    # 2. 传入已失效或格式非法的 continuation，均安全退回从 [0, 0] 扫描
    for bad_cont in (
        {"operation_id": "stale-op-id", "cursor": [0, 4]},
        {"operation_id": "", "cursor": [0, 2]},
        {"operation_id": manifest["operation_id"], "cursor": [-1, 0]},
        {"operation_id": manifest["operation_id"], "cursor": [True, 0]},
        {"operation_id": manifest["operation_id"], "cursor": [0]},
        "bad-continuation",
    ):
        rows, cov = bounded_authority_records(
            memory,
            max_records=4,
            max_bytes=1024 * 1024,
            continuation=bad_cont,
        )
        assert [row.entry_id for row in rows] == [row.entry_id for row in records[:4]]
        assert cov["continuation"] == {"operation_id": manifest["operation_id"], "cursor": [0, 4]}
