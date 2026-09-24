"""媒体压缩策略片 A：块分类、归档引用投影、后缀保护按策略切换，以及 checkpoint 里的媒体事实。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.backends.request_content import (
    NonTextClasses,
    classify_nontext_content,
    compact_source_supported,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    _split_nontext_transcript_suffix,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_media_policy import (
    CompactMediaDecision,
    MediaArchiveFacts,
    media_archive_facts,
    project_archived_media_message,
    resolve_compact_media_policy,
)
from agent_py_agent.agent.conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    conversation_compact_provider_source,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)

SHA = "a" * 64
LOCAL_IMAGE = {"type": "image", "source": {"type": "local_file", "path": "/owner/attachments/" + SHA, "sha256": SHA,
                                            "media_type": "image/png", "size_bytes": 321, "name": "chart.png"}}
OPAQUE_IMAGE = {"type": "image", "source": {"type": "opaque", "ref": "picture"}}


def test_classify_nontext_content_counts_media_and_unknown_separately():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "看图"}, LOCAL_IMAGE]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "read_asset", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": [OPAQUE_IMAGE]}]},
    ]
    assert classify_nontext_content(messages) == NonTextClasses(media=1, unknown=1)
    nested = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": [LOCAL_IMAGE]}]},
              {"role": "assistant", "content": [LOCAL_IMAGE]}]
    assert classify_nontext_content(nested) == NonTextClasses(media=0, unknown=2), "运输层不会展开的位置一律 unknown"
    assert classify_nontext_content([{"role": "user", "content": "纯文字"}]) == NonTextClasses()
    assert classify_nontext_content([{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "x"}}]}]) == NonTextClasses(unknown=1)


@pytest.mark.parametrize("policy, blocks, expected", [
    ("off", [], True), ("off", [LOCAL_IMAGE], False), ("off", [OPAQUE_IMAGE], False),
    ("archived_refs", [LOCAL_IMAGE], True), ("archived_refs", [OPAQUE_IMAGE], False), ("archived_refs", [LOCAL_IMAGE, OPAQUE_IMAGE], False),
    ("auto", [LOCAL_IMAGE], True),
])
def test_compact_source_supported_matrix(policy, blocks, expected):
    messages = [{"role": "user", "content": [{"type": "text", "text": "看"}, *blocks]}]
    assert compact_source_supported(messages, media_policy=policy) is expected


@pytest.mark.parametrize("configured, expected", [
    ("off", CompactMediaDecision("off", "policy_forced")),
    ("archived_refs", CompactMediaDecision("archived_refs", "policy_forced")),
    ("auto", CompactMediaDecision("archived_refs", "vision_fact_pending", vision_candidate=True)),
    (None, CompactMediaDecision("archived_refs", "vision_fact_pending", vision_candidate=True)),
])
def test_resolve_compact_media_policy(configured, expected):
    agent = SimpleNamespace(config=SimpleNamespace(compact_media_policy=configured) if configured is not None else SimpleNamespace())
    assert resolve_compact_media_policy(agent) == expected


def test_invalid_policy_fails_closed():
    with pytest.raises(ValueError):
        resolve_compact_media_policy(SimpleNamespace(config=SimpleNamespace(compact_media_policy="vision")))


def test_project_archived_media_message_touches_only_user_local_media():
    message = {"role": "user", "content": [{"type": "text", "text": "看图"}, LOCAL_IMAGE, OPAQUE_IMAGE]}
    projected = project_archived_media_message(message)
    assert projected["content"][0] == {"type": "text", "text": "看图"}
    reference = projected["content"][1]
    assert reference["type"] == "text" and "sha256:" + SHA[:12] in reference["text"] and "chart.png" in reference["text"]
    assert "321" in reference["text"] and "/owner/attachments" not in reference["text"]
    assert projected["content"][2] is OPAQUE_IMAGE, "未知块原样保留，由后缀保护处理"
    assert message["content"][1] is LOCAL_IMAGE, "输入消息不被改写"
    assistant = {"role": "assistant", "content": [LOCAL_IMAGE]}
    assert project_archived_media_message(assistant) is assistant
    plain = {"role": "user", "content": "纯文字"}
    assert project_archived_media_message(plain) is plain


@pytest.fixture
def media_case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1.0})
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="test", model_name="test-model"),
        config=SimpleNamespace(compact_media_policy="auto"), conversation_store=store,
    )
    policy = RuntimeCompactPolicy(
        context_window_tokens=1_000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600,
        allow_persistent_apply=True, recent_tail_max_turns=0, recent_tail_tokens=0,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    calls = []

    def summarize(_agent, previous, _evidence, rows, **kwargs):
        calls.append((tuple(row.message_id for row in rows), kwargs.get("call")))
        return f"{previous}|摘要"

    monkeypatch.setattr(compact_module, "_summarize", summarize)
    return SimpleNamespace(store=store, thread=thread, agent=agent, policy=policy, calls=calls)


def _append(case, role, content, turn_id, *, native=None):
    metadata = {"conversation_request_id": turn_id}
    if native is not None:
        metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(native)
    return case.store.messages.append({"thread_id": case.thread.thread_id, "role": role, "content": content,
                                       "metadata": metadata, "now": 10.0})


def _rows(case, block):
    native = [{"role": "user", "content": [{"type": "text", "text": "看附件"}, block]},
              {"role": "assistant", "content": [{"type": "text", "text": "图里是红色"}]}]
    return [
        _append(case, "user", "旧问题", "turn-text"), _append(case, "assistant", "旧回答", "turn-text"),
        _append(case, "user", "看附件", "turn-media"), _append(case, "assistant", "图里是红色", "turn-media", native=native),
        _append(case, "user", "下一个问题", "turn-later"), _append(case, "assistant", "下一个回答", "turn-later"),
    ]


def _compact(case, rows, *, force):
    thread = case.store.threads.require(case.thread.thread_id)
    view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    source = ConversationCompactSource(thread, tuple(rows), case.policy, {},
                                       AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view))
    return prepare_conversation_context(
        case.agent, case.store, source.thread,
        options=ConversationCompactOptions(
            source=source, force=force, exclude_request_id="active-turn",
            request_projector=lambda view: ConversationCompactProjection(
                projected_tokens=300 if view.is_candidate else 950,
                material={"candidate": view.is_candidate, "messages": view.messages}),
        ),
    )


@pytest.mark.parametrize("policy, block, protected_from", [
    ("off", LOCAL_IMAGE, 2), ("off", OPAQUE_IMAGE, 2),
    ("archived_refs", LOCAL_IMAGE, None), ("archived_refs", OPAQUE_IMAGE, 2),
])
def test_split_suffix_protects_by_policy(media_case, policy, block, protected_from):
    rows = _rows(media_case, block)
    prefix, suffix = _split_nontext_transcript_suffix(tuple(rows), media_policy=policy)
    if protected_from is None:
        assert list(prefix) == rows and list(suffix) == []
    else:
        assert list(prefix) == rows[:protected_from] and list(suffix) == rows[protected_from:]


def test_provider_source_projects_media_on_every_replay(media_case):
    rows = _rows(media_case, LOCAL_IMAGE)
    projected = conversation_compact_provider_source("", 0, tuple(rows), project_message=project_archived_media_message)
    for _replay in range(2):
        text = json.dumps(list(projected), ensure_ascii=False)
        assert "local_file" not in text and "附件引用" in text and "/owner/attachments" not in text
    plain = json.dumps(list(conversation_compact_provider_source("", 0, tuple(rows))), ensure_ascii=False)
    assert "local_file" in plain and "附件引用" not in plain, "没有投影时来源逐字保留"
    assert media_archive_facts(rows) == MediaArchiveFacts(blocks=1, refs=(SHA,), bytes=321, videos=0)


def test_archived_refs_compaction_covers_media_turn_and_records_facts(media_case):
    case = media_case
    rows = _rows(case, LOCAL_IMAGE)
    result = _compact(case, rows, force=True)
    assert result.compacted and result.messages == ()
    (summarized, call), = case.calls
    assert summarized == tuple(row.message_id for row in rows)
    assert call.media_policy == "archived_refs" and call.media_archived is True
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    # 强制恢复一律归档引用：fact_source 记 policy_forced，reason 记 forced_recovery（片 B 起）。
    assert checkpoint["media_policy"] == "archived_refs" and checkpoint["media_fact_source"] == "policy_forced"
    assert checkpoint["media_policy_reason"] == "forced_recovery"
    assert checkpoint["media_blocks_archived"] == 1 and checkpoint["media_blocks_summarized"] == 0
    assert checkpoint["media_refs"] == [SHA]
    assert provider_history_messages_from_rows(result.messages) == ()
    assert case.store.messages.recent_report(case.thread.thread_id, limit=0)[0] == rows, "canonical 行不删不改"


def test_off_policy_keeps_media_suffix_and_writes_no_media_fields(media_case):
    case = media_case
    case.agent.config.compact_media_policy = "off"
    rows = _rows(case, LOCAL_IMAGE)
    result = _compact(case, rows, force=True)
    assert result.compacted and list(result.messages) == rows[2:]
    (summarized, call), = case.calls
    assert summarized == tuple(row.message_id for row in rows[:2]) and call.media_policy == "off"
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert "media_policy" not in checkpoint and "media_refs" not in checkpoint


def test_unknown_block_stays_protected_under_archived_refs(media_case):
    case = media_case
    rows = _rows(case, OPAQUE_IMAGE)
    result = _compact(case, rows, force=True)
    assert result.compacted and list(result.messages) == rows[2:]
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert "media_policy" not in checkpoint
