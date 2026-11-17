"""S11 用户消息优先：后台定时工作片在已完成的工具轮边界看到同会话有用户回合排队时让出车道，
不再调用模型；同一定时执行停在 waiting，由续跑唤醒在用户回合之后接着做，不占技术续跑次数。"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.turn_trigger import (
    HOST_EVENT_FIRST_LINE_USER_INPUT_YIELD,
    USER_INPUT_YIELD_TRIGGER_REASON,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    run_claim,
)
from agent_py_agent.agent.conversation.background_tool_policy import BackgroundToolPolicyRequest
from agent_py_agent.agent.conversation.runtime import (
    _background_run_allowed_tools,
    _wake_signal_should_skip,
    background_prompt,
)
from agent_py_agent.agent.conversation.technical_continuation import (
    USER_INPUT_YIELD_REASON,
    USER_INPUT_YIELD_WAKE_REASON,
    clear_user_input_yield_parked,
    mark_user_input_yield_parked,
    park_for_user_input,
    user_input_yield_parked_task_id,
    yields_to_user_input,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_context
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_scheduler_runtime import _create_job, _RetryDelivery


@contextmanager
def _user_turn_waiting(store, thread_id):
    key = run_claim._user_input_lane_key(store, thread_id)
    run_claim._register_user_input_turn(key)
    try:
        yield
    finally:
        run_claim._adjust_user_input_turns(key, -1)


def _agent(tmp_path, **config):
    return SimpleAgent(AgentConfig(memory_path="memory.jsonl", orphan_supervision_interval_seconds=0, **config),
                       tmp_path)


def _slice_params(thread_id, task_id, *, source="background_main_agent", scope="default", run_id=None):
    attrs = {"conversation_thread_id": thread_id, "conversation_task_id": task_id}
    if run_id is not None:
        attrs["scheduler_run_id"] = run_id
    return SimpleNamespace(source=source, context_scope=scope, task_attributes=attrs)


def _thread(agent, conversation="chat-yield"):
    return agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "feishu", "channel_conversation_id": conversation,
        "channel_user_id": "open-1", "now": 900.0,
    })


def test_only_scheduled_background_slices_yield_and_only_while_a_user_turn_waits(tmp_path):
    agent = _agent(tmp_path)
    thread = _thread(agent)
    scheduled = _slice_params(thread.thread_id, "srun_1", run_id="srun_1")
    assert yields_to_user_input(agent, scheduled) is False
    with _user_turn_waiting(agent.conversation_store, thread.thread_id):
        assert yields_to_user_input(agent, scheduled) is True
        # 前台回合、子代理、非定时后台片（如技术续跑链、Goal 片）照旧跑完。
        assert yields_to_user_input(agent, _slice_params(thread.thread_id, "srun_1", source="gateway",
                                                         run_id="srun_1")) is False
        assert yields_to_user_input(agent, _slice_params(thread.thread_id, "srun_1", scope="task_local",
                                                         run_id="srun_1")) is False
        assert yields_to_user_input(agent, _slice_params(thread.thread_id, "task-1")) is False
        assert yields_to_user_input(agent, _slice_params(thread.thread_id, "task-1", run_id="srun_1")) is False
        assert yields_to_user_input(agent, _slice_params("other-thread", "srun_1", run_id="srun_1")) is False
    off = _agent(tmp_path / "off", technical_cutoff_continuation_enabled=False)
    off_thread = _thread(off)
    with _user_turn_waiting(off.conversation_store, off_thread.thread_id):
        assert yields_to_user_input(off, _slice_params(off_thread.thread_id, "srun_1", run_id="srun_1")) is False


# 函数用途: 建一个真实定时 job、真实调度与后台运行时，模型替身按次序返回 read_file 工具调用或最终回复。
def _scheduled_case(tmp_path, monkeypatch, *, tool_rounds):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = _thread(agent)
    owner_home = agent.home_paths.owner_home_dir
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "notes.txt").write_text("monitor input\n", encoding="utf-8")
    _create_job(agent, thread.thread_id, "monitor")
    agent.scheduler_service.enqueue_ready_runs(now=1_000)
    signal = store.wakes.pending()[0]
    channels = _RetryDelivery()
    channels.status = "sent"
    calls: list[dict] = []
    case = SimpleNamespace(agent=agent, store=store, thread=thread, signal=signal, channels=channels, calls=calls,
                           on_call=None, tool_rounds=tool_rounds)

    class _Backend:
        name = "s11-yield"

        def probe_tool_capability(self):
            return ProviderToolCapability(provider="synthetic", endpoint="local://test", model="test",
                                          stream=False, native_supported=True, evidence="synthetic")

        def generate(self, prompt, **kwargs):
            calls.append({"prompt": prompt, "kwargs": kwargs})
            if case.on_call is not None:
                case.on_call(len(calls))
            if len(calls) <= case.tool_rounds:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": f"read-{len(calls)}", "name": "read_file", "input": {"path": "notes.txt"}}])
            return ModelResponse(text="巡检完成。", backend=self.name)

    agent.backend = _Backend()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    case.scheduler = BackgroundMainAgentScheduler({
        "runtime": runtime, "store": store, "scheduler_service": agent.scheduler_service,
    })
    case.run_id = signal.root_task_id
    return case


def _run_status(case):
    run = case.agent.scheduler_repository.get_active_run(case.run_id)
    if run is not None:
        return str(run["status"])
    history, errors = case.agent.scheduler_repository.history(limit=10)
    assert errors == []
    return next(str(row["status"]) for row in history if row["run_id"] == case.run_id)


def _yield_wakes(case):
    return [wake for wake in case.store.wakes.pending(limit=0) if wake.reason == USER_INPUT_YIELD_WAKE_REASON]


def test_without_a_waiting_user_the_scheduled_slice_runs_to_its_natural_end(tmp_path, monkeypatch):
    case = _scheduled_case(tmp_path, monkeypatch, tool_rounds=3)
    case.scheduler.tick(now=1_001)
    assert len(case.calls) == 4
    assert _yield_wakes(case) == []
    assert _run_status(case) == "done"
    assert len(case.channels.attempts) == 1


def test_a_waiting_user_turn_gets_the_lane_within_one_tool_round(tmp_path, monkeypatch):
    case = _scheduled_case(tmp_path, monkeypatch, tool_rounds=30)
    key = run_claim._user_input_lane_key(case.store, case.thread.thread_id)

    def user_arrives(call_count):
        # 第 2 次模型调用（返回第 2 个工具调用）时用户消息到达并在车道上排队。
        if call_count == 2:
            run_claim._register_user_input_turn(key)

    case.on_call = user_arrives
    try:
        case.scheduler.tick(now=1_001)
        # 第 2 轮工具执行完就让出：没有第 3 次模型调用，30 轮的工作片不再把用户挡在车道外。
        assert len(case.calls) == 2
        link = case.store.tasks.load(case.run_id)
        assert link is not None and link.status == "active"
        assert _run_status(case) == "waiting"
        assert case.channels.attempts == []
        [wake] = _yield_wakes(case)
        assert wake.root_task_id == case.run_id and wake.thread_id == case.thread.thread_id
        assert wake.metadata["scheduler_run_id"] == case.run_id and "owner_delivery" not in wake.metadata
        # 让出片仍持车道时已把本执行标记为停放：排队的用户回合随后读到的线程带着这条标记。
        assert user_input_yield_parked_task_id(case.store.threads.require(case.thread.thread_id)) == case.run_id
        # 用户回合还在车道上：续跑不领取、不消费。
        case.scheduler.tick(now=1_002)
        assert len(case.calls) == 2 and _yield_wakes(case) == [wake]
    finally:
        run_claim._adjust_user_input_turns(key, -1)
    case.on_call = None
    case.tool_rounds = 2  # 续跑片第一次模型调用就给出最终回复。
    case.scheduler.tick(now=1_003)
    assert len(case.calls) == 3
    continuation = json.dumps(case.calls[2]["kwargs"].get("messages"), ensure_ascii=False)
    assert "持久计划的续跑" in continuation and "执行 monitor" in continuation
    assert HOST_EVENT_FIRST_LINE_USER_INPUT_YIELD in continuation and '"child"' not in continuation
    # 续跑片接着同一回合：上一片的两次工具调用随本片恢复，不从头重做。
    assert '"read-1"' in continuation and '"read-2"' in continuation
    assert case.store.tasks.load(case.run_id).status != "active"
    assert _run_status(case) == "done"
    assert len(case.channels.attempts) == 1 and _yield_wakes(case) == []
    # 续跑片没再让出：停放标记撤下。
    assert user_input_yield_parked_task_id(case.store.threads.require(case.thread.thread_id)) == ""
    history, errors = case.agent.scheduler_repository.history(limit=10)
    [row] = [item for item in history if item["run_id"] == case.run_id]
    # 定时执行的最终回执是续跑片的真实回复，不是让出片的空正文。
    assert not errors and row["response"] == "巡检完成。" and row["delivery_status"] == "sent"


def test_yield_continuation_is_stale_once_the_scheduled_task_is_no_longer_active(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = _thread(agent)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "srun_x", "goal": "执行 x", "now": 1_000})
    wake = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": "srun_x", "urgency": "normal",
        "reason": USER_INPUT_YIELD_WAKE_REASON, "dedupe_key": "user-input-yield:srun_x:w1",
        "metadata": {"scheduler_run_id": "srun_x"},
    })
    assert _wake_signal_should_skip(agent, store, wake, USER_INPUT_YIELD_WAKE_REASON) is False
    forged = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": "srun_x", "urgency": "normal",
        "reason": USER_INPUT_YIELD_WAKE_REASON, "dedupe_key": "user-input-yield:srun_x:w2",
        "metadata": {"scheduler_run_id": "srun_other"},
    })
    assert _wake_signal_should_skip(agent, store, forged, USER_INPUT_YIELD_WAKE_REASON) is True
    store.tasks.update_status({"task_id": "srun_x", "status": "blocked", "now": 1_010})
    assert store.tasks.load("srun_x").status == "blocked"
    assert _wake_signal_should_skip(agent, store, wake, USER_INPUT_YIELD_WAKE_REASON) is True


def test_continuation_keeps_the_scheduled_tool_catalog_and_names_the_plan():
    request = BackgroundToolPolicyRequest(reason=USER_INPUT_YIELD_WAKE_REASON)
    assert _background_run_allowed_tools(None, None, request) is None
    prompt = background_prompt(USER_INPUT_YIELD_WAKE_REASON,
                               wake_signal={"metadata": {"scheduler_prompt": "执行 monitor"}})
    assert "持久计划的续跑" in prompt and prompt.endswith("执行 monitor")
    assert USER_INPUT_YIELD_REASON == "USER_INPUT_YIELD"
    assert USER_INPUT_YIELD_TRIGGER_REASON == USER_INPUT_YIELD_WAKE_REASON


@pytest.mark.parametrize("reason", ["TOOL_ROUND_LIMIT_REACHED", USER_INPUT_YIELD_REASON])
def test_yield_does_not_count_as_a_technical_cutoff(reason):
    from agent_py_agent.agent.conversation.technical_continuation import _SOURCES

    assert (reason in _SOURCES) is (reason != USER_INPUT_YIELD_REASON)


# 函数用途: 按网关普通回合的方式（无精确绑定、无 Goal）为线程选择本轮工作区任务。
def _select_workspace(store, thread_id, **request):
    errors = request_context._GatewayLoadErrors()
    selected = request_context._gateway_workspace_task(store, store.threads.require(thread_id), None, errors,
                                                       **request)
    assert not errors
    return selected


def test_a_parked_scheduled_task_is_not_adopted_by_the_next_user_turn(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = _thread(agent)
    workspace = tmp_path / "srun-workspace"
    workspace.mkdir()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "srun_p", "goal": "执行 p", "now": 1_000,
                      "task_path": str(workspace)})
    # 原行为：线程上唯一的 active 定时链接会被下一条普通用户回合当成本轮任务接管。
    assert _select_workspace(store, thread.thread_id).task_id == "srun_p"
    assert mark_user_input_yield_parked(store, thread.thread_id, "srun_p") is True
    # 停放期间普通用户回合不接管它；用户回合自己建任务，定时执行等续跑接着做。
    assert _select_workspace(store, thread.thread_id) is None
    assert store.tasks.load("srun_p").status == "active"
    # 精确绑定到该任务的请求（恢复、重放）照旧选中。
    exact = {"id": "req-1", "conversation_runtime": {"request_id": "req-1", "thread_id": thread.thread_id,
                                                     "task_id": "srun_p"}}
    assert _select_workspace(store, thread.thread_id, request=exact, request_id="req-1").task_id == "srun_p"
    # 只撤下同一任务的标记。
    assert clear_user_input_yield_parked(store, thread.thread_id, "srun_other") is True
    assert _select_workspace(store, thread.thread_id) is None
    assert clear_user_input_yield_parked(store, thread.thread_id, "srun_p") is True
    assert user_input_yield_parked_task_id(store.threads.require(thread.thread_id)) == ""
    assert _select_workspace(store, thread.thread_id).task_id == "srun_p"


def test_a_slice_that_cannot_record_the_park_does_not_yield(tmp_path, monkeypatch, caplog):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = _thread(agent)
    params = _slice_params(thread.thread_id, "srun_1", run_id="srun_1")

    def refuse(*_args, **_kwargs):
        raise OSError("thread file unavailable")

    monkeypatch.setattr(store.threads, "update_atomic", refuse)
    from agent_py_agent.agent.agent_core._tool_loop_service import _yield_lane_to_user_input

    with _user_turn_waiting(store, thread.thread_id), caplog.at_level(
            logging.WARNING, logger="agent_py_agent.agent.conversation.technical_continuation"):
        assert yields_to_user_input(agent, params) is True
        # 写不下停放标记就不让出：本片照旧跑完，用户回合不会接管一条没有标记的 active 定时任务。
        assert park_for_user_input(agent, params) is False
        assert _yield_lane_to_user_input(agent, params) is False
    assert "USER_INPUT_YIELD_PARK_FAILED" in caplog.text


def _history_row(message_id, role, content, **metadata):
    return SimpleNamespace(message_id=message_id, role=role, content=content, metadata=metadata, channel_message_id="")


def test_the_parked_run_partial_rows_stay_out_of_the_user_turn_history():
    from agent_py_agent.agent.conversation.history_projection import history_row_selected

    rows = [
        _history_row("m1", "user", "之前的问题"),
        _history_row("m2", "assistant", "已读取 sample_01，继续读 sample_02。", task_id="srun_p",
                     conversation_request_id="background-turn-1"),
        _history_row("m3", "assistant", "上一次定时执行的结论。", task_id="srun_done"),
    ]
    parked = request_context._gateway_history_source(rows, "gwreq-user", None, parked_task_id="srun_p")
    assert [row.message_id for row in parked.rows] == ["m1", "m3"]
    # 没有停放标记（或续跑结束撤下标记）时，同一条记录照常进入历史。
    plain = request_context._gateway_history_source(rows, "gwreq-user", None)
    assert [row.message_id for row in plain.rows] == ["m1", "m2", "m3"]
    assert history_row_selected(rows[1], current_request_id="gwreq-user", work_scope=None) is True
    assert history_row_selected(rows[1], current_request_id="gwreq-user", work_scope=None, parked_task_id="srun_p") is False
