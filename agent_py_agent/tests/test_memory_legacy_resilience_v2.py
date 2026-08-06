"""真机回归:升级后 legacy 记忆再也不能瘫痪 curator 提炼。

真机根因(testbox, 2026-08-05):升级到 v2 后,旧版纯 Markdown lesson 没有 v1 元数据 marker,
lessons.list() 抛 ValueError → FormalMemorySource.read 崩 → 每次 curator run 都是
CURATOR_FORMAL_MEMORY_READ_FAILED → 记忆自主提炼完全瘫痪。

三件套机制级修复(升级/部署/迁移永不因旧数据瘫痪):
- 修复A: lessons.list() 逐文件容错,legacy/损坏单文件只跳过不拖垮整表。
- 修复B: curator 正式记忆读取失败降级为空并记入可降级错误,不阻断主链提炼。
- 修复C: curator 每次持 lease 执行前自动应用 v2 迁移(幂等),失败只记 warning 不阻断。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import (
    HotRuleRepository,
    LessonRepository,
)
from agent_py_agent.agent.memory_store.migration import MemoryMigrationService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


# ---------- 辅助(与既有 curator/migration 测试同一构造约定) ----------


class _StaticStructuredBackend:
    name = "fake-structured"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        assert "tools" not in response_schema
        return ModelResponse(
            text=json.dumps(self.payload, ensure_ascii=False),
            backend=self.name,
        )


def _conversation(store: ConversationStore, now: float = 10.0):
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": now,
        }
    )
    message = store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "我的个人电脑使用 macOS。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-1",
                "request_id": "request-1",
                "task_id": "task-1",
                "run_id": "run-1",
            },
            "now": now + 1.0,
        }
    )
    return thread, message


def _valid_output(thread_id: str, message_id: str) -> dict[str, object]:
    ref = {"message_id": message_id}
    return {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [
            {
                "event_type": "conversation",
                "summary": "用户说明个人电脑使用 macOS。",
                "actor": "user",
                "origin": "user_explicit",
                "message_refs": [ref],
                "tool_refs": [],
                "artifact_refs": [],
                "decisions": [],
                "lessons": [],
                "next_actions": [],
            }
        ],
        "candidates": [
            {
                "candidate_type": "long_term_fact",
                "content": "个人电脑使用 macOS。",
                "subject_key": "device.personal.os",
                "scope": {
                    "scope_type": "personal",
                    "scope_key": "personal",
                    "applies_when": "个人电脑",
                    "excludes_when": "公司服务器",
                },
                "origin": "user_explicit",
                "source_message_refs": [ref],
                "source_tool_refs": [],
                "source_artifact_refs": [],
                "observed_at": "1970-01-01T00:00:11+00:00",
                "valid_from": None,
                "valid_until": None,
                "confidence": 0.99,
                "proposed_action": "add",
                "target_entry_id": None,
                "conflicts_with": [],
                "promotion_target": "long_term",
            }
        ],
        "processed_message_refs": [{"message_id": message_id}],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {
            "per_thread_cursors": [{"thread_id": thread_id, "message_id": message_id}],
            "last_audit_event_id": None,
        },
    }


def _curator_service(
    tmp_path: Path,
    backend: object,
    *,
    store: ConversationStore,
    formal_memory_source: object | None = None,
    migration_service: object | None = None,
):
    return MemoryCuratorService(
        config=MemoryCuratorConfig(
            interval_seconds=60,
            turn_threshold=1,
            timeout_seconds=2,
            max_retries=0,
        ),
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=store,
            audit_dir=tmp_path / "audit",
            state_store=MemoryCuratorStateStore(
                tmp_path / "memory" / "curator" / "state.json"
            ),
            daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
            candidate_service=CandidateService(
                tmp_path / "memory" / "candidates.jsonl"
            ),
            run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(
                provider="fake-structured",
                model="curator-test",
            ),
            formal_memory_source=formal_memory_source,
            migration_service=migration_service,
        ),
    )


def _legacy_home(tmp_path: Path) -> tuple[object, CandidateService, JsonlMemory, DailyMemoryStore, LessonRepository, MemoryMigrationService, ConversationStore]:
    """构造一个带 legacy lesson 的完整 owner home(升级前数据形状)。"""
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
        legacy_workspace_roots=(),
    )
    store = ConversationStore(home.root / "conversations")
    # 升级前的 legacy lesson:纯 Markdown,无 v1 元数据 marker。
    (home.owner_memory_lessons_dir / "legacy.md").write_text(
        "# 旧教训\n\n详细正文。\n",
        encoding="utf-8",
    )
    return home, candidates, long_term, daily, lessons, migration, store


# ---------- 修复A:读路径按迁移状态容错/严格 ----------


def test_lessons_list_skips_legacy_until_migrated(tmp_path: Path):
    home, _candidates, _long_term, _daily, lessons, _migration, _store = _legacy_home(
        tmp_path
    )
    # 正式 lesson 与 legacy(无 marker)文件混放;migration.json 不存在(升级前/全新)。
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    observed = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="共享状态修改必须使用 CAS。",
            subject_key="concurrency.cas",
            scope=MemoryScope("task_class", "task_class:engineering"),
            origin="user_explicit",
            source_message_refs=({"message_id": "message-1"},),
            source_task_ids=("task-1",),
            promotion_target="lesson",
            observation_id="lesson-formal-1",
        )
    )
    approved = candidates.transition(
        observed.candidate_id,
        "approved",
        reviewer="admin",
        review_note="已核验。",
    )
    formal = lessons.promote(approved, min_occurrences=1)

    # 修前:任何一个 legacy 文件都让整表抛 ValueError → 正式 lesson 也读不出来。
    records = lessons.list()

    assert [record.lesson_id for record in records] == [formal.lesson_id]
    assert lessons.get(formal.lesson_id).content  # get 基于 list,同样不崩


def test_lessons_list_fails_closed_after_migration_marker(tmp_path: Path):
    # 迁移已 complete(正式 v2 区建立)后再出现无 marker 文件 = 损坏,必须 fail-closed
    # 不可静默吞——防模型拿到被破坏的正式记忆(与 push 集成测试同一约定)。
    home, _candidates, _long_term, _daily, lessons, _migration, _store = _legacy_home(
        tmp_path
    )
    (home.owner_memory_dir / "migration.json").write_text(
        json.dumps(
            {
                "schema_version": "my-agent.memory-migration.v2",
                "status": "complete",
                "run_id": "test",
            }
        ),
        encoding="utf-8",
    )
    (home.owner_memory_lessons_dir / "orphan.md").write_text(
        "# 损坏正式 lesson\n\n正文。\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="legacy lesson requires migration"):
        lessons.list()


# ---------- 修复B:formal 读取降级不阻断主链 ----------


class _FailingFormalSource:
    def read(self, *, max_items: int, max_chars: int):
        del max_items, max_chars
        raise RuntimeError("probe: formal memory unavailable")


def test_curator_formal_read_failure_degrades_but_commits(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread, message = _conversation(store)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id))
    service = _curator_service(
        tmp_path,
        backend,
        store=store,
        formal_memory_source=_FailingFormalSource(),
    )

    # 修前:CURATOR_FORMAL_MEMORY_READ_FAILED → 每次 run 失败,提炼完全瘫痪。
    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.candidates == 1  # 主链提炼照常落账
    assert any(
        "formal_memory_read_skipped" in warning for warning in result.warnings
    )
    assert service.candidate_service.list()[0].content == "个人电脑使用 macOS。"


# ---------- 修复C:preflight 自动迁移 ----------


def test_curator_preflight_auto_migrates_legacy_lessons(tmp_path: Path):
    home, candidates, long_term, daily, lessons, migration, store = _legacy_home(
        tmp_path
    )
    store2 = ConversationStore(home.root / "conversations")
    thread, message = _conversation(store2)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id))
    formal = FormalMemorySource(
        long_term,
        lessons,
        HotRuleRepository(home.owner_memory_hot_md),
    )
    service = _curator_service(
        tmp_path,
        backend,
        store=store2,
        formal_memory_source=formal,
        migration_service=migration,
    )
    legacy_path = home.owner_memory_lessons_dir / "legacy.md"

    result = service.run(reason="admin")

    # 迁移执行:legacy 文件被移出正式区,内容转成候选;提炼照常成功。
    assert result.status == "succeeded"
    assert not legacy_path.exists()
    assert any(
        "memory_migration_applied" in warning for warning in result.warnings
    )
    migrated = [item for item in candidates.list() if "旧教训" in item.content]
    assert migrated  # legacy 内容进了候选账本,没有丢
    # 迁移后 formal 读取路径恢复(第二跑 preflight already_current、formal 读成功)。
    again = service.run(reason="admin")
    assert again.status == "succeeded"
    assert not any(
        "memory_migration_applied" in warning for warning in again.warnings
    )


def test_curator_preflight_migration_failure_does_not_block(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread, message = _conversation(store)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id))

    class _FailingMigration:
        def apply(self):
            raise OSError("probe: backup disk unavailable")

    service = _curator_service(
        tmp_path,
        backend,
        store=store,
        migration_service=_FailingMigration(),
    )

    result = service.run(reason="admin")

    assert result.status == "succeeded"  # 迁移失败不阻断提炼
    assert result.candidates == 1
    assert any("memory_migration_failed" in warning for warning in result.warnings)


def test_curator_without_migration_service_runs_unchanged(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread, message = _conversation(store)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id))
    service = _curator_service(tmp_path, backend, store=store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert not any(
        "migration" in warning for warning in result.warnings
    )
