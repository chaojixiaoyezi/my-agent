"""B 路：自动学到的工作约定进唯一正式库（long_term JSONL）的行为测试。

覆盖：来源门（可学才自动晋升）、同 scope 合并与版本检查、撤回、子代理标签与
lineage 保留、跨 scope 相似条目不合并、删除后旧版本不复活、模型不能自带来源、
curator 从引用消息判定来源、普通线程里的定时回合不可学。
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate


def _runtime(tmp_path: Path, *, thread_source=None):
    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    request = {
        "canonical_user_id": "user-1",
        "channel": "internal",
        "channel_conversation_id": "chat-1",
        "channel_user_id": "user-1",
        "now": 10.0,
    }
    if thread_source is not None:
        request["learning_source"] = thread_source
    thread = conversations.threads.get_or_create(request)
    service = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=CandidateService(owner / "memory" / "candidates.jsonl"),
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
        ),
    )
    return service, conversations, thread


def _observe_agreement(
    service,
    conversations,
    thread,
    **overrides,
):
    content = overrides.get("content", "约定：先读任务文件全文再动手。")
    learning_source = overrides.get("learning_source")
    subject_key = overrides.get("subject_key", "workflow.agents.dispatch")
    scope_type = overrides.get("scope_type", "personal")
    scope_key = overrides.get("scope_key", "personal")
    proposed_action = overrides.get("proposed_action", "add")
    target_entry_id = overrides.get("target_entry_id", "")
    # 替换/删除须带宿主冻结的目标观察版本（PERSONA_AGENTS_OWNERSHIP 第 74 行合同），测试模拟宿主传入。
    target_entry_version = overrides.get("target_entry_version")
    message = conversations.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": content, "now": 11.0}
    )
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    ref = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "role": "user",
        "content_hash": content_hash,
        "quote": content,
    }
    return service.candidates.observe(
        CandidateObservation(
            candidate_type="working_agreement",
            content=content,
            subject_key=subject_key,
            scope=MemoryScope(scope_type, scope_key),
            origin="model_inferred",
            source_message_refs=(ref,),
            proposed_action=proposed_action,
            target_entry_id=target_entry_id,
            target_entry_version=target_entry_version,
            promotion_target="long_term",
            confidence=0.9,
            learning_source=dict(learning_source or {}),
        )
    )


def test_working_agreement_with_user_source_promotes_to_long_term_library(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _observe_agreement(
        service, conversations, thread, learning_source=ls.user_dialogue_projection()
    )

    result = confirm_candidate(service, candidate.candidate_id)

    assert result.promoted is True
    records = service.long_term.all()
    assert len(records) == 1
    assert records[0].kind == "working_agreement"
    assert records[0].attributes["learning_source"]["can_learn"] is True
    assert records[0].attributes["learning_source"]["source_kind"] == "user_dialogue"


def test_working_agreement_without_source_is_blocked_and_stays(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _observe_agreement(service, conversations, thread)

    result = confirm_candidate(service, candidate.candidate_id)

    assert result.promoted is False
    # 来源不可学时权限资格本身就不是 auto_eligible（第一道门）；自动策略门另有直接测试。
    assert result.reason_code == "PROMOTION_MANUAL_REQUIRED"
    assert service.long_term.all() == []
    assert service.candidates.get(candidate.candidate_id).status == "pending_review"


def test_working_agreement_dispatch_source_is_blocked(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _observe_agreement(
        service, conversations, thread, learning_source=ls.dispatch_projection()
    )

    result = confirm_candidate(service, candidate.candidate_id)

    assert result.promoted is False
    assert result.reason_code == "PROMOTION_MANUAL_REQUIRED"
    assert service.long_term.all() == []


def test_automatic_policy_block_rechecks_source_for_auto_eligible_candidate(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.promotion import _automatic_policy_block

    service, conversations, thread = _runtime(tmp_path)
    candidate = _observe_agreement(
        service, conversations, thread, learning_source=ls.user_dialogue_projection()
    )
    stored = service.candidates.get(candidate.candidate_id)
    assert stored.promotion_mode == "auto_eligible"

    # 旧候选/被绕过场景：权限记录仍是 auto_eligible，但来源不可核验时必须被策略门拦住。
    stale = replace(stored, learning_source={}, promotion_mode="auto_eligible")
    assert _automatic_policy_block(stale) == "AUTO_POLICY_LEARNING_SOURCE_UNVERIFIED"


def test_subagent_source_keeps_tag_and_lineage(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    source = ls.subagent_projection(
        trigger_kind="user_dialogue", run_ids=["run-1"], task_ids=["task-1"]
    )
    candidate = _observe_agreement(service, conversations, thread, learning_source=source)

    assert confirm_candidate(service, candidate.candidate_id).promoted

    learning = service.long_term.all()[0].attributes["learning_source"]
    assert learning["subagent_tag"] == "subagent"
    assert learning["source_run_ids"] == ["run-1"]
    assert learning["source_task_ids"] == ["task-1"]


def test_same_scope_replace_and_remove_use_expected_version(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    first = _observe_agreement(
        service, conversations, thread, learning_source=ls.user_dialogue_projection()
    )
    assert confirm_candidate(service, first.candidate_id).promoted
    entry = service.long_term.all()[0]
    entry_id = entry.entry_id

    updated = _observe_agreement(
        service,
        conversations,
        thread,
        content="约定：先读任务文件全文，再动手并小步提交。",
        proposed_action="replace",
        target_entry_id=entry_id,
        target_entry_version=entry.version,
        learning_source=ls.user_dialogue_projection(),
    )
    assert confirm_candidate(service, updated.candidate_id).promoted
    assert [record.content for record in service.long_term.all()] == [
        "约定：先读任务文件全文，再动手并小步提交。"
    ]

    removed = _observe_agreement(
        service,
        conversations,
        thread,
        content="撤回该约定。",
        proposed_action="remove",
        target_entry_id=entry_id,
        target_entry_version=service.long_term.all()[0].version,
        learning_source=ls.user_dialogue_projection(),
    )
    assert confirm_candidate(service, removed.candidate_id).promoted
    assert service.long_term.all() == []


def test_stale_expected_version_cannot_overwrite_or_revive(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _observe_agreement(
        service, conversations, thread, learning_source=ls.user_dialogue_projection()
    )
    assert confirm_candidate(service, candidate.candidate_id).promoted
    record = service.long_term.all()[0]
    stale_version = record.version

    # 并发写者先推进版本。
    service.long_term.apply_batch(
        [
            {
                "action": "replace",
                "entry_id": record.entry_id,
                "content": "约定：先读任务文件全文再动手。（并发更新）",
                "kind": "working_agreement",
                "expected_version": stale_version,
            }
        ]
    )

    # 用旧版本再写：必须失败，不覆盖新值、也不让旧值复活。
    with pytest.raises(RuntimeError):
        service.long_term.apply_batch(
            [
                {
                    "action": "replace",
                    "entry_id": record.entry_id,
                    "content": "约定：旧值。",
                    "kind": "working_agreement",
                    "expected_version": stale_version,
                }
            ]
        )
    assert [item.content for item in service.long_term.all()] == [
        "约定：先读任务文件全文再动手。（并发更新）"
    ]


def test_cross_scope_similar_agreement_is_not_merged(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    personal = _observe_agreement(
        service, conversations, thread, learning_source=ls.user_dialogue_projection()
    )
    project = _observe_agreement(
        service,
        conversations,
        thread,
        scope_type="project",
        scope_key="project:demo",
        learning_source=ls.user_dialogue_projection(),
    )

    assert confirm_candidate(service, personal.candidate_id).promoted
    assert confirm_candidate(service, project.candidate_id).promoted

    assert {record.attributes["scope_key"] for record in service.long_term.all()} == {
        "personal",
        "project:demo",
    }


def test_curator_candidate_payload_cannot_provide_learning_source():
    from agent_py_agent.agent.memory_store.curator_models import (
        CURATOR_OUTPUT_SCHEMA_VERSION,
        parse_curator_extraction,
    )

    payload = {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [
            {
                "candidate_type": "working_agreement",
                "content": "约定：先读任务文件全文再动手。",
                "subject_key": "workflow.agents.dispatch",
                "scope": {"scope_type": "personal", "scope_key": "personal"},
                "origin": "model_inferred",
                "source_message_refs": [{"message_id": "msg-1"}],
                "learning_source": {"can_learn": True, "source_kind": "user_dialogue"},
            }
        ],
        "processed_message_refs": [{"message_id": "msg-1"}],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {"per_thread_cursors": [], "last_audit_event_id": None},
    }
    with pytest.raises(ValueError):
        parse_curator_extraction(json.dumps(payload))


def _attach_fixture(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    user_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-user",
            "channel_user_id": "user-1",
            "now": 10.0,
            "learning_source": ls.user_dialogue_projection(),
        }
    )
    dispatch_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-2",
            "channel": "internal",
            "channel_conversation_id": "chat-dispatch",
            "channel_user_id": "user-2",
            "now": 10.0,
            "learning_source": ls.dispatch_projection(),
        }
    )
    user_message = store.messages.append(
        {"thread_id": user_thread.thread_id, "role": "user", "content": "hello", "now": 11.0}
    )
    dispatch_message = store.messages.append(
        {"thread_id": dispatch_thread.thread_id, "role": "user", "content": "task book", "now": 11.0}
    )
    return user_thread, dispatch_thread, user_message, dispatch_message


def test_curator_attach_source_uses_referenced_message_facts(tmp_path):
    from agent_py_agent.agent.memory_store.curator import _attach_learning_source
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorMessageInput

    user_thread, dispatch_thread, user_message, dispatch_message = _attach_fixture(tmp_path)

    def _input(entry, thread_source):
        return CuratorMessageInput(
            message_id=entry.message_id,
            thread_id=entry.thread_id,
            role=entry.role,
            channel=entry.channel,
            created_at=entry.created_at,
            full_content=entry.content,
            content_preview=entry.content,
            content_hash="sha256:x",
            metadata=dict(entry.metadata),
            thread_source=dict(thread_source),
        )

    batch = SimpleNamespace(
        messages=(
            _input(user_message, user_thread.learning_source),
            _input(dispatch_message, dispatch_thread.learning_source),
        )
    )

    # 消息来源按 message_id + thread_id 精确定位，只给 message_id 的引用视为未知来源。
    def _item(message_id, thread_id):
        return CandidateObservation(
            candidate_type="working_agreement",
            content="约定",
            subject_key="workflow.agents.dispatch",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            source_message_refs=({"message_id": message_id, "thread_id": thread_id},),
        )

    learnable = _attach_learning_source(_item(user_message.message_id, user_message.thread_id), batch, "user-1")
    blocked = _attach_learning_source(_item(dispatch_message.message_id, dispatch_message.thread_id), batch, "user-1")
    unknown = _attach_learning_source(_item("msg-missing", user_message.thread_id), batch, "user-1")
    bare = _attach_learning_source(
        replace(_item(user_message.message_id, user_message.thread_id),
                source_message_refs=({"message_id": user_message.message_id},)),
        batch, "user-1")
    assert bare.learning_source["can_learn"] is False

    assert learnable.learning_source["can_learn"] is True
    assert learnable.learning_source["source_kind"] == "user_dialogue"
    assert blocked.learning_source["can_learn"] is False
    assert blocked.learning_source["source_kind"] == "dispatch"
    assert unknown.learning_source["can_learn"] is False
    assert unknown.learning_source["source_kind"] == "unknown"


def test_scheduler_turn_inside_user_thread_is_unlearnable(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-mixed",
            "channel_user_id": "user-1",
            "now": 10.0,
            "learning_source": ls.user_dialogue_projection(),
        }
    )
    message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "定时任务完成",
            "now": 11.0,
            "metadata": {"scheduler_run_id": "sched-run-1", "scheduler_job_id": "job-1"},
        }
    )

    facts = ls.facts_for_message(
        message.metadata,
        owner="user-1",
        thread_id=thread.thread_id,
        thread_source=thread.learning_source,
    )

    assert facts.can_learn is False
    assert facts.source_kind == "schedule"
