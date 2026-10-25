from __future__ import annotations

"""AGENTS.md 第 0 期：自动晋升硬拦与学习排除名单（2026-10-08 生产热修；设计 v2 更新）。

真机账：curator 的自动晋升两天向 AGENTS.md 追加 437 条（来源是开发派活会话，任务书被当成
用户长期约定）。第 0 期（v2）口径：memory promotion 的最终提交入口一律拒绝 agents——自动、
积压重试、人工晋升与审核后提交都算，被拒候选保持原状态并带结构化原因码；curator 的可晋升
判定同步排除 agents 目标；学习排除名单在收集消息前跳过名单会话、对既有待定候选写持久排除
标记且不自动晋升；名单不可读时 fail-closed。
"""

import hashlib
from pathlib import Path

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
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
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    collect_curator_inputs,
)
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import (
    MemoryCuratorState,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.learning_exclusions import (
    LEARNING_SOURCE_EXCLUDED,
    sync_learning_exclusion_marks,
)
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate

_EXCLUDED_CHAT = "chat-excluded-1"
_OTHER_CHAT = "chat-other-1"


# 函数用途: 在隔离 owner 下写基线人格文件。
def _seed_owner_files(owner: Path) -> None:
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")


# 函数用途: 建两个测试会话线程（名单会话与普通会话）。
def _seed_threads(conversations: ConversationStore):
    return {
        _EXCLUDED_CHAT: conversations.threads.get_or_create(
            {
                "canonical_user_id": "user-1",
                "channel": "internal",
                "channel_conversation_id": _EXCLUDED_CHAT,
                "channel_user_id": "user-1",
                "now": 10.0,
            }
        ),
        _OTHER_CHAT: conversations.threads.get_or_create(
            {
                "canonical_user_id": "user-2",
                "channel": "internal",
                "channel_conversation_id": _OTHER_CHAT,
                "channel_user_id": "user-2",
                "now": 10.0,
            }
        ),
    }


# LLM: 夹具与 test_memory_promotion_v2 同构：真实候选账本、Persona、对话存储与证据校验器。
# 函数用途: 构造一个隔离 HOME 下的完整晋升运行环境。
def _runtime(tmp_path: Path):
    owner = tmp_path / "owner"
    owner.mkdir()
    _seed_owner_files(owner)
    conversations = ConversationStore(tmp_path / "conversations")
    threads = _seed_threads(conversations)
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=JsonlMemory(
                owner / "memory" / "long_term" / "memory.jsonl",
                ops_path=owner / "memory" / "ops.jsonl",
            ),
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
            exclusion_store=conversations,
        ),
    )
    return service, conversations, threads


# 函数用途: 从指定会话观察一条 auto_eligible 候选（env 为 (service, conversations)）。
def _observe(env, thread, content, *, promotion_target="agents", **observation_fields):
    service, conversations = env
    message = conversations.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": content, "now": 11.0}
    )
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    message_ref = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": content_hash,
        "quote": content,
    }
    candidate_type = "working_agreement" if promotion_target == "agents" else "long_term_fact"
    subject_key = "persona.agents.rule" if promotion_target == "agents" else "device.os"
    return service.candidates.observe(
        CandidateObservation(
            candidate_type=candidate_type,
            content=content,
            subject_key=subject_key,
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(message_ref,),
            promotion_target=promotion_target,
            promotion_mode="auto_eligible",
            confidence=0.99,
            # 来源由隔离夹具模拟宿主用户入站，不凭候选正文判断。
            learning_source=ls.user_dialogue_projection(),
            **observation_fields,
        )
    )


# 函数用途: 构造一个只带候选/对话/名单依赖的 Curator 服务。
def _curator(tmp_path: Path, conversations, candidates, *, exclusions=None):
    memory = tmp_path / "owner" / "memory"
    return MemoryCuratorService(
        config=MemoryCuratorConfig(),
        dependencies=MemoryCuratorDependencies(
            backend=object(),
            conversation_store=conversations,
            audit_dir=tmp_path / "audit",
            state_store=MemoryCuratorStateStore(memory / "curator" / "state.json"),
            daily_store=DailyMemoryStore(memory / "daily"),
            candidate_service=candidates,
            run_log=CuratorRunLog(memory / "curator" / "runs"),
            identity=MemoryCuratorIdentity(provider="test", model="test"),
            learning_exclusions=exclusions,
        ),
    )


# 函数用途: 收集一次输入并返回消息的 thread_id 集合。
def _collected_threads(conversations, *, excluded):
    batch = collect_curator_inputs(
        message_source=CuratorMessageSource(
            conversation_store=conversations,
            excluded_conversation_ids=excluded,
        ),
        audit_dir=Path("/nonexistent-audit"),
        state=MemoryCuratorState(),
        config=MemoryCuratorConfig(batch_message_limit=80, max_input_chars=40_000),
    )
    return {item.thread_id for item in batch.messages}


def test_automatic_agents_promotion_is_forbidden_and_candidate_stays_pending(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe(
        (service, conversations), threads[_OTHER_CHAT], "长期合作时先核对真实代码。"
    )

    result = service.promote(candidate.candidate_id, automatic=True)

    assert result.promoted is False
    assert result.reason_code == "AUTO_AGENTS_WRITE_FORBIDDEN"
    assert service.candidates.get(candidate.candidate_id).status == "pending_review"
    assert "长期合作时先核对真实代码。" not in service.persona.path_for("agents").read_text(
        encoding="utf-8"
    )


def test_manual_agents_promotion_is_rejected(tmp_path: Path):
    # v2：人工晋升与审核后提交也不能再写 AGENTS；候选保持原状态（approved），不丢弃、不标失败。
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe((service, conversations), threads[_OTHER_CHAT], "人工复核的长期约定。")
    service.review(candidate.candidate_id, approved=True, reviewer="admin")

    result = service.promote(candidate.candidate_id, reviewer="admin")

    assert result.promoted is False
    assert result.reason_code == "AGENTS_WRITE_USER_ONLY"
    assert service.candidates.get(candidate.candidate_id).status == "approved"
    assert "人工复核的长期约定。" not in service.persona.path_for("agents").read_text(
        encoding="utf-8"
    )


def test_long_term_promotion_requires_explicit_confirmation(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe(
        (service, conversations),
        threads[_OTHER_CHAT],
        "个人电脑使用 macOS。",
        promotion_target="long_term",
    )

    result = confirm_candidate(service, candidate.candidate_id)

    assert result.promoted is True
    assert [record.content for record in service.long_term.all()] == ["个人电脑使用 macOS。"]


def test_excluded_conversation_messages_are_skipped(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    _observe((service, conversations), threads[_EXCLUDED_CHAT], "派活会话的消息。")
    _observe((service, conversations), threads[_OTHER_CHAT], "普通会话的消息。")

    skipped = _collected_threads(conversations, excluded=frozenset({_EXCLUDED_CHAT}))
    unreadable = _collected_threads(conversations, excluded=None)
    baseline = _collected_threads(conversations, excluded=frozenset())

    assert threads[_OTHER_CHAT].thread_id in skipped
    assert threads[_EXCLUDED_CHAT].thread_id not in skipped
    assert unreadable == set()
    assert {threads[_EXCLUDED_CHAT].thread_id, threads[_OTHER_CHAT].thread_id} <= baseline


def test_excluded_pending_candidate_is_marked_and_not_promoted(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe((service, conversations), threads[_EXCLUDED_CHAT], "派活会话沉淀的约定。")

    marked = sync_learning_exclusion_marks(
        conversation_store=conversations,
        candidate_service=service.candidates,
        excluded_conversation_ids=frozenset({_EXCLUDED_CHAT}),
    )

    assert marked["marked"] == 1
    stored = service.candidates.get(candidate.candidate_id)
    assert stored.promotion_exclusion == LEARNING_SOURCE_EXCLUDED
    result = service.promote(candidate.candidate_id, automatic=True)
    # v2：agents 目标在最终提交入口一律先被硬拦；名单标记仍持久化，两道闸都拦住写入。
    assert result.promoted is False
    assert result.reason_code == "AUTO_AGENTS_WRITE_FORBIDDEN"
    assert service.candidates.get(candidate.candidate_id).status == "pending_review"
    assert "派活会话沉淀的约定。" not in service.persona.path_for("agents").read_text(encoding="utf-8")


def test_curator_backlog_skips_excluded_and_agents_candidates(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    excluded = _observe((service, conversations), threads[_EXCLUDED_CHAT], "派活会话沉淀的约定。")
    agents_candidate = _observe(
        (service, conversations), threads[_OTHER_CHAT], "普通会话的 agents 约定。"
    )
    keeper = _observe(
        (service, conversations),
        threads[_OTHER_CHAT],
        "普通会话的长期事实。",
        promotion_target="long_term",
    )
    sync_learning_exclusion_marks(
        conversation_store=conversations,
        candidate_service=service.candidates,
        excluded_conversation_ids=frozenset({_EXCLUDED_CHAT}),
    )
    curator = _curator(
        tmp_path,
        conversations,
        service.candidates,
        exclusions=lambda: (frozenset({_EXCLUDED_CHAT}), False),
    )

    pending = curator._pending_promotable_candidate_ids(limit=32)

    assert keeper.candidate_id in pending
    assert excluded.candidate_id not in pending
    # v2：agents 目标的候选不再进入自动补晋升队列（与最终提交入口硬拦同步）。
    assert agents_candidate.candidate_id not in pending


def test_unreadable_exclusions_fail_closed_for_backlog(tmp_path: Path):
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe((service, conversations), threads[_OTHER_CHAT], "普通会话的约定。")
    curator = _curator(
        tmp_path,
        conversations,
        service.candidates,
        exclusions=lambda: (frozenset(), True),
    )

    assert curator._pending_promotable_candidate_ids(limit=32) == []
    assert service.candidates.get(candidate.candidate_id).status == "pending_review"


def test_exclusion_sync_skips_already_marked_and_empty_thread_refs(tmp_path: Path, monkeypatch):
    service, conversations, threads = _runtime(tmp_path)
    candidate = _observe((service, conversations), threads[_EXCLUDED_CHAT], "派活会话沉淀的约定。")
    first = sync_learning_exclusion_marks(
        conversation_store=conversations,
        candidate_service=service.candidates,
        excluded_conversation_ids=frozenset({_EXCLUDED_CHAT}),
    )
    assert first["marked"] == 1

    mark_calls: list[str] = []
    monkeypatch.setattr(
        service.candidates,
        "mark_promotion_excluded",
        lambda cid, reason="": mark_calls.append(cid) or [],
    )
    second = sync_learning_exclusion_marks(
        conversation_store=conversations,
        candidate_service=service.candidates,
        excluded_conversation_ids=frozenset({_EXCLUDED_CHAT}),
    )
    assert second["marked"] == 0
    assert mark_calls == []

    no_thread_row = service.candidates.get(candidate.candidate_id)
    no_thread_row.source_message_refs = [{"thread_id": ""}]
    thread_scans: list[int] = []
    monkeypatch.setattr(
        conversations.threads,
        "list_report",
        lambda limit=0: thread_scans.append(limit) or ([], []),
    )
    from agent_py_agent.agent.memory_store.learning_exclusions import excluded_candidate_ids

    assert (
        excluded_candidate_ids(
            rows=[no_thread_row],
            conversation_store=conversations,
            excluded_conversation_ids=frozenset({_EXCLUDED_CHAT}),
        )
        == set()
    )
    assert thread_scans == []
