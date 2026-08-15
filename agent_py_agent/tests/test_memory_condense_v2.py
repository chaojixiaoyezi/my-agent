from __future__ import annotations

"""P2 long_term 访问信号(touch 事件)与总量闸(condense 浓缩)的确定性测试。

覆盖:召回命中写 last_accessed_at、浓缩只淘汰非 user_explicit 冷条目、
archive 账本、单次 ≤25% 上限、promotion 写路径自动触发浓缩。
"""

from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord


def _memory(tmp_path: Path) -> JsonlMemory:
    return JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")


def _fact(memory: JsonlMemory, entry_id: str, content: str, *, origin: str) -> MemoryRecord:
    return memory.add_record(
        MemoryRecord(
            role="user" if origin == "user_explicit" else "system",
            content=content,
            kind="fact",
            entry_id=entry_id,
            attributes={"origin": origin},
        )
    )


def test_touch_updates_last_accessed_for_hits_only(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _fact(memory, "memory-a", "祥子买了两次车。", origin="user_explicit")
    _fact(memory, "memory-c", "与查询无关的测试记忆。", origin="model_inferred")

    hits = memory.search_scoped("祥子买车", top_k=5, predicate=lambda record: True)
    assert [record.entry_id for record in hits] == ["memory-a"]
    memory.flush_access_events()

    by_id = {record.entry_id: record for record in memory.all()}
    assert by_id["memory-a"].last_accessed_at > 0.0, "命中条目必须记录访问信号"
    assert by_id["memory-c"].last_accessed_at == 0.0, "未命中条目不得 touch"
    # 内容/版本不受 touch 影响。
    assert by_id["memory-a"].version == 1
    assert by_id["memory-a"].content == "祥子买了两次车。"


def test_touch_survives_reload_from_disk(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _fact(memory, "memory-a", "祥子买了两次车。", origin="user_explicit")
    memory.search_scoped("祥子买车", top_k=5, predicate=lambda record: True)
    memory.flush_access_events()

    reloaded = JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")
    by_id = {record.entry_id: record for record in reloaded.all()}
    assert by_id["memory-a"].last_accessed_at > 0.0


def test_condense_removes_coldest_non_user_explicit_only(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _fact(memory, "memory-a", "事实一。", origin="user_explicit")
    _fact(memory, "memory-b", "事实二。", origin="user_explicit")
    _fact(memory, "memory-c", "推断记忆。", origin="model_inferred")
    _fact(memory, "memory-d", "工具记忆。", origin="tool_verified")

    removed = memory.condense(max_records=3, target_records=2)
    assert [record.entry_id for record in removed] == ["memory-c"]
    assert [record.entry_id for record in memory.all()] == ["memory-a", "memory-b", "memory-d"]
    # user_explicit 永不因用量淘汰。
    assert all(record.entry_id != "memory-a" for record in removed)


def test_condense_archives_removed_records(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _fact(memory, "memory-a", "事实一。", origin="user_explicit")
    _fact(memory, "memory-c", "待淘汰正文。", origin="model_inferred")

    memory.condense(max_records=1, target_records=1)
    archive = tmp_path / "memory.jsonl.archive.jsonl"
    assert archive.exists()
    assert "待淘汰正文" in archive.read_text(encoding="utf-8")


def test_condense_noop_within_limit(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _fact(memory, "memory-a", "事实一。", origin="user_explicit")
    _fact(memory, "memory-c", "推断记忆。", origin="model_inferred")

    assert memory.condense(max_records=5, target_records=2) == []
    assert len(memory.all()) == 2
    assert not (tmp_path / "memory.jsonl.archive.jsonl").exists()


def test_condense_caps_removal_at_ratio(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    for index in range(10):
        _fact(
            memory,
            f"memory-{index:02d}",
            f"推断记忆第{index}条。",
            origin="model_inferred",
        )
    removed = memory.condense(
        max_records=5,
        target_records=0,
        max_ratio=0.25,  # 10 条 → 单次最多移除 2 条
    )
    assert len(removed) == 2
    assert len(memory.all()) == 8


def test_condense_empty_and_all_user_explicit_noop(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    assert memory.condense() == []
    _fact(memory, "memory-a", "事实一。", origin="user_explicit")
    # 全是 user_explicit → 无可淘汰,不动。
    assert memory.condense(max_records=0, target_records=0) == []
    assert len(memory.all()) == 1


def test_promote_triggers_condense_on_long_term_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总量闸挂在 promote 成功路径:long_term 写成功即触发 condense(机制保证,无需 cron)。"""
    import hashlib

    from agent_py_agent.agent.capability.persona_repository import PersonaRepository
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.memory_store.candidate_models import (
        CandidateObservation,
        MemoryScope,
    )
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.lessons import (
        HotRuleRepository,
        LessonRepository,
    )
    from agent_py_agent.agent.memory_store.promotion import (
        ConversationMessageEvidenceVerifier,
        MemoryPromotionDependencies,
        MemoryPromotionService,
    )

    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    thread = conversations.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    long_term = JsonlMemory(owner / "memory" / "long_term" / "memory.jsonl")
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=long_term,
            persona=PersonaRepository(
                owner_home=owner,
                soul_path=owner / "SOUL.md",
                user_path=owner / "USER.md",
                agents_path=owner / "AGENTS.md",
            ),
            lessons=LessonRepository(owner / "memory" / "lessons", owner / "routing" / "INDEX.md"),
            hot=HotRuleRepository(owner / "memory-hot.md"),
            message_verifier=ConversationMessageEvidenceVerifier(conversations),
        ),
    )
    message = conversations.append_message(
        {"thread_id": thread.thread_id, "role": "user", "content": "祥子买了两次车。", "now": 11.0}
    )
    content_hash = "sha256:" + hashlib.sha256("祥子买了两次车。".encode()).hexdigest()
    message_ref = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": content_hash,
        "quote": "祥子买了两次车。",
    }
    candidate = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="祥子买了两次车。",
            subject_key="xiangzi.car",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(message_ref,),
            proposed_action="add",
            promotion_target="long_term",
            confidence=0.99,
            # 权限合同:自动晋升路径需要宿主已授权 auto_eligible 的候选。
            promotion_mode="auto_eligible",
        )
    )

    calls: list[dict[str, object]] = []
    original = JsonlMemory.condense

    def _spy_condense(self, **kwargs):
        calls.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(JsonlMemory, "condense", _spy_condense)
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted
    assert calls, "long_term 晋升成功后必须触发 condense"
