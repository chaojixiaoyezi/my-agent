"""G1/G6（2026-11-18）：持续目标续跑被扣下时要让用户看见；熔断只认成功的工具调用。

G1：后台一片结束、结束原因（blocked/error/max-tokens 等）扣下续跑资格时，Goal 仍是 active 却不会再有下一片。
改前 continue_goal_after_report 直接返回，用户什么也看不到。现在宿主按结构化原因排一条固定文案的提示
（来源 goal_continuation、代码 GOAL_CONTINUATION_WITHHELD，同代码只留一条），可外发通道再主动推一次；
不读模型正文或显示快照。以下不提示：用户输入让步、用户自己停止、技术截断已接管下一片、子代理仍在跑或状态未知、
目标/任务已不匹配或不再 active、车道上有用户回合。真实链路钉两条：Gateway /ask 往返带出并只带一次；
流式→插话→/stop→/new→闲聊时，新会话的一问一答只调一次模型、不带旧会话的提示，旧提示也不被停止的回合取走。
G6：调用全部失败的片不算进展（以前任意调用都清零空转计数，同一失败可无限续跑）；只读成功照常算进展。
"""
from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_execution
from agent_py_agent.agent.conversation.background_goal import (
    GoalContinuationDependencies,
    continue_goal_after_report,
)
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    NO_PROGRESS_NOTICE_SOURCE,
    NO_PROGRESS_REASON_CODE,
    WAKE_SNAPSHOT_KEY,
    WITHHELD_REASON_CODE,
    goal_progress_snapshot,
    goal_withheld_notice,
)
from agent_py_agent.agent.conversation.goal_runtime import raise_goal_continuation_wake
from agent_py_agent.agent.conversation.host_notices import pending_host_notices, queue_host_notice
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport, WakeSignal
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.http_handlers import _public_result
from agent_py_agent.tests.test_gateway_goal_fuse_reset import (  # noqa: F401
    _ask,
    _background_slice,
    world,
)

_SIGNAL = WakeSignal(wake_signal_id="wake-7", thread_id="thread-1", root_task_id="task-1", metadata={"goal_id": "goal-1"})


# 函数用途: 造一个 wake 精确匹配的目标。
def _goal(**fields) -> SimpleNamespace:
    return SimpleNamespace(**{"goal_id": "goal-1", "task_id": "task-1", "thread_id": "thread-1", "status": "active",
                              "revision": 4, **fields})


# 函数用途: 造一片后台报告；默认是续跑资格被 blocked 扣下的一片。
def _report(**fields) -> BackgroundMainAgentReport:
    values = {"goal_continuation_allowed": False, "turn_end_reason": "blocked", "runtime_status": "blocked", **fields}
    return BackgroundMainAgentReport(thread_id="thread-1", task_id="task-1", reason="thread_goal_continue", response="",
                                     route_channel="", route_target="", created_at=1.0, **values)


# 函数用途: 组装只记录提示与唤醒的续跑依赖；各项可按用例替换。
def _dependencies(notices: list, raised: list, **fields) -> GoalContinuationDependencies:
    goal = fields.pop("goal", _goal())
    values = {
        "goals": SimpleNamespace(load=lambda _thread, goal_id="": goal,
                                 record_continuation_fuse=lambda _request: (goal, False)),
        "tasks": SimpleNamespace(update_status=lambda _request: None),
        "goal_clock": SimpleNamespace(),
        "task_status": lambda _thread, _task: "active",
        "subagent_phase": lambda _task: ("no_subagents", ""),
        "raise_wake": lambda *args, **kwargs: raised.append(args),
        "task_registry": lambda: None,
        "user_turn_gate": lambda _thread_id: nullcontext(False),
        "queue_withheld_notice": lambda goal, signal, report: notices.append((goal, signal, report)),
        **fields,
    }
    return GoalContinuationDependencies(**values)


def test_withheld_continuation_queues_one_notice_and_raises_no_wake():
    notices, raised = [], []
    report = _report()
    continue_goal_after_report(_dependencies(notices, raised), _SIGNAL, report=report, now=2.0)
    assert raised == [] and len(notices) == 1
    goal, signal, seen = notices[0]
    assert goal.goal_id == "goal-1" and signal is _SIGNAL and seen is report
    # 有续跑资格时照常发下一片，不提示。
    notices, raised = [], []
    continue_goal_after_report(_dependencies(notices, raised), _SIGNAL, report=_report(goal_continuation_allowed=True,
                                                                                      turn_end_reason="completed"), now=3.0)
    assert notices == [] and len(raised) == 1


# 函数用途: 子代理状态读取失败（未知）。
def _phase_raises(_task):
    raise OSError("subagent store unreadable")


@pytest.mark.parametrize(("report_fields", "dependency_fields"), [
    pytest.param({"runtime_reason": "USER_INPUT_YIELD", "turn_end_reason": "interrupted"}, {}, id="user_input_yield"),
    pytest.param({"turn_end_reason": "aborted"}, {}, id="user_stop"),
    pytest.param({}, {"subagent_phase": lambda _task: ("subagents_active", "")}, id="subagents_active"),
    pytest.param({}, {"subagent_phase": lambda _task: ("subagent_state_unknown", "run_index_unreadable")},
                 id="subagent_state_unknown"),
    pytest.param({}, {"subagent_phase": _phase_raises}, id="subagent_phase_raises"),
    pytest.param({}, {"goal": _goal(status="paused")}, id="goal_paused"),
    pytest.param({}, {"goal": _goal(goal_id="goal-2")}, id="other_goal"),
    pytest.param({}, {"goal": _goal(task_id="task-2")}, id="task_rebound"),
    pytest.param({}, {"task_status": lambda _thread, _task: "interrupted"}, id="task_not_active"),
    pytest.param({}, {"user_turn_gate": lambda _thread_id: nullcontext(True)}, id="user_turn_on_lane"),
])
def test_withheld_notice_is_not_queued_when_the_next_step_belongs_elsewhere(report_fields, dependency_fields):
    notices, raised = [], []
    dependencies = _dependencies(notices, raised, **dependency_fields)
    continue_goal_after_report(dependencies, _SIGNAL, report=_report(**report_fields), now=2.0)
    assert notices == [] and raised == []


def test_notice_text_names_structured_reasons_or_says_they_are_unavailable():
    notice = goal_withheld_notice(_goal(), _SIGNAL, _report(turn_end_reason="error", runtime_reason="PROVIDER_TIMEOUT",
                                                            runtime_status="failed"))
    assert (notice.source, notice.code) == (NO_PROGRESS_NOTICE_SOURCE, WITHHELD_REASON_CODE)
    assert "（turn_end_reason=error，runtime_reason=PROVIDER_TIMEOUT，runtime_status=failed）" in notice.text
    assert "/goal resume" in notice.text and "/goal pause" in notice.text and "直接回复" in notice.text
    assert dict(notice.details) == {"goal_id": "goal-1", "goal_revision": "4", "wake_signal_id": "wake-7",
                                    "turn_end_reason": "error", "runtime_reason": "PROVIDER_TIMEOUT",
                                    "runtime_status": "failed"}
    # 冻结重投等没有新执行的报告三项都空：如实写“不可取得”，不猜。
    frozen = goal_withheld_notice(_goal(), _SIGNAL, _report(turn_end_reason="", runtime_status=""))
    assert "（结束原因不可取得）" in frozen.text


# 函数用途: 在真实 GoalStore 上建一个绑定了 active 任务的目标。
def _store_goal(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread_id = store.threads.get_or_create({"canonical_user_id": "owner-a"}).thread_id
    goal = store.goals.create({"thread_id": thread_id, "objective": "持续推进同一件事"})
    store.tasks.bind({"thread_id": thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active"})
    return store, thread_id, goal


def test_all_failed_slices_trip_the_fuse_but_successful_read_only_slices_never_do(tmp_path):
    store, thread_id, goal = _store_goal(tmp_path)
    paused: list = []
    dependencies = GoalContinuationDependencies(
        goals=store.goals, tasks=store.tasks, goal_clock=store.goal_clock,
        task_status=lambda _thread, _task: "active", subagent_phase=lambda _task: ("no_subagents", ""),
        raise_wake=lambda *args, **kwargs: None, task_registry=lambda: None, goal_continuation_idle_limit=3,
        queue_no_progress_notice=lambda current: paused.append(current) or True,
    )

    # 函数用途: 按当前目标快照跑一片，calls 次调用里 successes 次成功（只读，无物化进展）。
    def run(index: int, *, calls: int, successes: int) -> None:
        current = store.goals.load(thread_id, goal_id=goal.goal_id)
        signal = WakeSignal(wake_signal_id=f"wake-{index}", thread_id=thread_id, root_task_id=goal.task_id,
                            metadata={"goal_id": goal.goal_id, WAKE_SNAPSHOT_KEY: goal_progress_snapshot(current, "active")})
        report = replace(_report(goal_continuation_allowed=True, turn_end_reason="completed"), thread_id=thread_id,
                         task_id=goal.task_id, tool_call_count=calls, tool_success_count=successes)
        continue_goal_after_report(dependencies, signal, report=report, now=float(10 + index))

    for index in range(5):
        run(index, calls=1, successes=1)
    assert store.goals.load(thread_id, goal_id=goal.goal_id).status == "active" and paused == []
    for index in range(5, 8):
        run(index, calls=2, successes=0)
    updated = store.goals.load(thread_id, goal_id=goal.goal_id)
    assert updated.status == "paused" and len(paused) == 1
    assert updated.metadata["goal_continuation_fuse"]["reason_code"] == NO_PROGRESS_REASON_CODE


def test_withheld_notice_reaches_the_next_gateway_reply_once(world, monkeypatch):  # noqa: F811
    # 真实后台调度器跑一片，结束原因为 blocked：续跑资格被扣下，目标仍 active、不再有唤醒。
    monkeypatch.setattr(background_execution, "result_turn_end_reason", lambda _result: "blocked")
    raise_goal_continuation_wake(world.store, world.goal, now=100.0)
    _background_slice(world, 101.0)
    assert world.store.wakes.pending() == []
    assert world.store.goals.load(world.thread_id, goal_id=world.goal.goal_id).status == "active"
    pending = pending_host_notices(world.store, world.thread_id)
    assert [(notice.source, notice.code) for notice in pending] == [(NO_PROGRESS_NOTICE_SOURCE, WITHHELD_REASON_CODE)]
    notice = pending[0]
    assert "turn_end_reason=blocked" in notice.text and dict(notice.details)["goal_id"] == world.goal.goal_id

    # 经 submit_gateway_ask + _process_gateway_requests 的真实往返：下一条回复带出一次（原始与公开结果一致），随即已读。
    response = _ask(world.agent, world.paths, "现在进展如何")
    assert response["ok"] and response["channel_delivery"]["host_notices"] == [notice.to_dict()]
    assert _public_result(response)["channel_delivery"]["host_notices"] == [notice.to_dict()]
    assert pending_host_notices(world.store, world.thread_id) == ()
    again = _ask(world.agent, world.paths, "再问一句")
    assert again["ok"] and "host_notices" not in again["channel_delivery"]


def test_cutoff_takeover_owns_the_next_slice_and_queues_no_withheld_notice(world, monkeypatch):  # noqa: F811
    # 同一片结束时技术截断回执仍 pending：下一片归截断续跑（宿主发表它的唤醒），不进目标续跑判定，也就不提示“没接上”。
    from agent_py_agent.agent.conversation import technical_continuation as cutoff

    monkeypatch.setattr(background_execution, "result_turn_end_reason", lambda _result: "blocked")
    thread = world.store.threads.require(world.thread_id)
    receipt = cutoff.CutoffReceipt(
        world.agent.home_paths.owner_id, world.thread_id, world.goal.task_id, thread.last_user_message_id,
        "request-cut", "run-cut", "attempt-cut", 1, "TOOL_ROUND_LIMIT_REACHED", "interrupted", "pending",
        world.goal.goal_id,
    )
    cutoff._write(world.store, world.goal.task_id, lambda _link: receipt)
    raise_goal_continuation_wake(world.store, world.goal, now=100.0)
    _background_slice(world, 101.0)
    assert [wake.reason for wake in world.store.wakes.pending()] == [cutoff.WAKE_REASON]
    assert world.store.goals.load(world.thread_id, goal_id=world.goal.goal_id).status == "active"
    assert pending_host_notices(world.store, world.thread_id) == ()


def test_headless_stream_steer_stop_new_then_casual_question_carries_no_withheld_notice(tmp_path, monkeypatch):
    """原 claim/worker/control/stream 往返；仅外部模型用有界 Event 后端。"""
    from uuid import uuid4

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import control_service, request_worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=False)
    agent = SimpleAgent(config, tmp_path)
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "transport": "offline", "model_backend": "echo", "model_name": "headless-local",
        "model_context_window_tokens": 200_000,
    }})
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    store = agent.conversation_store
    scope = control_service.GatewayControlScope("tester", "chat", "sequence")
    conversation = {"channel": "chat", "channel_conversation_id": "sequence", "channel_user_id": "tester",
                    "canonical_user_id": "tester"}
    old_thread = store.threads.get_or_create(conversation).thread_id
    streaming, release = threading.Event(), threading.Event()
    errors, completed, calls = [], [], []

    # 函数用途: 第一次调用流式输出后阻塞到测试放行；之后的调用直接给完整回答。
    def generate(backend, prompt, on_chunk=None, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            on_chunk("正在生成原始响应。" * 20)
            streaming.set()
            assert release.wait(10), "test provider wait must remain bounded"
            return ModelResponse(text="停止前的迟到结果", backend=backend.name)
        return ModelResponse(text="下一轮的完整回答", backend=backend.name)

    # 函数用途: 往 Gateway 收件箱放一条同会话请求。
    def enqueue(rid):
        path = paths.inbox / (rid + ".json")
        write_json_file_atomic(path, {"id": rid, "kind": "ask", "goal": "验证连续交互", "status": "pending",
                                      "verbose_level": "stream", "user_id": "tester",
                                      "metadata": {"channel": "chat", "user_id": "tester"}, "conversation": conversation})
        return path

    warmup = enqueue("sequence-warmup")
    assert request_worker._process_gateway_request_path(agent, paths, warmup, "sequence-warmup")
    notice = goal_withheld_notice(_goal(thread_id=old_thread), SimpleNamespace(wake_signal_id="wake-old"),
                                  _report(turn_end_reason="error", runtime_status="failed"))
    assert queue_host_notice(store, old_thread, notice, replace_same_code=True)
    monkeypatch.setattr(EchoBackend, "generate", generate)
    first = enqueue("sequence-first")

    # 函数用途: 在后台线程里跑会被插话和 /stop 打断的那一轮。
    def worker():
        try:
            completed.append(request_worker._process_gateway_request_path(agent, paths, first, "sequence-worker"))
        except BaseException as error:  # noqa: BLE001 - 交给主线程断言
            errors.append(error)
        finally:
            streaming.set()

    running = threading.Thread(target=worker, daemon=True)
    running.start()
    try:
        assert streaming.wait(10) and not errors, errors
        steer_scope = replace(scope, metadata={"message_id": "sequence-steer", "expected_turn_id": first.stem})
        steer = control_service.steer_active_conversation_if_running(agent, paths, message="补充要求", scope=steer_scope)
        assert steer is not None and steer.ok
        stopped = control_service.execute_gateway_conversation_control(agent, paths, parse_conversation_control("/stop"),
                                                                       scope)
        assert stopped.ok, stopped
    finally:
        release.set()
        running.join(10)
    assert not running.is_alive() and not errors and completed == [True], errors
    # 被停止的回合不取走旧会话的待送达提示，也不留下悬空的活动任务。
    assert pending_host_notices(store, old_thread) == (notice,)
    assert not store.threads.require(old_thread).active_task_ids
    assert control_service.execute_gateway_conversation_control(agent, paths, parse_conversation_control("/new"),
                                                                scope).ok
    following = enqueue("sequence-next")
    assert request_worker._process_gateway_request_path(agent, paths, following, "sequence-successor")
    answer = read_json_file(paths.responses / following.name)
    new_thread = store.threads.resolve(channel="chat", channel_conversation_id="sequence",
                                       channel_user_id="tester").thread_id
    # 新会话的闲聊一问一答：只调一次模型，不带旧会话的提示；旧提示原样留在旧会话。
    assert answer["ok"] and answer["response"] == "下一轮的完整回答" and len(calls) == 2
    assert "host_notices" not in answer.get("channel_delivery", {})
    assert new_thread != old_thread and not store.threads.require(new_thread).active_task_ids
    assert json.dumps(answer, ensure_ascii=False).count(notice.text) == 0
    assert pending_host_notices(store, old_thread) == (notice,)
