"""W7 与 V2-5 行为验收测试：有界 lesson/HOT 读取、generation 级 scope_pairs LRU 投影、
非 touch read_ids 刷新、缺损自愈与按最终入选去重记命中。

全部基于 tmp_path 与隔离环境运行，不修改真机 ~/.my-agent。
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _confirm_final_memory_access,
    _formal_memories_for_request,
    _prepare_runtime_context,
    _refresh_recall_candidates,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_routing import RoutedMemoryContext
from agent_py_agent.agent.memory_store import _jsonl_indexing as indexing_module
from agent_py_agent.agent.memory_store import jsonl as jsonl_module
from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
from agent_py_agent.agent.memory_store.generation_manifest import ContextBudget
from agent_py_agent.agent.memory_store.jsonl import (
    _SCOPE_PAIRS_CACHE_MAX_ENTRIES,
    JsonlMemory,
    MemoryRecord,
)
from agent_py_agent.agent.memory_store.lessons import (
    HOT_SCHEMA_VERSION,
    LESSON_SCHEMA_VERSION,
    HotRuleRecord,
    HotRuleRepository,
    LessonRecord,
    LessonRepository,
    _render_hot,
    _render_lesson,
)
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    hot_memory_records,
    routed_lesson_records,
    runtime_long_term_scope,
)
from agent_py_agent.agent.prompting_parts.memory_context import BudgetedMemoryRecords
from agent_py_agent.agent.settings.config import AgentConfig


def _repos(root: Path) -> tuple[LessonRepository, HotRuleRepository, JsonlMemory]:
    memory_dir = root / "memory"
    return (
        LessonRepository(
            memory_dir / "lessons",
            memory_dir / "routing" / "INDEX.md",
        ),
        HotRuleRepository(memory_dir / "memory-hot.md"),
        JsonlMemory(memory_dir / "long_term.jsonl"),
    )


def _lesson_candidate(
    cid: str,
    *,
    subject_key: str,
    content: str,
    scope_type: str = "project",
    scope_key: str = "project:alpha",
    occurrence_count: int = 3,
) -> MemoryCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type="lesson",
        content=content,
        subject_key=subject_key,
        scope={"scope_type": scope_type, "scope_key": scope_key},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.95,
        proposed_action="add",
        target_entry_id="",
        conflicts_with=[],
        promotion_target="lesson",
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _hot_candidate(
    cid: str,
    *,
    target_lesson_id: str,
    subject_key: str,
    rule: str,
    occurrence_count: int = 4,
) -> MemoryCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type="hot_rule",
        content=rule,
        subject_key=subject_key,
        scope={"scope_type": "project", "scope_key": "project:alpha"},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.95,
        proposed_action="add",
        target_entry_id=target_lesson_id,
        conflicts_with=[],
        promotion_target="hot",
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _write_canonical_lesson_file(
    lessons_dir: Path,
    *,
    index: int,
    status: str = "active",
    scope_type: str = "project",
    scope_key: str = "project:alpha",
    subject_key: str | None = None,
    lesson_id: str | None = None,
    content: str | None = None,
) -> LessonRecord:
    lessons_dir.mkdir(parents=True, exist_ok=True)
    hex8 = f"{index:08x}"
    filename = f"topic-{index:04d}-{hex8}.md"
    rel_path = f"memory/lessons/{filename}"
    now_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    record = LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id=lesson_id or f"lesson-{index:04d}",
        candidate_id=f"cand-{index:04d}",
        subject_key=subject_key or f"subj.{index:04d}",
        scope={"scope_type": scope_type, "scope_key": scope_key},
        content=content or f"教训内容 {index}",
        occurrence_count=2,
        evidence_groups=("task:a", "task:b"),
        source_task_ids=("a", "b"),
        source_run_ids=("r1", "r2"),
        created_at=now_iso,
        updated_at=now_iso,
        path=rel_path,
        status=status,
    )
    (lessons_dir / filename).write_text(_render_lesson(record), encoding="utf-8")
    return record


def test_hot_and_routed_recall_read_only_selected_canonical_refs_across_500_live_and_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, _memory = _repos(tmp_path)
    (tmp_path / "memory" / "migration.json").write_text('{"version": 2}\n', encoding="utf-8")

    # 500 条 live 正式 lesson + 60 条永久归档 lesson
    live: list[LessonRecord] = []
    for i in range(500):
        live.append(_write_canonical_lesson_file(lessons.lessons_dir, index=i))
    archive_dir = lessons.lessons_dir / "archive" / "2026-01"
    for i in range(500, 560):
        _write_canonical_lesson_file(archive_dir, index=i, status="archived")

    # 写 2 条共享同一 lesson 的 HOT 规则与 1 条引用另一 lesson 的 HOT 规则
    target_a = live[10]
    target_b = live[200]
    now_dt = datetime.now(timezone.utc)
    hot_rules = [
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-1",
            candidate_id="cand-h1",
            lesson_id=target_a.lesson_id,
            lesson_ref=f"{target_a.path}#{target_a.lesson_id}",
            rule="先看边界。",
            occurrence_count=5,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=3)).isoformat(),
        ),
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-2",
            candidate_id="cand-h2",
            lesson_id=target_a.lesson_id,
            lesson_ref=f"{target_a.path}#{target_a.lesson_id}",
            rule="同源复核。",
            occurrence_count=4,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=2)).isoformat(),
        ),
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-3",
            candidate_id="cand-h3",
            lesson_id=target_b.lesson_id,
            lesson_ref=f"{target_b.path}#{target_b.lesson_id}",
            rule="先跑回归。",
            occurrence_count=3,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=1)).isoformat(),
        ),
    ]
    hot.path.write_text(_render_hot(hot_rules), encoding="utf-8")

    read_calls: list[list[str]] = []
    orig_read_refs = LessonRepository.read_refs

    def spy_read_refs(self, refs):
        batch = list(refs)
        read_calls.append(batch)
        return orig_read_refs(self, batch)

    monkeypatch.setattr(
        LessonRepository,
        "list",
        lambda self: (_ for _ in ()).throw(AssertionError("full lesson list() must not be called")),
    )
    monkeypatch.setattr(LessonRepository, "read_refs", spy_read_refs)

    scope = MemoryRecallScope.from_runtime(task_id="alpha")

    # 1. 空 selection 零磁盘/read_refs 读取
    assert routed_lesson_records(lessons, read_paths=[], scope=scope) == []
    assert read_calls == []

    # 2. routed 只读取去重后的规范路径，保持 (subject_key, lesson_id) 确定顺序
    routed = routed_lesson_records(
        lessons,
        read_paths=[live[350].path, live[10].path, live[350].path],
        scope=scope,
    )
    assert [row.entry_id for row in routed] == [live[10].lesson_id, live[350].lesson_id]
    assert read_calls == [[live[350].path, live[10].path]]

    # 3. HOT 只读取预算后去重的规范 ref（2 个去重 ref，而不是 500 个）
    read_calls.clear()
    hot_records = hot_memory_records(hot, lessons, scope=scope)
    assert [row.entry_id for row in hot_records] == ["hot-1", "hot-2", "hot-3"]
    assert read_calls == [[hot_rules[0].lesson_ref, hot_rules[2].lesson_ref]]


def test_hot_and_routed_reject_superseded_mismatched_id_wrong_scope_and_noncanonical_refs(
    tmp_path: Path,
) -> None:
    lessons, hot, _memory = _repos(tmp_path)
    valid = lessons.promote(
        _lesson_candidate("c-valid", subject_key="alpha.ok", content="有效教训"),
    )
    old = lessons.promote(
        _lesson_candidate("c-old", subject_key="alpha.superseded", content="旧教训"),
    )
    successor, _ = lessons.supersede_with_receipt(
        _lesson_candidate("c-new", subject_key="alpha.superseded", content="新教训"),
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    wrong_scope = lessons.promote(
        _lesson_candidate(
            "c-scope",
            subject_key="company.only",
            content="仅限公司作用域",
            scope_type="company",
            scope_key="company:acme",
        ),
    )
    mismatch_target = _write_canonical_lesson_file(
        lessons.lessons_dir,
        index=900,
        lesson_id="actual-lesson-900",
        subject_key="alpha.mismatch",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    hot.path.write_text(
        _render_hot(
            [
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-ok",
                    candidate_id="ch-1",
                    lesson_id=valid.lesson_id,
                    lesson_ref=f"{valid.path}#{valid.lesson_id}",
                    rule="有效短规则。",
                    occurrence_count=5,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-superseded",
                    candidate_id="ch-2",
                    lesson_id=old.lesson_id,
                    lesson_ref=f"{old.path}#{old.lesson_id}",
                    rule="已替代规则不应进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-id-frag",
                    candidate_id="ch-3",
                    lesson_id="different-id",
                    lesson_ref=f"{mismatch_target.path}#{mismatch_target.lesson_id}",
                    rule="fragment 与 rule.lesson_id 不一致。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-id-no-frag",
                    candidate_id="ch-4",
                    lesson_id="different-id-2",
                    lesson_ref=mismatch_target.path,
                    rule="lesson_id 与文件头不一致。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-scope",
                    candidate_id="ch-5",
                    lesson_id=wrong_scope.lesson_id,
                    lesson_ref=f"{wrong_scope.path}#{wrong_scope.lesson_id}",
                    rule="跨 scope 不应进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-noncanonical",
                    candidate_id="ch-6",
                    lesson_id=valid.lesson_id,
                    lesson_ref="../escape-path.md",
                    rule="非规范引用不得进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
            ]
        ),
        encoding="utf-8",
    )

    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_out = hot_memory_records(hot, lessons, scope=scope)
    assert [row.entry_id for row in hot_out] == ["hot-ok"]

    routed_out = routed_lesson_records(
        lessons,
        read_paths=[
            old.path,
            successor.path,
            wrong_scope.path,
            "../noncanonical.md",
            "memory/lessons/draft.md",
        ],
        scope=scope,
    )
    assert [row.entry_id for row in routed_out] == [successor.lesson_id]


def test_read_ids_non_touch_order_and_refresh_avoids_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, memory = _repos(tmp_path)
    now = time.time()
    r1 = memory.add(
        role="user",
        content="项目事实一",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:alpha"},
    )
    r2 = memory.add(
        role="user",
        content="个人事实二",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    r_other = memory.add(
        role="user",
        content="其他项目事实",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:beta"},
    )
    r_removed = memory.add(
        role="user",
        content="将被删除",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    r_expired = memory.add_record(
        MemoryRecord(
            role="user",
            content="已过期事实",
            kind="fact",
            attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
            expires_at=now - 10.0,
        )
    )
    memory.remove(r_removed.entry_id)
    memory.migrate_tiers()
    memory.rebuild_read_index()

    assert memory.read_ids([]) == []
    got = memory.read_ids([r2.entry_id, r1.entry_id, r1.entry_id, r_removed.entry_id, r_expired.entry_id, "missing"])
    assert [row.entry_id for row in got] == [r2.entry_id, r1.entry_id]
    assert all(row.last_accessed_at == 0.0 for row in got)
    assert memory._pending_access == {}

    # 禁止调用 all()，用 _refresh_recall_candidates 分别刷新 1 个和 3 个 ID
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda self: (_ for _ in ()).throw(AssertionError("memory.all() must not be called during refresh")),
    )
    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_lesson = lessons.promote(
        _lesson_candidate("c-hl", subject_key="alpha.hl", content="热教训本体"),
    )
    hot.promote(
        _hot_candidate("ch-hl", target_lesson_id=hot_lesson.lesson_id, subject_key="alpha.hl", rule="热短规"),
        lesson=hot_lesson,
    )
    # 同时往 JSONL 写入一个与 hot 同 ID 的普通记录以测试退役回落
    same_id_jsonl = memory.add_record(
        MemoryRecord(
            role="user",
            content="旧 JSONL 同 ID",
            kind="fact",
            entry_id="hot-shadow",
            attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
        )
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    agent = SimpleNamespace(
        memory=memory,
        memory_hot=hot,
        memory_lessons=lessons,
        config=SimpleNamespace(home_lesson_stale_caveat_days=7.0),
    )

    refreshed_1 = _refresh_recall_candidates(agent, [r1], [], scope)
    assert [row.entry_id for row in refreshed_1] == [r1.entry_id]
    assert memory._pending_access == {}

    fixed_ids = {"hot-shadow"}
    refreshed_3 = _refresh_recall_candidates(
        agent,
        [same_id_jsonl, r2, r_other, r1],
        [],
        scope,
        fixed_source_ids=fixed_ids,
        budget=ContextBudget(12000, 0),
    )
    # r_other 不在 alpha scope 内；hot-shadow 已不在 HOT 中，应从 fixed_ids 移出以便最终确认
    assert [row.entry_id for row in refreshed_3] == [same_id_jsonl.entry_id, r2.entry_id, r1.entry_id]
    assert "hot-shadow" not in fixed_ids
    assert memory._pending_access == {}

    # 更新版本并删除一条，再刷新应同步反映最新正文并移除已删记录
    r2_v2 = memory.replace(r2.entry_id, "个人事实二-新版")
    memory.remove(r1.entry_id)
    memory.migrate_tiers()
    memory.rebuild_read_index()
    refreshed_after = _refresh_recall_candidates(
        agent,
        [r2, r1],
        [],
        scope,
        budget=ContextBudget(12000, 0),
    )
    assert [(row.entry_id, row.content, row.version) for row in refreshed_after] == [
        (r2.entry_id, "个人事实二-新版", r2_v2.version),
    ]


def test_read_ids_stale_index_unreadable_segment_and_incomplete_bounded_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    r1 = memory.add(
        role="user",
        content="正式记忆",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    # 1. 有界扫描覆盖不全（index 不可用且 partial=True）必须显式区别于证明为空 []
    idx_path = memory.path.parent / "long_term.jsonl.read_index.sqlite3"
    idx_path.unlink()
    monkeypatch.setattr(
        indexing_module,
        "bounded_authority_records",
        lambda *_args, **_kwargs: ([], {"partial": True, "scanned_records": 1, "scanned_bytes": 100}),
    )
    with pytest.raises(ValueError, match="MEMORY_AUTHORITY_INCOMPLETE"):
        memory.read_ids([r1.entry_id])

    # 2. 索引失效 + 单个分片不可读，不得静默复活旧文件已删数据或假装空库
    monkeypatch.undo()
    seg_dir = memory.path.parent / "long_term.jsonl.generations"
    for seg in seg_dir.rglob("*.jsonl"):
        seg.unlink()
    with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
        memory.read_ids([r1.entry_id])


def test_scope_pairs_warm_hit_zero_segment_stat_no_deepcopy_no_all_and_invalidation_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    t0 = time.time()
    fake_clock = [t0]
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])

    old_ts = t0 - 365 * 86400
    memory.add_record(
        MemoryRecord(
            role="user",
            content="冷数据项目",
            kind="fact",
            created_at=old_ts,
            updated_at=old_ts,
            last_accessed_at=old_ts,
            attributes={"scope_type": "project", "scope_key": "project:cold"},
        )
    )
    memory.add_record(
        MemoryRecord(
            role="user",
            content="临期项目",
            kind="fact",
            expires_at=t0 + 120.0,
            attributes={"scope_type": "project", "scope_key": "project:expiring"},
        )
    )
    memory.add(
        role="user",
        content="其他领域",
        kind="fact",
        attributes={"scope_type": "company", "scope_key": "company:acme"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    # 冷启动重建
    cold_pairs = memory.scope_pairs("project")
    assert cold_pairs == (("project", "project:cold"), ("project", "project:expiring"))

    # 热命中禁止 deepcopy、禁止 all()、禁止 stat .generations 下的分片
    stat_calls: list[str] = []
    orig_stat = Path.stat

    def spy_stat(self, *args, **kwargs):
        stat_calls.append(str(self))
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(
        jsonl_module,
        "deepcopy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm scope_pairs")),
    )
    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm scope_pairs")),
    )
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda self: (_ for _ in ()).throw(AssertionError("all() forbidden on scope_pairs")),
    )

    warm_pairs = memory.scope_pairs("project")
    assert warm_pairs is cold_pairs
    assert not any("long_term.jsonl.generations" in p for p in stat_calls)

    # 写入新记录立即失效并包含新作用域
    monkeypatch.undo()
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])
    added = memory.add(
        role="user",
        content="新写入项目",
        kind="fact",
        attributes={"scope_type": "project", "scope_key": "project:fresh"},
    )
    assert memory.scope_pairs("project") == (
        ("project", "project:cold"),
        ("project", "project:expiring"),
        ("project", "project:fresh"),
    )

    # 删除记录立即失效
    memory.remove(added.entry_id)
    assert memory.scope_pairs("project") == (("project", "project:cold"), ("project", "project:expiring"))

    # 注入时钟越过最近 expires_at（无需 sleep）自动失效重算
    fake_clock[0] = t0 + 121.0
    assert memory.scope_pairs("project") == (("project", "project:cold"),)

    # 写入变更使缓存失效后，部分扫描重建失败不得写入空元组缓存；恢复后应立即算回真实范围
    added_2 = memory.add(
        role="user",
        content="变更触发重算",
        kind="fact",
        attributes={"scope_type": "project", "scope_key": "project:after_partial"},
    )
    assert added_2.entry_id
    monkeypatch.setattr(
        indexing_module,
        "bounded_authority_records",
        lambda *_args, **_kwargs: ([], {"partial": True, "scanned_records": 1, "scanned_bytes": 100}),
    )
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    assert "project" not in memory._scope_pairs_cache
    # runtime_long_term_scope 在不可用时降级保留基础 runtime scope，不崩召回
    degraded_scope = runtime_long_term_scope(memory, task_id="base_task")
    assert ("project", "project:base_task") in degraded_scope.keys

    monkeypatch.undo()
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])
    assert memory.scope_pairs("project") == (("project", "project:after_partial"), ("project", "project:cold"))


def test_scope_pairs_cache_strictly_bounded_ordered_dict_lru(tmp_path: Path) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    total = _SCOPE_PAIRS_CACHE_MAX_ENTRIES + 12
    for i in range(total):
        assert memory.scope_pairs(f"scope_type_{i}") == ()

    assert isinstance(memory._scope_pairs_cache, OrderedDict)
    assert len(memory._scope_pairs_cache) == _SCOPE_PAIRS_CACHE_MAX_ENTRIES
    assert "scope_type_0" not in memory._scope_pairs_cache
    assert f"scope_type_{total - 1}" in memory._scope_pairs_cache


def test_manual_deletion_self_healing_v2_5(tmp_path: Path) -> None:
    lessons, hot, memory = _repos(tmp_path)
    lesson = lessons.promote(
        _lesson_candidate("c-del", subject_key="self.heal", content="删除自愈验证"),
    )
    hot.promote(
        _hot_candidate("ch-del", target_lesson_id=lesson.lesson_id, subject_key="self.heal", rule="自愈规则"),
        lesson=lesson,
    )
    r1 = memory.add(
        role="user",
        content="需要持久化的项目记忆",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:alpha"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    assert memory.scope_pairs("project") == (("project", "project:alpha"),)
    assert len(memory.read_ids([r1.entry_id])) == 1
    assert len(hot_memory_records(hot, lessons, scope=scope)) == 1
    assert len(routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope)) == 1

    # 1. 手动删墓碑文件：runtime_long_term_scope 温和降级，缓存失效
    for tb in memory.path.parent.glob("*storage_tombstones*"):
        tb.unlink()
    degraded = runtime_long_term_scope(memory, task_id="alpha")
    assert ("project", "project:alpha") in degraded.keys

    # 2. 手动删整块 memory/ 目录：scope_pairs / read_ids / hot / routed 全部自愈返回空集不抛致命异常
    shutil.rmtree(tmp_path / "memory")
    assert memory.scope_pairs("project") == ()
    assert memory.read_ids([r1.entry_id]) == []
    assert hot_memory_records(hot, lessons, scope=scope) == []
    assert routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope) == []

    # 3. 手动删整个 owner 目录：同样干净自愈
    shutil.rmtree(tmp_path)
    assert memory.scope_pairs("project") == ()
    assert memory.read_ids([r1.entry_id]) == []
    assert hot_memory_records(hot, lessons, scope=scope) == []
    assert routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope) == []


def test_final_hit_counting_once_per_turn_and_zero_for_excluded_or_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, memory = _repos(tmp_path)
    l1 = lessons.promote(
        _lesson_candidate("c-hit-1", subject_key="hit.one", content="命中的教训一"),
    )
    l2 = lessons.promote(
        _lesson_candidate("c-hit-2", subject_key="hit.two", content="命中的教训二"),
    )
    l_rejected_budget = lessons.promote(
        _lesson_candidate("c-hit-budget", subject_key="hit.zzz", content="超预算教训" * 4000),
    )
    l_wrong_scope = lessons.promote(
        _lesson_candidate(
            "c-hit-scope",
            subject_key="hit.other",
            content="非本作用域教训",
            scope_type="company",
            scope_key="company:other",
        ),
    )
    h1 = hot.promote(
        _hot_candidate("ch-hit-1", target_lesson_id=l1.lesson_id, subject_key="hit.one", rule="短规则一"),
        lesson=l1,
    )

    hit_calls: list[list[str]] = []
    orig_note_hits = LessonRepository.note_hits

    def spy_note_hits(self, lesson_ids, *, at=None):
        batch = list(lesson_ids)
        hit_calls.append(batch)
        return orig_note_hits(self, batch, at=at)

    monkeypatch.setattr(LessonRepository, "note_hits", spy_note_hits)

    agent = SimpleNamespace(
        memory=memory,
        memory_hot=hot,
        memory_lessons=lessons,
        config=SimpleNamespace(home_lesson_stale_caveat_days=7.0),
    )
    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_recs = hot_memory_records(hot, lessons, scope=scope)
    routed_recs = routed_lesson_records(
        lessons,
        read_paths=[l1.path, l2.path, l_rejected_budget.path, l_wrong_scope.path],
        scope=scope,
    )
    assert [row.entry_id for row in hot_recs] == [h1.hot_id]
    assert [row.entry_id for row in routed_recs] == [l1.lesson_id, l2.lesson_id, l_rejected_budget.lesson_id]

    heat_path = tmp_path / "memory" / "routing" / "lesson_heat.json"
    heat_before_text = heat_path.read_text(encoding="utf-8")
    heat_before_mtime_ns = heat_path.stat().st_mtime_ns

    index_path = str(tmp_path / "memory" / "routing" / "INDEX.md")
    # 1. 仅选中 h1、l1、l2，跳过超大 l_rejected_budget；同源 HOT+routed l1 只计 1 次
    final_budgeted = _formal_memories_for_request(
        agent,
        RuntimeContextRequest("检查边界", None, None),
        RoutedMemoryContext(
            enabled=True,
            index_path=index_path,
            receipts=[
                {"path": l1.path, "status": "read"},
                {"path": l2.path, "status": "read"},
                {"path": l_rejected_budget.path, "status": "read"},
                {"path": l_wrong_scope.path, "status": "read"},
            ],
            findings=[],
        ),
        recall_scope=scope,
        long_term_memories=[],
        skip_formal_recall=False,
    )
    selected_ids = {row.entry_id for row in final_budgeted}
    # 确认总候选不跨预算后触发唯一一次 note_hits
    assert l1.lesson_id in selected_ids and l2.lesson_id in selected_ids
    assert l_rejected_budget.lesson_id not in selected_ids
    assert len(hit_calls) == 1
    assert hit_calls[0] == [l1.lesson_id, l2.lesson_id]
    assert set(lessons._pending_hits_map()) == {l1.lesson_id, l2.lesson_id}
    assert all(count == 1 for count, _epoch in lessons._pending_hits_map().values())
    assert heat_path.read_text(encoding="utf-8") == heat_before_text
    assert heat_path.stat().st_mtime_ns == heat_before_mtime_ns

    # 2. 验证超大预算截断被拒绝的 lesson 不计入命中（直接用明确 Budget 测试）
    hit_calls.clear()
    _confirm_final_memory_access(
        agent,
        BudgetedMemoryRecords(hot_recs[:1]),  # 仅 h1
        scope,
        {h1.hot_id},
        RoutedMemoryContext(enabled=True, index_path=index_path, receipts=[], findings=[]),
    )
    assert hit_calls == [[l1.lesson_id]]
    assert l_rejected_budget.lesson_id not in lessons._pending_hits_map()
    assert l_wrong_scope.lesson_id not in lessons._pending_hits_map()

    # 3. 隔离上下文（task_local / control_plane）不得触发 note_hits
    hit_calls.clear()
    for isolated_scope in ("task_local", "control_plane"):
        _formal_memories_for_request(
            agent,
            RuntimeContextRequest("隔离执行", None, None, context_scope=isolated_scope),
            RoutedMemoryContext(
                enabled=True,
                index_path=index_path,
                receipts=[{"path": l1.path, "status": "read"}],
                findings=[],
            ),
            recall_scope=scope,
            long_term_memories=[],
            skip_formal_recall=True,
        )
    assert hit_calls == []
    # 纯 _prepare_runtime_context 在 task_local 下也不会计命中
    real_agent = SimpleAgent(
        AgentConfig(enable_tools=False),
        tmp_path,
    )
    real_agent.memory = memory
    real_agent.memory_hot = hot
    real_agent.memory_lessons = lessons
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runtime.loop_support.build_routed_memory_context",
        lambda *_args, **_kwargs: RoutedMemoryContext(
            enabled=True,
            index_path=index_path,
            receipts=[{"path": l1.path, "status": "read"}],
            findings=[],
        ),
    )
    ctx = _prepare_runtime_context(
        real_agent,
        RuntimeContextRequest("隔离执行", None, None, context_scope="task_local"),
    )
    assert ctx is not None
    assert hit_calls == []
    # lesson_heat.json 始终零磁盘写
    assert heat_path.read_text(encoding="utf-8") == heat_before_text
    assert heat_path.stat().st_mtime_ns == heat_before_mtime_ns
    assert json.loads(json.dumps(list(lessons._pending_hits_map().keys()))) == [l1.lesson_id, l2.lesson_id]
