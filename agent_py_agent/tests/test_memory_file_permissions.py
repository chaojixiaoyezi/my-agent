"""装记忆正文的其它文件也按私有原子写（S2，be 复审语义记忆的后续项）：候选、日事件、lesson/INDEX/HOT、Curator 事务目标与前镜像，
以及一次性迁移的备份副本（私有复制，保留原字节和修改时间）。

钉的是“替换之后”的权限：先放 0644 的旧文件、0755 的目录，下一次写入后文件必须是 0600、目录权限一律不动
（pdp 2026-10-03：私有写只动自己建的东西）；umask 放到 0 也一样（临时文件一出生就是 0600，替换时不抄回旧权限）。
生产里已有的旧文件靠下次写入收紧，不做批量 chmod。
全部用临时目录和测试替身，不调真实模型。
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.memory_store.lessons import (
    HotRuleRepository,
    LessonRepository,
    _render_hot,
)
from agent_py_agent.tests.test_hot_budget import _hot, _hot_candidate, _hot_pile, _lesson
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)
from agent_py_agent.tests.test_memory_migration_v2 import _runtime as _migration_runtime

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")
_NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


# 函数用途: 把进程 umask 临时放到 0，验证私有写入不依赖 umask；用例结束恢复原值。
@pytest.fixture
def open_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


# 函数用途: 读一个路径的权限位。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# 函数用途: 把文件和它的目录放宽成生产旧状态（文件 0644、目录 0755）。
def _loosen(path: Path) -> None:
    os.chmod(path, 0o644)
    os.chmod(path.parent, 0o755)


# 函数用途: 构造一条普通候选观察。
def _observation(content: str, key: str) -> CandidateObservation:
    return CandidateObservation(candidate_type="long_term_fact", content=content, subject_key=key,
                                scope=MemoryScope("personal", "personal"), origin="user_explicit", observation_id=key)


# 函数用途: 构造某天的一条日事件。
def _daily_event(summary: str, minute: int) -> DailyMemoryEvent:
    return DailyMemoryEvent(event_type="conversation", summary=summary, actor="user", origin="user_explicit",
                            session_id=f"s{minute}", thread_id=f"t{minute}", created_at=f"2026-05-13T12:{minute:02d}:00+00:00",
                            extracted_at=f"2026-05-13T12:{minute:02d}:30+00:00", curator_run_id=f"run-{minute}")


@_POSIX_ONLY
def test_candidates_are_tightened_on_the_next_write(tmp_path, open_umask):
    service = CandidateService(tmp_path / "memory" / "candidates.jsonl")
    service.observe(_observation("客户要求本季度完成支付迁移", "work:migration"))
    assert (_mode(service.path), _mode(service.path.parent)) == (0o600, 0o700)

    _loosen(service.path)
    service.observe(_observation("团建定在周五下午", "work:teambuilding"))

    assert (_mode(service.path), _mode(service.path.parent)) == (0o600, 0o755), "文件被收紧；已存在的目录权限不动"
    assert len(service.list()) == 2


@_POSIX_ONLY
def test_daily_shard_is_tightened_on_the_next_write(tmp_path, open_umask):
    store = DailyMemoryStore(tmp_path / "memory" / "daily")
    store.append(_daily_event("用户喜欢表格", 0))
    shard = tmp_path / "memory" / "daily" / "2026-05-13.jsonl"
    assert (_mode(shard), _mode(shard.parent)) == (0o600, 0o700)

    _loosen(shard)
    store.append(_daily_event("用户想要干净的目录", 5))

    assert (_mode(shard), _mode(shard.parent)) == (0o600, 0o755), "文件被收紧；已存在的目录权限不动"
    assert len(store.list(day="2026-05-13")) == 2


@_POSIX_ONLY
def test_lesson_file_is_private_and_routing_index_is_tightened_on_rebuild(tmp_path, open_umask):
    lessons_dir, index = tmp_path / "memory" / "lessons", tmp_path / "memory" / "routing" / "INDEX.md"
    repository = LessonRepository(lessons_dir, index)
    candidate = replace(_hot_candidate(content="先验证失败路径，再宣称完成。"), candidate_id="candidate-lesson-perm",
                        candidate_type="lesson", promotion_target="lesson", subject_key="testing.permissions")
    record = repository.promote(candidate)
    lesson_file = tmp_path / record.path
    assert (_mode(lesson_file), _mode(lessons_dir), _mode(index), _mode(index.parent)) == (0o600, 0o700, 0o600, 0o700)

    _loosen(index)
    repository.rebuild_routing_index()

    assert (_mode(index), _mode(index.parent)) == (0o600, 0o755), "索引文件被收紧；已存在的目录权限不动"


@_POSIX_ONLY
def test_hot_rules_are_tightened_on_promote_and_on_demotion_rewrite(tmp_path, open_umask):
    path = tmp_path / "memory" / "memory-hot.md"
    repository = HotRuleRepository(path)
    path.write_text(_render_hot([_hot(_NOW, index=1, rule_len=60)]), encoding="utf-8")
    _loosen(path)
    repository.promote(_hot_candidate(content="提交前必须跑一次全量测试。"), lesson=_lesson())
    assert (_mode(path), _mode(path.parent)) == (0o600, 0o755), "HOT 文件被收紧；已存在的目录权限不动"

    path.write_text(_render_hot(_hot_pile()), encoding="utf-8")
    _loosen(path)
    assert repository.demote_cold_rules(), "超预算才会走降级重写"

    assert (_mode(path), _mode(path.parent)) == (0o600, 0o755), "HOT 文件被收紧；已存在的目录权限不动"


@_POSIX_ONLY
def test_curator_backups_targets_and_rollback_restore_are_private(tmp_path, open_umask):
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    crashed = _service(tmp_path, _StaticStructuredBackend(output), store)
    candidates = crashed.candidate_service.path
    candidates.parent.mkdir(parents=True, exist_ok=True)
    candidates.write_text("", encoding="utf-8")
    _loosen(candidates)  # 生产旧状态：事务开始前就有 0644 的候选文件
    write_target = crashed.committer._write_target

    def crash_after_candidates(path: Path, content: str) -> None:
        write_target(path, content)
        if path == candidates:
            raise SystemExit("simulated process stop")

    crashed.committer._write_target = crash_after_candidates
    with pytest.raises(SystemExit, match="simulated process stop"):
        crashed.run(reason="admin")

    assert _mode(candidates) == 0o600, "事务目标替换后必须收紧"
    [transaction] = [path for path in crashed.committer.transactions_dir.iterdir() if path.is_dir()]
    backups = sorted(transaction.glob("before-*.txt"))
    assert backups and {_mode(path) for path in backups} == {0o600} and _mode(transaction) == 0o700, "前镜像是正文副本"

    _loosen(candidates)
    restarted = _service(tmp_path, _StaticStructuredBackend(output), store)
    assert restarted.committer.recover_incomplete(now=datetime.now(timezone.utc) + timedelta(hours=1)) == 1

    assert candidates.read_text(encoding="utf-8") == "" and _mode(candidates) == 0o600, "回滚写回不带回 0644"


@_POSIX_ONLY
def test_migration_backup_copies_are_private_with_original_bytes_and_mtime(tmp_path, open_umask):
    home, _candidates, _long_term, _daily, _lessons, migration = _migration_runtime(tmp_path)
    draft = home.owner_data_dir / "learning_drafts" / "draft.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"content": "旧草稿正文。"}, ensure_ascii=False), encoding="utf-8")
    legacy_lesson = home.owner_memory_lessons_dir / "legacy.md"
    legacy_lesson.write_text("# 旧 lesson\n\n正文。\n", encoding="utf-8")
    for path in (draft, legacy_lesson):
        os.chmod(path, 0o644)  # 生产旧状态：旧记忆文件 0644
    originals = {path.resolve(): (path.read_bytes(), path.stat().st_mtime_ns) for path in (draft, legacy_lesson)}

    report = migration.apply()

    assert report.applied is True
    backup_dir = Path(report.backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    copies = {Path(item["source"]): Path(item["backup"]) for item in manifest["files"] if item["existed"]}
    assert copies and {_mode(path) for path in copies.values()} == {0o600}, "备份副本一律 0600，不抄旧文件的 0644"
    directories = [backup_dir, *(path for path in backup_dir.rglob("*") if path.is_dir())]
    assert {_mode(path) for path in directories} == {0o700}, "备份目录每一级都是 0700"
    checked = [(copies[source].read_bytes(), copies[source].stat().st_mtime_ns) == original
               for source, original in originals.items()]
    assert checked == [True, True], "副本保留原字节和修改时间，回滚才能原样拷回"
