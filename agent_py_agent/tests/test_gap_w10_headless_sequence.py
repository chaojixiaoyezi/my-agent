"""连续操作走真实创建、控制入口和显示存储；供应商与控制运输不接外网。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.settings.model_scope import selected_model_scope
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from agent_py_agent.agent.subagents.services.lifecycle import SetStatusParams
from agent_py_agent.cli.chat_parts import tui_actions
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_decision_subagent import created, prepared  # noqa: F401
from agent_py_agent.tests.test_gap_w10_tui_status_thread_scoping import _usage
from agent_py_agent.tests.test_tui_control_delivery import _post_turn_params


@pytest.mark.parametrize("arrival", ["before_stop", "after_new"])
def test_spawn_compact_stream_steer_stop_new_next_turn(prepared, monkeypatch, arrival):  # noqa: F811
    agent, (explicit, _other), _ = prepared
    patch(agent, {"enabled": False})
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda _kind, config: SimpleNamespace(
        model_name=config.model_name))
    tid = agent._current_run_params.task_attributes["conversation_thread_id"]
    # 老fixture没有owner_home，真实/new继任必须带完整owner；仅补本例输入，不放宽原校验。
    agent.conversation_store.threads.update_atomic(tid, lambda thread: replace(
        thread, owner_id=agent.home_paths.owner_id, owner_home=str(agent.home_paths.owner_home_dir),
        channel_bindings=(ChannelBinding("chat", "w10-sequence", "alice", "alice", tid),)))
    thread_model_profile_id(agent, tid, select=explicit)
    runtime, steers, controls = TuiRuntime("w10-sequence"), [], []
    runtime.publish_conversation_reset(tid)
    params = _post_turn_params(runtime, steers)
    params.control_operation_reconciler = SimpleNamespace(enqueue_background=controls.append)
    runtime.register_gateway_request("gwreq-turn-1")
    turn = runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    target = GatewayExecutionTarget(tid, "request", "gwreq-turn-1", "claim-1").to_dict()
    runtime.publish_execution_target(target)
    with selected_model_scope(agent, thread_id=tid):
        children = created(agent, CreateSubagentsTool(agent).execute({"items": [
            {"goal": "检查第一部分"}, {"goal": "检查第二部分"},
        ]}))
        assert len(children) == 2
        assert all(agent.conversation_store.threads.require(child.agent_thread_id).model_profile_id == explicit
                   for child in children)
    assert turn.write_context_usage(_usage(tid))
    assert turn.write_model("正在生成")
    assert turn.on_gateway_event({"kind": "conversation_compacted", "thread_id": tid,
                                  "compact_generation": 1})
    assert turn.write_context_usage(_usage(tid, generation=1))

    def foreign_frame():
        before = runtime.store.snapshot().status
        assert not turn.on_gateway_event({"kind": "context_usage_updated", "thread_id": "foreign",
                                           "context_usage": _usage("foreign", generation=3, model="sol", window=500000)})
        assert runtime.store.snapshot().status == before

    if arrival == "before_stop":
        foreign_frame()
    assert tui_actions._tui_submit_active_turn_input(params, "补充核对", display_text="补充核对")
    assert steers[0].expected_turn_id == "gwreq-turn-1"
    assert tui_actions._tui_submit_control_operation(params, "/stop")
    assert controls[0].expected_turn_id == "gwreq-turn-1" and controls[0].command_kind == "stop"
    assert runtime.request_interrupt()
    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, interrupted=True))
    _reset_worker_refs(params)
    # 假发布器没有启动子进程；沿真实生命周期取消这些已创建任务，不把展示结束当执行完成。
    for child in children:
        agent.subagents.lifecycle.set_status(SetStatusParams(child.id, "CANCELLED"))
        assert str(agent.subagents.load(child.id).status) == "CANCELLED"
    successor = agent.conversation_store.threads.succeed(tid).thread_id
    runtime.publish_conversation_reset(successor)
    if arrival == "after_new":
        foreign_frame()
    assert not runtime.publish_execution_target(target)
    assert not tui_actions._tui_submit_active_turn_input(params, "下一轮", display_text="下一轮")
    tui_actions._tui_enqueue_job(params, "下一轮")
    job = params.jobs.get_nowait()
    assert job.user == "下一轮" and job.request_id not in {"chat-turn-1", "gwreq-turn-1"}
    next_turn = runtime.begin_turn(job.request_id)
    assert next_turn.write_context_usage(_usage(successor))
    status = runtime.store.snapshot().status
    assert status.thread_id == successor and status.compact_count == 0
    assert status.context_usage.context_window_tokens == 400000
    assert not turn.write_context_usage(_usage(tid, generation=3, model="sol", window=500000))
    assert runtime.store.snapshot().status == status
    runtime.complete_turn(job.request_id, TuiTurnSummary(response_text="下一轮完成"))
    view = runtime.store.snapshot()
    assert not view.has_active_work and not view.pending_steers and not view.queued_inputs
    assert runtime.execution_target() is None
    assert not runtime._turns and not runtime._retired_turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests
    assert not runtime._pending_steers and not runtime._deferred_background_events
