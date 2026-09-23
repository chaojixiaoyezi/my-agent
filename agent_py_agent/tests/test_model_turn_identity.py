"""模型轮身份跨同 attempt 工作片保持唯一，旧 Compact 不隐藏新调用。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _live_archive_state_from_carried_archive_tool_calls,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _begin_model_turn_identity,
    current_model_turn_source,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.active_turn_compact import (
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_checkpoint import (
    LiveToolCompactCheckpointRequest,
    write_live_tool_compact_checkpoint,
)
from agent_py_agent.agent.conversation.compact_tool_identity import compact_tool_ref
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


def test_next_goal_turn_keeps_call_outside_previous_checkpoint(tmp_path):
    calls = []
    for _ in range(2):
        params = SimpleNamespace(
            run_id="child-run", attempt_id="same-attempt",
            live_archive_state=_live_archive_state_from_carried_archive_tool_calls([]),
        )
        turn_id = _begin_model_turn_identity(params, 0)
        calls.append(ToolCall(
            call_id="reused-provider-id", tool_name="read_file", arguments={"path": "source"},
            source_protocol="native", schema_hash="sha256:test", run_id=params.run_id,
            attempt_id=params.attempt_id, turn_id=turn_id,
        ))

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner"})
    agent = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
    )
    policy = RuntimeCompactPolicy(
        context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600,
        allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    checkpoint_id = write_live_tool_compact_checkpoint(agent, LiveToolCompactCheckpointRequest(
        thread=thread, summary="first turn summary", source_tool_call_ids=(calls[0].call_id,),
        retained_tool_call_ids=(), source_tool_refs=(compact_tool_ref(calls[0]),),
        projected_tokens_before=900, projected_tokens_after=100, policy=policy,
        request_id="same-attempt", attempt_id="same-attempt",
    ))
    store.threads.update_compact_state(thread.thread_id, commit=ConversationCompactCommit(
        summary="first turn summary", operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id="", compacted_through_byte_offset=0,
        source_messages=0, source_tool_pairs=1,
    ), expected_generation=0)
    records = [dict(call.to_dict(), output=f"turn-{index}") for index, call in enumerate(calls)]
    visible = model_visible_active_turn_tool_calls(agent, {
        "conversation_thread_id": thread.thread_id,
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
    }, records)

    assert visible == [records[1]]


def test_unique_model_turn_identity_preserves_local_sequence_contract():
    params = SimpleNamespace(run_id="same-run", live_archive_state={})
    first = _begin_model_turn_identity(params, 4)
    first_source = current_model_turn_source(params)
    second = _begin_model_turn_identity(params, 4)
    second_source = current_model_turn_source(params)
    restarted = SimpleNamespace(run_id="same-run", live_archive_state={})
    third = _begin_model_turn_identity(restarted, 4)

    assert len({first, second, third}) == 3
    assert first_source == {"turn_id": first, "sequence": 1}
    assert second_source == {"turn_id": second, "sequence": 2}
    assert current_model_turn_source(restarted) == {"turn_id": third, "sequence": 1}
