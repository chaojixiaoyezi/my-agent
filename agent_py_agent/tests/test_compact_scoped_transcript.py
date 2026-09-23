"""显式摘要范围贯穿原 transcript Compact writer/CAS 的测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit, MessageLogEntry


@pytest.fixture
def scoped_case(tmp_path, monkeypatch):
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
        allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    clock = [10.0]
    monkeypatch.setattr(checkpoints, "time", SimpleNamespace(time=lambda: clock[0]))
    calls = []

    def summarize(_agent, previous, evidence, rows, **_kwargs):
        calls.append((previous, dict(evidence), tuple(row.message_id for row in rows)))
        return f"{previous}|{','.join(row.content for row in rows)}"

    monkeypatch.setattr(compact_module, "_summarize", summarize)
    return SimpleNamespace(
        store=store, thread_id=thread.thread_id, agent=agent, policy=policy,
        clock=clock, calls=calls,
    )


def _message(case, content):
    return case.store.messages.append({
        "thread_id": case.thread_id, "role": "user", "content": content,
        "now": case.clock[0],
    })


def _source(case, thread, scope, rows):
    view = resolve_compact_summary_view(case.agent, thread, scope)
    return ConversationCompactSource(
        thread=thread, messages=tuple(rows), policy=case.policy,
        recent_operation_evidence={},
        compact_context=AppliedCompactContext(thread.thread_id, scope, view),
    )


def _projector(seen):
    def project(view):
        seen.append((view.is_candidate, view.summary, view.operation_evidence,
                     tuple(row.message_id for row in view.messages)))
        return ConversationCompactProjection(
            projected_tokens=300 if view.is_candidate else 950,
            material={"summary": view.summary, "candidate": view.is_candidate},
        )

    return project


def _compact(case, scope, content):
    thread = case.store.threads.require(case.thread_id)
    row = _message(case, content)
    seen = []
    result = prepare_conversation_context(
        case.agent, case.store, thread,
        options=ConversationCompactOptions(
            source=_source(case, thread, scope, (row,)),
            request_projector=_projector(seen),
            exclude_request_id=f"request-{content}", force=True,
        ),
    )
    assert result.compacted and result.request_projection is not None
    assert seen[0][1] == case.calls[-1][0]
    assert seen[-1][1] == result.request_projection.material["summary"]
    return result, row, seen


def test_task_scoped_transcript_keeps_own_base_and_global_cursor(scoped_case):
    case = scoped_case
    task_a = CompactScope(kind="task", task_id="A", task_ids=("A",),
                          anchor_message_id="anchor-A", created_at=15.0)
    task_b = CompactScope(kind="task", task_id="B", task_ids=("B",),
                          anchor_message_id="anchor-B", created_at=35.0)
    first, first_row, _seen = _compact(case, THREAD_COMPACT_SCOPE, "thread-1")
    first_id = first.thread.compact_checkpoint_id
    assert first.thread.summary == "|thread-1"

    case.clock[0] = 20.0
    a1, a1_row, a1_seen = _compact(case, task_a, "A-1")
    a1_id = a1.thread.compact_checkpoint_id
    assert a1_seen[0][1] == first.thread.summary
    assert a1.thread.summary == first.thread.summary
    assert a1.thread.compacted_through_message_id == first.thread.compacted_through_message_id
    assert a1.thread.compacted_through_byte_offset == first.thread.compacted_through_byte_offset
    assert a1.thread.compact_updated_at == first.thread.compact_updated_at
    assert a1.thread.compact_operation_evidence == first.thread.compact_operation_evidence

    case.clock[0] = 30.0
    second, second_row, second_seen = _compact(case, THREAD_COMPACT_SCOPE, "thread-2")
    second_id = second.thread.compact_checkpoint_id
    assert second_seen[0][1] == first.thread.summary
    assert second.thread.summary == "|thread-1|thread-2"
    assert second.thread.compacted_through_message_id == second_row.message_id

    case.clock[0] = 40.0
    b1, b1_row, b1_seen = _compact(case, task_b, "B-1")
    b1_id = b1.thread.compact_checkpoint_id
    assert b1_seen[0][1] == second.thread.summary

    case.clock[0] = 50.0
    a2, a2_row, a2_seen = _compact(case, task_a, "A-2")
    a2_id = a2.thread.compact_checkpoint_id
    assert a2_seen[0][1] == "|thread-1|A-1"
    assert a2.thread.summary == second.thread.summary
    assert a2.thread.compacted_through_message_id == second_row.message_id
    assert a2.thread.compacted_through_byte_offset == second.thread.compacted_through_byte_offset
    assert a2.thread.compact_updated_at == second.thread.compact_updated_at

    chain = checkpoints.committed_compact_checkpoint_chain(case.agent, a2.thread)
    assert [(row["checkpoint_id"], row["summary_base_checkpoint_id"]) for row in chain] == [
        (first_id, ""), (a1_id, first_id), (second_id, first_id),
        (b1_id, second_id), (a2_id, a1_id),
    ]
    assert [row["scope"] for row in chain] == [
        THREAD_COMPACT_SCOPE.to_dict(), task_a.to_dict(), THREAD_COMPACT_SCOPE.to_dict(),
        task_b.to_dict(), task_a.to_dict(),
    ]
    assert resolve_compact_summary_view(case.agent, a2.thread, THREAD_COMPACT_SCOPE).source_message_ids == frozenset({
        first_row.message_id, second_row.message_id,
    })
    assert resolve_compact_summary_view(case.agent, a2.thread, task_a).source_message_ids == frozenset({
        first_row.message_id, a1_row.message_id, a2_row.message_id,
    })
    assert resolve_compact_summary_view(case.agent, a2.thread, task_b).source_message_ids == frozenset({
        first_row.message_id, second_row.message_id, b1_row.message_id,
    })


def test_scope_source_mismatch_and_empty_source_fail_or_noop_before_summary(scoped_case):
    case = scoped_case
    thread = case.store.threads.require(case.thread_id)
    scope = CompactScope(kind="turn", turn_id="turn-A")
    row = _message(case, "one")
    source = _source(case, thread, scope, (row,))
    wrong_thread = AppliedCompactContext("other-thread", scope, source.compact_context.view)
    with pytest.raises(ConversationCompactError, match="scope mismatches"):
        prepare_conversation_context(case.agent, case.store, thread, options=ConversationCompactOptions(
            source=ConversationCompactSource(thread, (row,), case.policy, {}, wrong_thread),
            request_projector=_projector([]), force=True,
        ))
    wrong_row = MessageLogEntry("other-row", "other-thread", "user", "foreign")
    with pytest.raises(ConversationCompactError, match="messages are invalid"):
        prepare_conversation_context(case.agent, case.store, thread, options=ConversationCompactOptions(
            source=ConversationCompactSource(thread, (wrong_row,), case.policy, {}, source.compact_context),
            request_projector=_projector([]), force=True,
        ))
    empty = ConversationCompactSource(thread, (), case.policy, {}, source.compact_context)
    result = prepare_conversation_context(case.agent, case.store, thread, options=ConversationCompactOptions(
        source=empty, request_projector=_projector([]), force=True,
    ))
    assert not result.compacted and result.messages == ()
    assert case.calls == [] and case.store.threads.require(case.thread_id).compact_generation == 0


def test_scoped_preflight_noop_uses_local_summary_without_publishing(scoped_case):
    case = scoped_case
    first, _row, _seen = _compact(case, THREAD_COMPACT_SCOPE, "thread-1")
    task = CompactScope(kind="task", task_id="A", created_at=15.0)
    case.clock[0] = 20.0
    local, _row, _seen = _compact(case, task, "A-1")
    pending = _message(case, "A-pending")
    seen = []

    def under_threshold(view):
        seen.append((view.summary, view.operation_evidence, view.is_candidate))
        return ConversationCompactProjection(projected_tokens=100, material={"ready": True})

    result = prepare_conversation_context(
        case.agent, case.store, local.thread,
        options=ConversationCompactOptions(
            source=_source(case, local.thread, task, (pending,)),
            request_projector=under_threshold, exclude_request_id="request-pending",
        ),
    )
    assert not result.compacted and result.messages == (pending,) and result.projected_tokens == 100
    assert seen == [("|thread-1|A-1", resolve_compact_summary_view(
        case.agent, local.thread, task,
    ).operation_evidence, False)]
    assert case.store.threads.require(case.thread_id) == local.thread
    assert local.thread.summary == first.thread.summary


def test_competing_cas_keeps_winning_thread_summary_and_orphan_invisible(scoped_case, monkeypatch):
    case = scoped_case
    first, _row, _seen = _compact(case, THREAD_COMPACT_SCOPE, "thread-1")
    first_id = first.thread.compact_checkpoint_id
    task = CompactScope(kind="task", task_id="A", created_at=15.0)
    case.clock[0] = 20.0
    task_row = _message(case, "A-1")
    competing_row = _message(case, "thread-wins")

    def summarize_with_competitor(_agent, previous, evidence, rows, **_kwargs):
        assert previous == first.thread.summary and [row.message_id for row in rows] == [task_row.message_id]
        thread = case.store.threads.require(case.thread_id)
        winner_id = checkpoints.write_compact_checkpoint(
            case.agent, checkpoints.CompactCheckpointRequest(
                thread=thread, summary="winning-thread", operation_evidence={},
                compact_rows=(competing_row,), retained_tail=(), source_end_byte_offset=case.store.messages.byte_offset_after(
                    case.thread_id, competing_row.message_id,
                ), projected_tokens_before=950, projected_tokens_after=300,
                policy=case.policy, forced=True, summary_base_checkpoint_id=first_id,
            ),
        )
        case.store.threads.update_compact_state(
            case.thread_id,
            commit=ConversationCompactCommit(
                summary="winning-thread", operation_evidence={}, checkpoint_id=winner_id,
                compacted_through_message_id=competing_row.message_id,
                compacted_through_byte_offset=case.store.messages.byte_offset_after(
                    case.thread_id, competing_row.message_id,
                ), source_messages=2, source_tool_pairs=0,
            ), expected_generation=1,
        )
        return "losing-task"

    monkeypatch.setattr(compact_module, "_summarize", summarize_with_competitor)
    with pytest.raises(RuntimeError, match="generation changed"):
        prepare_conversation_context(
            case.agent, case.store, first.thread,
            options=ConversationCompactOptions(
                source=_source(case, first.thread, task, (task_row,)),
                request_projector=_projector([]),
                exclude_request_id="request-task", force=True,
            ),
        )
    current = case.store.threads.require(case.thread_id)
    assert current.compact_generation == 2 and current.summary == "winning-thread"
    assert current.compacted_through_message_id == competing_row.message_id
    assert resolve_compact_summary_view(case.agent, current, THREAD_COMPACT_SCOPE).checkpoint_id == current.compact_checkpoint_id
    assert resolve_compact_summary_view(case.agent, current, task).checkpoint_id == first_id
    assert len(checkpoints.committed_compact_checkpoint_chain(case.agent, current)) == 2
