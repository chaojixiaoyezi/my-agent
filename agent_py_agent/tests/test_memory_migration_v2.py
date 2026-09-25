from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.migration import MemoryMigrationService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


def _runtime(tmp_path: Path, *, legacy_workspaces: tuple[Path, ...] = ()):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    long_term = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
    )
    daily = DailyMemoryStore(home.owner_memory_daily_dir)
    lessons = LessonRepository(
        home.owner_memory_lessons_dir,
        home.owner_memory_routing_index_md,
    )
    migration = MemoryMigrationService(
        home_paths=home,
        candidates=candidates,
        long_term=long_term,
        daily=daily,
        lessons=lessons,
        legacy_workspace_roots=legacy_workspaces,
    )
    return home, candidates, long_term, daily, lessons, migration


def _write_jsonl(path: Path, *rows: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _file_state(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _error_codes(report) -> set[str]:
    return {error.error_code for error in report.errors}


def _promote_formal_lesson_and_hot(
    *,
    candidates: CandidateService,
    lessons: LessonRepository,
    hot_path: Path,
):
    lesson_observation = CandidateObservation(
        candidate_type="lesson",
        content="共享状态修改前必须使用精确版本进行比较交换。",
        subject_key="concurrency.cas",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="subagent_lesson",
        source_task_ids=("task-1",),
        promotion_target="lesson",
        observation_id="lesson-task-1",
    )
    lesson_candidate = candidates.observe(lesson_observation)
    candidates.observe(
        CandidateObservation(
            **{
                **lesson_observation.__dict__,
                "source_task_ids": ("task-2",),
                "observation_id": "lesson-task-2",
            }
        )
    )
    approved_lesson = candidates.transition(
        lesson_candidate.candidate_id,
        "approved",
        reviewer="admin",
        review_note="已核验。",
    )
    lesson = lessons.promote(approved_lesson)

    hot_observation = CandidateObservation(
        candidate_type="hot_rule",
        content="共享状态修改必须使用精确版本 CAS。",
        subject_key="hot.concurrency.cas",
        scope=MemoryScope("task_class", "task_class:engineering"),
        origin="reviewed",
        source_task_ids=("task-1",),
        promotion_target="hot",
        target_entry_id=lesson.lesson_id,
        observation_id="hot-task-1",
    )
    hot_candidate = candidates.observe(hot_observation)
    for task_id in ("task-2", "task-3"):
        candidates.observe(
            CandidateObservation(
                **{
                    **hot_observation.__dict__,
                    "source_task_ids": (task_id,),
                    "observation_id": f"hot-{task_id}",
                }
            )
        )
    approved_hot = candidates.transition(
        hot_candidate.candidate_id,
        "approved",
        reviewer="admin",
        review_note="已核验。",
    )
    hot = HotRuleRepository(hot_path).promote(approved_hot, lesson=lesson)
    return lesson, hot


def test_migration_dry_run_detects_all_legacy_sources_without_writes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home, _candidates, long_term, daily, _lessons, migration = _runtime(
        tmp_path,
        legacy_workspaces=(workspace,),
    )
    gate = home.owner_tasks_dir / "2026-08-04" / "task-a" / "work" / "memory_gate"
    _write_jsonl(gate / "candidates.jsonl", {"content": "子代理旧发现。"})
    draft = workspace / "data" / "learning_drafts" / "draft.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"content": "旧学习草稿。"}), encoding="utf-8")
    _write_jsonl(home.owner_memory_ops_jsonl, {"event": "candidate", "content": "旧 ops 候选。"})
    committed = daily.append(
        DailyMemoryEvent(
            event_type="decision",
            summary="保留的 v2 Daily。",
            actor="main_agent",
            created_at="2026-08-04T01:00:00+00:00",
        )
    )
    daily_path = home.owner_memory_daily_dir / "2026-08-04.jsonl"
    with daily_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"role": "user", "content": "旧 daily 镜像。"}) + "\n")
    long_term.add_record(
        MemoryRecord(
            role="user",
            content="旧完整对话。",
            kind="dialogue",
            created_at=datetime.fromisoformat(
                "2026-08-04T03:00:00+00:00"
            ).timestamp(),
        )
    )
    long_term.add("system", "旧 lesson 正文。", kind="lesson")
    (home.owner_memory_lessons_dir / "legacy.md").write_text("# 旧教训\n\n详细正文。\n", encoding="utf-8")
    home.owner_memory_md.write_text("# 旧 Memory\n\n重复事实。\n", encoding="utf-8")
    home.owner_memory_hot_md.write_text("# 旧 HOT\n\n重复规则。\n", encoding="utf-8")
    conversation_sentinel = home.root / "conversations" / "sentinel.jsonl"
    _write_jsonl(conversation_sentinel, {"message_id": "message-1", "content": "历史原话"})
    observed_paths = [
        gate / "candidates.jsonl",
        draft,
        home.owner_memory_ops_jsonl,
        daily_path,
        home.owner_memory_long_term_jsonl,
        home.owner_memory_lessons_dir / "legacy.md",
        home.owner_memory_md,
        home.owner_memory_hot_md,
        conversation_sentinel,
    ]
    before = _file_state(observed_paths)

    report = migration.plan()

    assert report.ok is True
    assert report.applied is False
    assert {finding.category for finding in report.findings} == {
        "task_memory_gate",
        "learning_drafts",
        "ops_candidates",
        "legacy_daily_mirror",
        "legacy_long_term",
        "legacy_lesson_markdown",
        "legacy_memory_md",
        "legacy_memory_hot",
    }
    assert _file_state(observed_paths) == before
    assert committed.event_id in daily_path.read_text(encoding="utf-8")
    assert not (home.owner_memory_dir / "migration.json").exists()
    assert not any((home.system_backups_dir / "memory-migration").glob("*/manifest.json"))


# LLM: 新 owner 刚初始化的 memory.md / memory-hot.md（根级模板副本或正式默认导航）不是旧正文，不能变成待审候选；
#   只有与两种精确来源都不同的内容才是 legacy。
# 函数用途: 验证种子文件按精确来源判定为 current，改成自定义内容后才成为 legacy。
def test_freshly_seeded_navigation_files_are_not_legacy(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout_v2 import owner_navigation_seeds
    from agent_py_agent.agent.user_space.home_memory_seeds import (
        default_memory_hot_md,
        default_memory_md,
    )

    home, *_rest, migration = _runtime(tmp_path)
    seeded_memory, seeded_hot = owner_navigation_seeds(home)
    assert seeded_memory.strip() and seeded_hot.strip()
    home.owner_memory_md.write_text(seeded_memory, encoding="utf-8")
    home.owner_memory_hot_md.write_text(seeded_hot, encoding="utf-8")
    assert not {"legacy_memory_md", "legacy_memory_hot"} & {f.category for f in migration.plan().findings}, "种子内容是 current"

    home.memory_md.write_text("# 管理员导航模板\n\n- 团队约定入口\n", encoding="utf-8")
    home.owner_memory_md.write_text(home.memory_md.read_text(encoding="utf-8"), encoding="utf-8")
    assert "legacy_memory_md" not in {f.category for f in migration.plan().findings}, "根级模板副本也是 current"
    home.owner_memory_md.write_text(default_memory_md(), encoding="utf-8")
    home.owner_memory_hot_md.write_text(default_memory_hot_md(), encoding="utf-8")
    assert not {"legacy_memory_md", "legacy_memory_hot"} & {f.category for f in migration.plan().findings}, "正式默认导航是 current"

    home.owner_memory_md.write_text("# 旧 Memory\n\n真正的旧正文。\n", encoding="utf-8")
    assert "legacy_memory_md" in {f.category for f in migration.plan().findings}, "与两种精确来源都不同才是 legacy"


def test_migration_apply_backs_up_preserves_mixed_daily_chunks_and_is_idempotent(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    home, candidates, long_term, daily, _lessons, migration = _runtime(
        tmp_path,
        legacy_workspaces=(workspace,),
    )
    learning = workspace / "data" / "learning_drafts"
    learning.mkdir(parents=True)
    long_text = "迁移长正文" * 1_100
    (learning / "draft.json").write_text(
        json.dumps({"content": long_text}, ensure_ascii=False),
        encoding="utf-8",
    )
    (workspace / "data" / "keep.txt").write_text("保留", encoding="utf-8")
    gate = home.owner_tasks_dir / "2026-08-04" / "task-a" / "work" / "memory_gate"
    _write_jsonl(gate / "exports.jsonl", {"lesson": "旧 memory_gate 教训。"})
    _write_jsonl(
        home.owner_memory_ops_jsonl,
        {"event": "candidate", "content": "旧 ops 候选正文。", "source": "legacy"},
    )
    existing = daily.append(
        DailyMemoryEvent(
            event_type="decision",
            summary="已有 v2 事件。",
            actor="main_agent",
            created_at="2026-08-04T01:00:00+00:00",
        )
    )
    daily_path = home.owner_memory_daily_dir / "2026-08-04.jsonl"
    with daily_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "role": "tool",
                    "content": "旧工具摘要。",
                    "status": "success",
                    "created_at": "2026-08-04T02:00:00+00:00",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    long_term.add("system", "旧长期事实。", kind="fact")
    long_term.add_record(
        MemoryRecord(
            role="user",
            content="旧完整对话。",
            kind="dialogue",
            created_at=datetime.fromisoformat(
                "2026-08-04T03:00:00+00:00"
            ).timestamp(),
        )
    )
    conversation_sentinel = home.root / "conversations" / "sentinel.jsonl"
    _write_jsonl(conversation_sentinel, {"message_id": "message-1", "content": "历史原话"})
    conversation_before = conversation_sentinel.read_bytes()

    first = migration.apply()

    assert first.ok is True
    assert first.applied is True
    backup_dir = Path(first.backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "my-agent.memory-migration.v2"
    assert any(item["source"] == str(learning / "draft.json") for item in manifest["files"])
    assert not learning.exists()
    assert workspace.exists()
    assert (workspace / "data" / "keep.txt").read_text(encoding="utf-8") == "保留"
    assert not gate.exists()
    assert conversation_sentinel.read_bytes() == conversation_before

    migrated = candidates.list()
    long_chunks = [item for item in migrated if "迁移长正文" in item.content]
    assert len(long_chunks) >= 3
    assert all(len(item.content) <= 2_000 for item in long_chunks)
    assert all(item.source_artifact_refs for item in long_chunks)
    assert "".join(item.content for item in long_chunks) == long_text
    assert any(item.content == "旧长期事实。" for item in migrated)
    assert long_term.all_including_expired() == []
    ops_text = home.owner_memory_ops_jsonl.read_text(encoding="utf-8")
    assert "旧 ops 候选正文" not in ops_text
    assert "content_hash" in ops_text

    events = daily.list(day="2026-08-04")
    assert events[0].event_id == existing.event_id
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert any(event.summary == "旧工具摘要。" for event in events)
    assert any(event.summary == "旧完整对话。" for event in events)
    candidate_count = len(migrated)
    event_ids = [event.event_id for event in events]

    second = migration.apply()

    assert second.ok is True
    assert second.applied is False
    assert second.already_current is True
    assert len(candidates.list()) == candidate_count
    assert [event.event_id for event in daily.list(day="2026-08-04")] == event_ids


def test_expired_legacy_long_term_is_still_migrated(tmp_path: Path):
    _home, candidates, long_term, _daily, _lessons, migration = _runtime(tmp_path)
    long_term.add_record(
        MemoryRecord(
            role="system",
            content="已过期但仍需安全迁移的旧事实。",
            kind="fact",
            expires_at=1.0,
        )
    )
    assert long_term.all() == []

    report = migration.apply()

    assert report.ok is True
    assert any(item.content == "已过期但仍需安全迁移的旧事实。" for item in candidates.list())
    assert long_term.all_including_expired() == []


def test_formal_lesson_and_hot_are_preserved_verbatim(tmp_path: Path):
    home, candidates, _long_term, _daily, lessons, migration = _runtime(tmp_path)
    lesson, hot = _promote_formal_lesson_and_hot(
        candidates=candidates,
        lessons=lessons,
        hot_path=home.owner_memory_hot_md,
    )
    lesson_path = home.owner_memory_lessons_dir / Path(lesson.path).name
    before = _file_state([lesson_path, home.owner_memory_hot_md, home.owner_memory_routing_index_md])

    plan = migration.plan()
    applied = migration.apply()

    assert plan.ok is True
    assert not {"legacy_lesson_markdown", "legacy_memory_hot"} & {
        finding.category for finding in plan.findings
    }
    assert applied.ok is True
    assert _file_state(list(before)) == before
    assert lessons.get(lesson.lesson_id).content
    assert HotRuleRepository(home.owner_memory_hot_md).list()[0].hot_id == hot.hot_id


@pytest.mark.parametrize(
    ("target", "payload", "expected_code"),
    [
        ("ops", "{bad json\n", "MEMORY_MIGRATION_CORRUPT_OPS"),
        ("daily", "{bad json\n", "MEMORY_MIGRATION_CORRUPT_DAILY"),
        ("long_term", "{bad json\n", "MEMORY_MIGRATION_CORRUPT_LONG_TERM"),
        (
            "lesson",
            "<!-- my-agent-lesson-meta:{bad} -->\n\n正文\n",
            "MEMORY_MIGRATION_CORRUPT_LESSON_MARKER",
        ),
        (
            "hot",
            "# Memory HOT\n\n<!-- my-agent-hot-meta:{bad} -->\n- 坏规则\n",
            "MEMORY_MIGRATION_CORRUPT_HOT_MARKER",
        ),
        ("marker", "{bad json\n", "MEMORY_MIGRATION_CORRUPT_MARKER"),
        (
            "retention",
            "{bad json\n",
            "MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY",
        ),
    ],
)
def test_corrupt_legacy_or_formal_data_fails_closed(
    tmp_path: Path,
    target: str,
    payload: str,
    expected_code: str,
):
    home, candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    paths = {
        "ops": home.owner_memory_ops_jsonl,
        "daily": home.owner_memory_daily_dir / "2026-08-04.jsonl",
        "long_term": home.owner_memory_long_term_jsonl,
        "lesson": home.owner_memory_lessons_dir / "formal.md",
        "hot": home.owner_memory_hot_md,
        "marker": home.owner_memory_dir / "migration.json",
        "retention": home.owner_retention_json,
    }
    path = paths[target]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()

    plan = migration.plan()
    applied = migration.apply()

    assert expected_code in _error_codes(plan)
    assert applied.applied is False
    assert expected_code in _error_codes(applied)
    assert path.read_bytes() == before
    assert candidates.list() == []


def test_apply_failure_restores_every_touched_authority(tmp_path: Path, monkeypatch):
    home, _candidates, long_term, daily, lessons, migration = _runtime(tmp_path)
    learning = home.owner_data_dir / "learning_drafts"
    learning.mkdir(parents=True)
    draft = learning / "draft.json"
    draft.write_text(json.dumps({"content": "必须恢复的旧草稿。"}), encoding="utf-8")
    legacy_lesson = home.owner_memory_lessons_dir / "legacy.md"
    legacy_lesson.write_text("# 必须恢复的旧 lesson\n\n正文。\n", encoding="utf-8")
    home.owner_memory_md.write_text("# 必须恢复的旧导航\n", encoding="utf-8")
    home.owner_memory_hot_md.write_text("# 必须恢复的旧 HOT\n", encoding="utf-8")
    long_term.add("system", "必须恢复的旧长期事实。", kind="fact")
    daily_event = daily.append(
        DailyMemoryEvent(
            event_type="decision",
            summary="必须恢复的 v2 Daily。",
            actor="main_agent",
            created_at="2026-08-04T01:00:00+00:00",
        )
    )
    daily_path = home.owner_memory_daily_dir / "2026-08-04.jsonl"
    with daily_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"content": "必须恢复的旧 Daily。"}) + "\n")
    tracked = [
        draft,
        legacy_lesson,
        home.owner_memory_md,
        home.owner_memory_hot_md,
        home.owner_memory_routing_index_md,
        home.owner_memory_long_term_jsonl,
        home.owner_memory_ops_jsonl,
        daily_path,
        home.owner_memory_candidates_jsonl,
        home.owner_memory_dir / "migration.json",
    ]
    before = _file_state(tracked)

    def _fail_rebuild() -> Path:
        raise RuntimeError("injected routing write failure")

    monkeypatch.setattr(lessons, "rebuild_routing_index", _fail_rebuild)
    report = migration.apply()

    assert report.applied is False
    assert "MEMORY_MIGRATION_APPLY_FAILED" in _error_codes(report)
    assert "injected routing write failure" in report.errors[0].message
    assert _file_state(tracked) == before
    assert daily_event.event_id in daily_path.read_text(encoding="utf-8")
    assert Path(report.backup_dir, "manifest.json").is_file()


def test_source_change_after_backup_aborts_and_restores_original(
    tmp_path: Path,
    monkeypatch,
):
    home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    source = home.owner_data_dir / "learning_drafts" / "draft.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"content": "原始草稿。"}), encoding="utf-8")
    original_backup = migration._backup

    def _backup_then_change(snapshot, backup_dir):
        result = original_backup(snapshot, backup_dir)
        source.write_text(json.dumps({"content": "扫描后被修改。"}), encoding="utf-8")
        return result

    monkeypatch.setattr(migration, "_backup", _backup_then_change)
    report = migration.apply()

    assert report.applied is False
    assert "MEMORY_MIGRATION_APPLY_FAILED" in _error_codes(report)
    assert "MEMORY_MIGRATION_SOURCE_CHANGED" in report.errors[0].message
    assert json.loads(source.read_text(encoding="utf-8"))["content"] == "原始草稿。"
    assert not (home.owner_memory_dir / "migration.json").exists()


def test_legacy_directory_symlink_fails_closed(tmp_path: Path):
    home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "draft.json").write_text(json.dumps({"content": "不能跟随链接。"}), encoding="utf-8")
    link = home.owner_tasks_dir / "task" / "work" / "memory_gate"
    link.parent.mkdir(parents=True)
    os.symlink(external, link)

    report = migration.plan()

    assert "MEMORY_MIGRATION_LEGACY_SYMLINK" in _error_codes(report)
    assert external.is_dir()
    assert (external / "draft.json").is_file()


def test_external_learning_path_requires_explicit_exact_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    learning = workspace / "data" / "learning_drafts"
    learning.mkdir(parents=True)
    (learning / "draft.json").write_text(json.dumps({"content": "工作区旧草稿。"}), encoding="utf-8")
    (workspace / "keep.txt").write_text("保留", encoding="utf-8")
    _home, candidates, _long_term, _daily, _lessons, without_workspace = _runtime(
        tmp_path / "without"
    )
    assert "learning_drafts" not in {
        finding.category for finding in without_workspace.plan().findings
    }

    _home2, candidates2, _long_term2, _daily2, _lessons2, configured = _runtime(
        tmp_path / "configured",
        legacy_workspaces=(workspace,),
    )
    report = configured.apply()

    assert report.ok is True
    assert any(item.content == "工作区旧草稿。" for item in candidates2.list())
    assert candidates.list() == []
    assert not learning.exists()
    assert workspace.is_dir()
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "保留"


def test_filesystem_root_cannot_be_configured_as_legacy_workspace(tmp_path: Path):
    home, candidates, long_term, daily, lessons, _migration = _runtime(tmp_path)

    with pytest.raises(ValueError, match="filesystem root"):
        MemoryMigrationService(
            home_paths=home,
            candidates=candidates,
            long_term=long_term,
            daily=daily,
            lessons=lessons,
            legacy_workspace_roots=(Path(Path.cwd().anchor),),
        )


def test_legacy_retention_policy_is_backed_up_and_converted_once(tmp_path: Path):
    home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    legacy = {
        "schema_version": "retention.v1",
        "raw_days": 42,
        "daily_days": 120,
        "compact_days": 80,
        "task_completed_days": 70,
        "subagent_scratch_days": 15,
        "cache_days": 9,
        "tmp_days": 3,
        "trash_days": 11,
        "legal_hold": False,
        "legal_hold_task_ids": [],
        "maintenance_enabled": True,
        "maintenance_interval_seconds": 7_200,
    }
    home.owner_retention_json.write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )

    plan = migration.plan()
    applied = migration.apply()

    assert plan.ok is True
    assert "legacy_retention_policy" in {
        finding.category for finding in plan.findings
    }
    assert applied.ok is True
    current = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    assert current["schema_version"] == "my-agent.memory-retention.v2"
    assert current["audit_days"] == 42
    assert current["conversation_days"] == 365
    assert current["tool_output_days_after_terminal"] == 30
    assert current["completed_task_days"] == 70
    assert "raw_days" not in current
    assert "task_completed_days" not in current
    manifest = json.loads(
        Path(applied.backup_dir, "manifest.json").read_text(encoding="utf-8")
    )
    assert any(
        item["source"] == str(home.owner_retention_json)
        and item["existed"] is True
        for item in manifest["files"]
    )
    assert migration.apply().already_current is True


# LLM: 稳态短路是 CPU 修复，不是迁移语义变更：marker 已是本版本 complete 且契约位置无遗留目录时，
# 不允许为了"再确认一次"递归遍历整棵 owner home（真实 home 上是分钟级）。
# 函数用途: 验证第二次 apply 在读 marker 后直接返回，不再调用完整扫描。
def test_apply_skips_full_scan_when_marker_is_current_and_no_legacy_sources(
    tmp_path: Path, monkeypatch
):
    _home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    first = migration.apply()
    assert first.ok is True
    assert migration.marker_path.is_file()
    scans: list[str] = []
    original = migration._scan
    monkeypatch.setattr(
        type(migration),
        "_scan",
        lambda self: scans.append("scan") or original(),
    )
    second = migration.apply()
    assert second.ok is True
    assert second.already_current is True
    assert scans == []
    marker = json.loads(migration.marker_path.read_text(encoding="utf-8"))
    assert marker["status"] == "complete"
    assert marker["schema_version"] == "my-agent.memory-migration.v2"


# LLM: complete marker 之后重新出现的遗留目录必须仍然被迁移：契约位置探针命中即回落到完整扫描。
# 函数用途: 验证 tasks/<date>/<task>/work/memory_gate 形状的新遗留目录不会被稳态短路跳过。
def test_apply_rescans_when_legacy_dir_reappears_after_complete_marker(tmp_path: Path):
    home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    assert migration.apply().ok is True
    gate = home.owner_tasks_dir / "2026-09-12" / "task-late" / "work" / "memory_gate"
    _write_jsonl(gate / "candidates.jsonl", {"content": "complete 之后重新出现的旧发现。"})
    report = migration.apply()
    assert report.ok is True
    assert "task_memory_gate" in {finding.category for finding in report.findings}
    assert report.applied is True


# LLM: schema 版本不同的 marker 不构成稳态事实，必须照旧完整扫描。
# 函数用途: 验证 marker schema_version 不匹配时不会跳过扫描。
def test_apply_rescans_when_marker_schema_version_differs(tmp_path: Path, monkeypatch):
    _home, _candidates, _long_term, _daily, _lessons, migration = _runtime(tmp_path)
    assert migration.apply().ok is True
    marker = json.loads(migration.marker_path.read_text(encoding="utf-8"))
    marker["schema_version"] = "my-agent.memory-migration.v1"
    migration.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    scans: list[str] = []
    original = migration._scan
    monkeypatch.setattr(
        type(migration),
        "_scan",
        lambda self: scans.append("scan") or original(),
    )
    report = migration.apply()
    assert report.ok is True
    assert scans == ["scan"]
