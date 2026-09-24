"""媒体原生历史只由可完整阅读的文字前缀取得 transcript Compact 覆盖。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.auxiliary_model_call import AuxiliaryModelCallRequest
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
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


@pytest.fixture
def media_case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1.0})
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="test", model_name="test-model"),
        config=SimpleNamespace(), conversation_store=store,
    )
    policy = RuntimeCompactPolicy(
        context_window_tokens=1_000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600,
        allow_persistent_apply=True, recent_tail_max_turns=0, recent_tail_tokens=0,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    summarized = []

    def summarize(_agent, previous, _evidence, rows, **_kwargs):
        summarized.append(tuple(row.message_id for row in rows))
        return f"{previous}|已读文字前缀"

    monkeypatch.setattr(compact_module, "_summarize", summarize)
    return SimpleNamespace(store=store, thread=thread, agent=agent, policy=policy, summarized=summarized)


def _append(case, role, content, turn_id, *, native=None):
    metadata = {"conversation_request_id": turn_id}
    if native is not None:
        metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(native)
    return case.store.messages.append({
        "thread_id": case.thread.thread_id, "role": role, "content": content,
        "metadata": metadata, "now": 10.0,
    })


def _source(case, rows):
    thread = case.store.threads.require(case.thread.thread_id)
    view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    return ConversationCompactSource(
        thread, tuple(rows), case.policy, {},
        AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view),
    )


def _compact(case, rows, *, force):
    source = _source(case, rows)
    return prepare_conversation_context(
        case.agent, case.store, source.thread,
        options=ConversationCompactOptions(
            source=source, force=force, exclude_request_id="active-turn",
            request_projector=lambda view: ConversationCompactProjection(
                projected_tokens=300 if view.is_candidate else 950,
                material={"candidate": view.is_candidate, "messages": view.messages},
            ),
        ),
    )


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("block", [
    {"type": "image", "source": {"type": "opaque", "ref": "picture"}},
    {"type": "future_nontext", "payload": {"ref": "opaque"}},
])
def test_media_turn_and_following_rows_remain_exact_after_real_checkpoint(media_case, force, block):
    case = media_case
    first = [
        _append(case, "user", "旧问题", "turn-text"),
        _append(case, "assistant", "旧回答", "turn-text"),
    ]
    native = [
        {"role": "user", "content": [{"type": "text", "text": "看附件"}, block]},
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-media", "name": "read_asset", "input": {"ref": "opaque"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "call-media", "content": [block],
        }]},
        {"role": "assistant", "content": [{"type": "text", "text": "附件仍须保留"}]},
    ]
    protected = [
        _append(case, "user", "看附件", "turn-media"),
        _append(case, "assistant", "调用 read_asset", "turn-media"),
        _append(case, "assistant", "附件仍须保留", "turn-media", native=native),
        _append(case, "user", "下一个问题", "turn-later"),
        _append(case, "assistant", "下一个回答", "turn-later"),
    ]
    result = _compact(case, [*first, *protected], force=force)

    assert result.compacted
    assert case.summarized == [tuple(row.message_id for row in first)]
    assert result.messages == tuple(protected)
    assert result.request_projection.material["messages"] == tuple(protected)
    assert result.thread.compacted_through_message_id == first[-1].message_id
    assert resolve_compact_summary_view(
        case.agent, result.thread, THREAD_COMPACT_SCOPE,
    ).source_message_ids == frozenset(row.message_id for row in first)
    assert provider_history_messages_from_rows(result.messages) == tuple(native + [
        {"role": "user", "content": [{"type": "text", "text": "下一个问题"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "下一个回答"}]},
    ])
    assert case.store.messages.recent_report(case.thread.thread_id, limit=0)[0] == [*first, *protected]


def test_media_first_turn_has_no_checkpoint_or_coverage(media_case):
    case = media_case
    native = [{"role": "user", "content": [{"type": "image", "source": {"ref": "opaque"}}]}]
    rows = [
        _append(case, "user", "图片", "turn-media"),
        _append(case, "assistant", "继续", "turn-media", native=native),
        _append(case, "user", "后续", "turn-later"),
    ]
    with pytest.raises(ConversationCompactError) as error:
        _compact(case, rows, force=True)
    assert error.value.code == "COMPACT_SOURCE_EMPTY"
    assert case.summarized == []
    current = case.store.threads.require(case.thread.thread_id)
    assert current.compact_generation == 0 and current.compact_checkpoint_id == ""
    assert current.compacted_through_message_id == ""
    assert case.store.messages.recent_report(case.thread.thread_id, limit=0)[0] == rows


def test_segmented_summary_rejects_nontext_before_serializing_or_calling_model(monkeypatch):
    agent = SimpleNamespace(
        backend=SimpleNamespace(max_tokens=128),
        config=SimpleNamespace(model_context_window_tokens=2_000, model_context_window_explicit=True),
    )
    request = AuxiliaryModelCallRequest(
        agent=agent, prompt="请总结历史", system_instruction="稳定系统前缀", tools=[],
        messages=[{"role": "user", "content": [{"type": "future_nontext", "source": {"ref": "opaque"}}]}],
        purpose="conversation_compact_summary",
    )
    monkeypatch.setattr(budget_module, "_request_tokens", lambda _request, _source=None: 9_000)
    monkeypatch.setattr(
        budget_module, "generate_auxiliary_model_response",
        lambda _request: pytest.fail("unknown source reached the model"),
    )
    with pytest.raises(ConversationCompactError) as error:
        budget_module.generate_bounded_compact_response(request)
    assert error.value.code == "COMPACT_SOURCE_NON_TEXT"
