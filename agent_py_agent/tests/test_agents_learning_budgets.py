from __future__ import annotations

"""B 路第 3 步：三层预算的行为测试（写入硬上限、注入子预算、索引上限）。

覆盖：
- 学习库写入硬帽（设计 §3.5.1）：写入后超帽且非净缩减 → 结构化拒绝、文件不变；
  已超帽文件允许受控净缩减；promotion 把拒绝转成结构化结果码。
- 工作约定注入子预算（设计 §3.5.2）：子预算截断约定条目、不挤占其它长期记忆、总预算仍生效。
- 可重建导航投影上限（设计 §3.5.3）：行数/字节双帽、最近更新优先、省略标注、lesson 正文不受影响。
"""

import hashlib
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _FORMAL_MEMORY_BUDGET_CHARS,
    _WORKING_AGREEMENT_SUB_BUDGET_CHARS,
    _budgeted_formal_memories,
)
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import (
    MEMORY_LIBRARY_WRITE_CAP_CHARS,
    JsonlMemory,
    MemoryLibraryWriteCapExceeded,
    MemoryRecord,
)
from agent_py_agent.agent.memory_store.lessons import (
    _ROUTING_INDEX_MAX_BYTES,
    _ROUTING_INDEX_MAX_LINE_COUNT,
    LESSON_SCHEMA_VERSION,
    HotRuleRepository,
    LessonRecord,
    LessonRepository,
    _render_lesson,
    _render_routing_index,
)
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate

# ---------------------------------------------------------------- 层 1：写入硬上限


def _memory(tmp_path: Path) -> JsonlMemory:
    return JsonlMemory(
        tmp_path / "memory" / "long_term" / "memory.jsonl",
        ops_path=tmp_path / "memory" / "ops.jsonl",
    )


def _raw_long_term_line(entry_id: str, content: str) -> str:
    return json.dumps(
        {
            "role": "user",
            "content": content,
            "kind": "fact",
            "entry_id": entry_id,
            "action": "add",
            "version": 1,
            "created_at": 1.0,
        },
        ensure_ascii=False,
    ) + "\n"


def test_long_term_write_cap_rejects_growth_and_keeps_file(tmp_path):
    memory = _memory(tmp_path)
    memory.add_record(MemoryRecord(role="user", content="甲" * 19000, kind="fact"))

    with pytest.raises(MemoryLibraryWriteCapExceeded) as excinfo:
        memory.add_record(MemoryRecord(role="user", content="乙" * 2000, kind="fact"))

    assert excinfo.value.code == "MEMORY_LIBRARY_WRITE_CAP_EXCEEDED"
    assert excinfo.value.cap_chars == MEMORY_LIBRARY_WRITE_CAP_CHARS
    assert excinfo.value.before_chars == 19000
    assert excinfo.value.after_chars == 21000
    # 拒绝发生在写文件之前：仓库仍只有第一条。
    assert [record.content for record in memory.all()] == ["甲" * 19000]


def test_long_term_write_cap_allows_net_shrink_on_already_over_cap_file(tmp_path):
    path = tmp_path / "memory" / "long_term" / "memory.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_raw_long_term_line("memory-over", "丙" * 21000), encoding="utf-8")
    memory = _memory(tmp_path)

    # 已超帽文件上继续增长：拒绝。
    with pytest.raises(MemoryLibraryWriteCapExceeded):
        memory.add_record(MemoryRecord(role="user", content="丁" * 100, kind="fact"))

    # 受控净缩减放行（清理才能进行）。
    memory.remove("memory-over")
    assert memory.all() == []


def _budget_service(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    thread = conversations.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    long_term_path = owner / "memory" / "long_term" / "memory.jsonl"
    long_term_path.parent.mkdir(parents=True, exist_ok=True)
    long_term_path.write_text(_raw_long_term_line("memory-filler", "填" * 19000), encoding="utf-8")
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=CandidateService(owner / "memory" / "candidates.jsonl"),
            long_term=JsonlMemory(long_term_path, ops_path=owner / "memory" / "ops.jsonl"),
            persona=PersonaRepository(
                owner_home=owner,
                soul_path=owner / "SOUL.md",
                user_path=owner / "USER.md",
                agents_path=owner / "AGENTS.md",
            ),
            lessons=LessonRepository(
                owner / "memory" / "lessons",
                owner / "memory" / "routing" / "INDEX.md",
            ),
            hot=HotRuleRepository(owner / "memory-hot.md"),
            message_verifier=ConversationMessageEvidenceVerifier(conversations),
            tool_verifier=LocalStoreToolEvidenceVerifier(owner, owner_id="user-1"),
        ),
    )
    return conversations, thread, service


def test_promotion_returns_structured_cap_code(tmp_path):
    conversations, thread, service = _budget_service(tmp_path)
    content = "约定：先读任务文件全文再动手。" + "补" * 1900
    message = conversations.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": content, "now": 11.0}
    )
    ref = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "quote": content[:80],
    }
    candidate = service.candidates.observe(
        CandidateObservation(
            candidate_type="working_agreement",
            content=content,
            subject_key="workflow.agents.dispatch",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            source_message_refs=(ref,),
            proposed_action="add",
            target_entry_id="",
            promotion_target="long_term",
            confidence=0.9,
            learning_source=dict(ls.user_dialogue_projection()),
        )
    )

    result = confirm_candidate(service, candidate.candidate_id)

    assert result.promoted is False
    assert result.reason_code == "MEMORY_LIBRARY_WRITE_CAP_EXCEEDED"
    # 候选保持原状态、不丢弃；仓库没有被写入第二条。
    assert service.candidates.get(candidate.candidate_id).status == "approved"
    assert [record.entry_id for record in service.long_term.all()] == ["memory-filler"]


# ---------------------------------------------------------------- 层 2：注入子预算


class _Row:
    def __init__(self, kind: str, content: str, entry_id: str) -> None:
        self.kind = kind
        self.content = content
        self.entry_id = entry_id


def test_working_agreement_sub_budget_caps_agreements_without_crowding_rest():
    per = _WORKING_AGREEMENT_SUB_BUDGET_CHARS // 2 + 100  # 单条在子预算内、两条超
    rows = [
        _Row("working_agreement", "约" * per, "wa-1"),
        _Row("working_agreement", "约" * per, "wa-2"),
        _Row("dialogue", "话" * 500, "rest-1"),
    ]

    kept = _budgeted_formal_memories(rows)
    ids = [row.entry_id for row in kept]

    assert "wa-1" in ids
    assert "wa-2" not in ids  # 子预算：第二条约定被截断
    assert "rest-1" in ids  # 其它长期记忆不被子预算挤占


def test_working_agreement_sub_budget_keeps_agreements_within_budget():
    per = _WORKING_AGREEMENT_SUB_BUDGET_CHARS // 2 - 100
    rows = [
        _Row("working_agreement", "约" * per, "wa-1"),
        _Row("working_agreement", "约" * per, "wa-2"),
    ]

    kept = _budgeted_formal_memories(rows)

    assert [row.entry_id for row in kept] == ["wa-1", "wa-2"]


def test_formal_memory_total_budget_still_caps_everything():
    hot_len = 2000
    rest_len = _FORMAL_MEMORY_BUDGET_CHARS - hot_len + 500  # 放不下
    rows = [
        _Row("hot", "热" * hot_len, "hot-1"),
        _Row("dialogue", "话" * rest_len, "rest-1"),
    ]

    kept = _budgeted_formal_memories(rows)

    assert [row.entry_id for row in kept] == ["hot-1"]


# ---------------------------------------------------------------- 层 3：索引上限


def _lesson(index: int, *, updated_at: str) -> LessonRecord:
    return LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id=f"lesson-{index:04d}",
        candidate_id=f"candidate-{index}",
        subject_key=f"demo.topic.{index}",
        scope={"scope_type": "personal", "scope_key": "personal"},
        content=f"第 {index} 条教训",
        occurrence_count=3,
        evidence_groups=("task:a", "task:b"),
        source_task_ids=("a",),
        source_run_ids=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=updated_at,
        path=f"memory/lessons/lesson-{index:04d}.md",
    )


def test_routing_index_bounded_and_recent_first():
    records = [
        _lesson(index, updated_at=f"2026-01-01T00:00:{index:02d}+00:00")
        for index in range(60)
    ]

    text = _render_routing_index(records)

    assert text.count("\n") <= _ROUTING_INDEX_MAX_LINE_COUNT
    assert len(text.encode("utf-8")) <= _ROUTING_INDEX_MAX_BYTES
    assert "已省略" in text
    assert "lesson-0059" in text  # 最近更新优先保留
    assert "lesson-0000" not in text  # 最旧的被省略


def test_routing_index_small_set_has_no_omission():
    records = [
        _lesson(index, updated_at="2026-01-01T00:00:00+00:00") for index in range(3)
    ]

    text = _render_routing_index(records)

    assert "已省略" not in text
    assert "lesson-0000" in text and "lesson-0002" in text


def test_rebuild_routing_index_end_to_end_applies_caps(tmp_path):
    repo = LessonRepository(
        tmp_path / "memory" / "lessons",
        tmp_path / "memory" / "routing" / "INDEX.md",
    )
    records = [
        _lesson(index, updated_at=f"2026-01-01T00:00:{index:02d}+00:00")
        for index in range(60)
    ]
    for record in records:
        (repo.lessons_dir / f"{record.lesson_id}.md").write_text(
            _render_lesson(record), encoding="utf-8"
        )

    repo.rebuild_routing_index()
    text = repo.routing_index_path.read_text(encoding="utf-8")

    assert text.count("\n") <= _ROUTING_INDEX_MAX_LINE_COUNT
    assert len(text.encode("utf-8")) <= _ROUTING_INDEX_MAX_BYTES
    assert "已省略" in text
    # 正文不受索引省略影响：被省略的 lesson 文件仍在。
    assert (repo.lessons_dir / "lesson-0000.md").exists()
