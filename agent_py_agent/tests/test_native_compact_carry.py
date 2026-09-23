# LLM: 只验证临时原生 Compact 载体的内存身份、释放和历史隔离，不代替宿主 CAS 或真实 provider 验收。
# 模块用途: 防止超窗续接复用错 owner/工作片/摘要，或按相同文字误删真实用户插话。
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeLoopParams
from agent_py_agent.agent.agent_core.tool_ir_history import record_tool_call_ir
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.conversation.active_turn_compact import (
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.compact_carry import (
    capture_native_compact_carry,
    native_compact_carry_from_result,
    restore_native_compact_carry,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    CompactSummaryView,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)


@pytest.mark.parametrize("attributes", [{}, {"conversation_thread_id": "other-thread"},
    {"agent_thread_id": "thread-1", "conversation_thread_id": "parent-thread"}])
def test_host_compact_view_checks_explicit_current_thread_without_creating_task(attributes):
    context = _context()
    if attributes == {"conversation_thread_id": "other-thread"}:
        with pytest.raises(OSError, match="线程不匹配"):
            model_visible_active_turn_tool_calls(None, attributes, [], compact_context=context)
    else:
        before = dict(attributes)
        assert model_visible_active_turn_tool_calls(None, attributes, [], compact_context=context) == []
        assert attributes == before


# LLM: 捕获入口接收真实 ToolLoopExecuteParams 形状，不能拿缺少本轮回执状态的外层 RunParams 代替。
# 函数用途: 构造一个具备真实执行身份、原生协议与活动 IR 的内存工具循环输入。
def _loop_params(*, context: AppliedCompactContext | None = None) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="继续任务", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=["已执行工具"],
        effective_on_chunk=None, allowed_tools=None, write_boundary=None, task_attributes={},
        request_id="request-1", run_id="run-1", task_id="task-1",
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        attempt_id="attempt-1", conversation_turn_id="logical-turn-1",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        compact_context=context or _context(),
    )


# LLM: 不构造 Agent；fake store 仅记录原 guidance recovery 的精确释放参数及原返回身份。
# 函数用途: 让 capture 的释放调用可核对 request 与来源 attempt，且不碰文件或真实账本。
def _agent(*, owner_id: str = "owner-1", released: tuple[str, ...] = ()) -> object:
    calls: list[tuple[str, str]] = []

    def release_reserved(turn_id: str, *, dead_attempt_id: str) -> dict[str, object]:
        calls.append((turn_id, dead_attempt_id))
        return {"released_guidance_ids": list(released)}

    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_id=owner_id),
        conversation_store=SimpleNamespace(
            guidance=SimpleNamespace(recovery=SimpleNamespace(release_reserved=release_reserved)),
        ),
        release_calls=calls,
    )


# LLM: 真实摘要视图和范围作为整体绑定，不能以相同正文替代不同 scope/view。
# 函数用途: 构造本轮冻结的线程摘要上下文，供载体身份检查使用。
def _context(*, scope: CompactScope = THREAD_COMPACT_SCOPE, summary: str = "已有摘要") -> AppliedCompactContext:
    return AppliedCompactContext(
        thread_id="thread-1", scope=scope,
        view=CompactSummaryView(
            checkpoint_id="checkpoint-1", summary=summary,
            operation_evidence={"source": {"state": "old"}}, generation=1,
        ),
    )


# LLM: 恢复参数是外层 RuntimeLoopParams；attempt 可由主宿主重新创建，但其余身份沿原工作片冻结。
# 函数用途: 按原结果构造待恢复请求，便于逐字段篡改验证拒绝行为。
def _resume_params(carry, *, attempt_id: str = "attempt-1", context_scope: str = "default") -> RuntimeLoopParams:
    return RuntimeLoopParams(
        user_prompt="继续任务", root_user_prompt="继续任务", memories=[], runtime_injections=[],
        routed_context=None, resume_context_section="", request_id="request-1", run_id="run-1",
        task_id="task-1", attempt_id=attempt_id, conversation_turn_id="logical-turn-1",
        context_scope=context_scope, compact_context=carry.compact_context, native_compact_carry=carry,
    )


def test_capture_freezes_tool_loop_history_and_releases_source_attempt() -> None:
    params = _loop_params()
    turn = AssistantTurn(text="先读资料", content_blocks=[{"type": "text", "text": "原块"}])
    params.tool_ir_history.extend([turn, UserTurn("补充要求", input_ids=("guidance-a",))])
    params.live_archive_state.update({
        "_guidance_ack_ids": {"guidance-a"},
        "_forwarded_runtime_guidance": {"forwarded-a"},
    })
    agent = _agent(released=("guidance-a",))

    carry = capture_native_compact_carry(agent, params, SimpleNamespace(runtime_status="context_overflow"))

    assert carry is not None
    assert agent.release_calls == [("request-1", "attempt-1")]
    assert carry.released_input_ids == ("guidance-a",)
    assert carry.source_attempt_id == "attempt-1"
    assert carry.forwarded_guidance == frozenset({"forwarded-a"})
    turn.content_blocks[0]["text"] = "后来修改"
    params.tool_context[0] = "后来工具上下文"
    params.compact_context.view.operation_evidence["source"]["state"] = "later"
    params.live_archive_state["_forwarded_runtime_guidance"].add("forwarded-b")
    assert carry.history[0].content_blocks == [{"type": "text", "text": "原块"}]
    assert carry.tool_context == ("已执行工具",)
    assert carry.compact_context.view.operation_evidence == {"source": {"state": "old"}}
    assert carry.forwarded_guidance == frozenset({"forwarded-a"})

    restored = restore_native_compact_carry(agent, _resume_params(carry))
    assert restored is not None and restored is not carry
    assert restored.history[0] is not carry.history[0]
    restored.history[0].content_blocks[0]["text"] = "恢复副本修改"
    assert carry.history[0].content_blocks[0]["text"] == "原块"


@pytest.mark.parametrize("field,value", [
    ("request_id", "request-other"), ("run_id", "run-other"),
    ("task_id", "task-other"), ("conversation_turn_id", "logical-turn-other"),
])
def test_restore_rejects_changed_request_run_task_or_logical_turn(field: str, value: str) -> None:
    agent = _agent()
    carry = capture_native_compact_carry(
        agent, _loop_params(), SimpleNamespace(runtime_status="context_overflow"),
    )
    assert carry is not None

    with pytest.raises(ConversationCompactError, match="来源身份") as error:
        restore_native_compact_carry(agent, replace(_resume_params(carry), **{field: value}))
    assert error.value.code == "COMPACT_SOURCE_CHANGED"


def test_restore_rejects_changed_owner_scope_and_view() -> None:
    agent = _agent()
    carry = capture_native_compact_carry(
        agent, _loop_params(), SimpleNamespace(runtime_status="context_overflow"),
    )
    assert carry is not None
    changed_scope = _context(scope=CompactScope(kind="turn", turn_id="logical-turn-1"))
    changed_view = _context(summary="其它摘要")

    for other_agent, context in (
        (_agent(owner_id="owner-other"), carry.compact_context),
        (agent, changed_scope),
        (agent, changed_view),
    ):
        with pytest.raises(ConversationCompactError) as error:
            restore_native_compact_carry(
                other_agent, replace(_resume_params(carry), compact_context=context),
            )
        assert error.value.code == "COMPACT_SOURCE_CHANGED"


def test_main_new_attempt_allowed_but_child_new_attempt_rejected() -> None:
    agent = _agent()
    carry = capture_native_compact_carry(
        agent, _loop_params(), SimpleNamespace(runtime_status="context_overflow"),
    )
    assert carry is not None

    assert restore_native_compact_carry(agent, _resume_params(carry, attempt_id="attempt-2")) is not None
    assert restore_native_compact_carry(
        agent, _resume_params(carry, context_scope="task_local"),
    ) is not None
    with pytest.raises(ConversationCompactError) as error:
        restore_native_compact_carry(
            agent, _resume_params(carry, attempt_id="attempt-2", context_scope="task_local"),
        )
    assert error.value.code == "COMPACT_SOURCE_CHANGED"


def test_released_user_input_filters_only_matching_ids_not_identical_text() -> None:
    params = _loop_params()
    params.tool_ir_history.extend([
        UserTurn("相同正文", input_ids=("guidance-a",)),
        UserTurn("相同正文", input_ids=("guidance-b",)),
        UserTurn("普通初始输入"),
    ])
    params.live_archive_state["_guidance_ack_ids"] = {"guidance-a"}
    agent = _agent(released=("guidance-a",))
    carry = capture_native_compact_carry(agent, params, SimpleNamespace(runtime_status="context_overflow"))
    assert carry is not None

    result = native_compact_carry_from_result(SimpleNamespace(
        runtime_status="context_overflow", native_compact_carry=carry,
    ))

    assert result is not None
    assert result.history == (
        UserTurn("相同正文", input_ids=("guidance-b",)), UserTurn("普通初始输入"),
    )
    assert carry.history[0] == UserTurn("相同正文", input_ids=("guidance-a",))


def test_partially_released_user_input_group_is_rejected() -> None:
    params = _loop_params()
    params.tool_ir_history.append(UserTurn("一批插话", input_ids=("guidance-a", "guidance-b")))
    params.live_archive_state["_guidance_ack_ids"] = {"guidance-a"}
    agent = _agent(released=("guidance-a",))
    carry = capture_native_compact_carry(agent, params, SimpleNamespace(runtime_status="context_overflow"))
    assert carry is not None

    with pytest.raises(ConversationCompactError) as error:
        native_compact_carry_from_result(SimpleNamespace(
            runtime_status="context_overflow", native_compact_carry=carry,
        ))
    assert error.value.code == "COMPACT_SOURCE_CHANGED"


def test_nonoverflow_does_not_capture_or_release() -> None:
    agent, params = _agent(released=("guidance-a",)), _loop_params()
    params.live_archive_state["_guidance_ack_ids"] = {"guidance-a"}

    assert capture_native_compact_carry(agent, params, SimpleNamespace(runtime_status="completed")) is None
    assert agent.release_calls == []


def test_restored_marked_assistant_turn_cannot_merge_new_same_round_call() -> None:
    agent, params = _agent(), _loop_params()
    old_call = canonical_history_call("read_file", {"path": "old.txt"}, call_id="old-call")
    record_tool_call_ir(
        params, tool_rounds=1, call=old_call, result=canonical_history_result(old_call, "旧结果"),
    )
    assert isinstance(params.tool_ir_history[0], AssistantTurn)
    assert type(params.tool_ir_history[0]) is not AssistantTurn
    carry = capture_native_compact_carry(agent, params, SimpleNamespace(runtime_status="context_overflow"))
    assert carry is not None
    restored = restore_native_compact_carry(agent, _resume_params(carry))
    assert restored is not None
    assert type(restored.history[0]) is AssistantTurn

    next_loop = SimpleNamespace(tool_ir_history=list(restored.history))
    new_call = canonical_history_call("read_file", {"path": "new.txt"}, call_id="new-call")
    record_tool_call_ir(
        next_loop, tool_rounds=1, call=new_call,
        result=canonical_history_result(new_call, "新结果"),
    )

    turns = [item for item in next_loop.tool_ir_history if isinstance(item, AssistantTurn)]
    assert len(turns) == 2
    assert [[call.call_id for call in turn.tool_calls] for turn in turns] == [
        ["old-call"], ["new-call"],
    ]
