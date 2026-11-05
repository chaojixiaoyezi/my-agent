"""R9-17：子代理容量已满是预期等待，不计执行失败；容量释放后由同一已受理任务继续。

真实 Gateway 请求、SessionTask、唤醒尝试账、后台调度与 create_subagents 容量规则；只有供应商与 IM 运输是记录型替身。
"""
from __future__ import annotations

import time
from dataclasses import replace
from threading import Event

from agent_py_agent.agent.conversation.execution_threads import IM_TASK_EXECUTION_KEY
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT_COUNT,
    WAKE_UNCOUNTED_STALL_SECONDS,
    WAKE_VERDICT_FAILURE,
    WAKE_VERDICT_NEUTRAL,
    WakePoisonState,
    next_poison_state,
    quarantine_decision,
    verdict_for_admission,
)
from agent_py_agent.agent.settings.deployment_policy import update_deployment_policy
from agent_py_agent.agent.settings.im_task_preferences import set_delegation_preference
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.cli.gateway_loops import _build_background_scheduler
from agent_py_agent.tests.test_im_delegation_single_speaker import _set_route, ask, ordinary_chain

# 相邻两拍间隔超过不计数退避上限（300 秒），每一拍这条唤醒都已到期。
_TICK_GAP_SECONDS = 400.0


# LLM: 原执行路径里容量等待在模型回合内抛出，默认部署重试阶梯会按文本把它当供应商过载原地退避约六分钟
#   （R3 范围外的原生阻塞，见交付记录）。这里经真实部署策略写口显式关闭该阶梯，执行路径本身不替换。
# 函数用途: 在 Agent 构造前为临时宿主写入"关闭供应商短链重试"的部署策略。
def _disable_provider_retry_ladder(tmp_path):
    host = replace(home_paths(tmp_path / "home"), owner_provider="local", owner_kind="main", owner_id="main")
    update_deployment_policy(trusted_host_paths=host, expected_revision=0,
                             changes={"runtime_guards": {"provider_transient_auto_resume_delays_seconds": []}})


def _thread_wakes(store, thread_id):
    return [signal for signal in store.wakes.pending(limit=0) if signal.thread_id == thread_id]


def _children_for(agent, task_id):
    return [run for run in agent.subagents.list_runs()
            if run.attributes.get("conversation_request_id") == task_id]


# LLM: 当前IM任务分派须先经可信route开关，委派偏好不能隐式开启路由；原五拍与同任务续跑断言不变。
# LLM: 测试屏障只延后真实孩子进入core，不替换容量裁决或执行结果；五拍后解除，必须由原runner完成。
# 函数用途: 在真正启用任务路由的普通链上核验容量等待与释放后的继续执行。
def test_five_capacity_waits_stay_uncounted_and_same_task_continues(tmp_path, monkeypatch, request):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.runtime_context import current_subagent_run_id
    from agent_py_agent.agent.subagents.manager import creation_capacity_counts

    _disable_provider_retry_ladder(tmp_path)
    agent, paths, _provider, calls, _ = ordinary_chain(tmp_path, monkeypatch)
    child_entered = Event()
    release_child = Event()
    request.addfinalizer(release_child.set)
    original_run = SimpleAgent.run

    # LLM: 保留真实run输入与返回，只让占位孩子等待显式测试事件；有界超时失败且异常退出亦释放。
    # 函数用途: 排除离线孩子在第五拍前已跑完的时序竞态。
    def run(current, prompt, *, params):
        if current_subagent_run_id(current):
            child_entered.set()
            if not release_child.wait(timeout=120):
                raise TimeoutError('capacity test child release was not signalled')
        return original_run(current, prompt, params=params)

    monkeypatch.setattr(SimpleAgent, 'run', run)
    assert agent.runtime_guard_policy.provider_transient_auto_resume_delays_seconds == ()
    store = agent.conversation_store
    set_delegation_preference(agent, "feishu", "ou_a", True)
    _set_route(agent, True)
    ask(agent, paths, "first", "整理工作区里的资料")
    scheduler = _build_background_scheduler(agent, agent.delivery_service)
    base = time.time()
    scheduler.tick(now=base)
    (first,) = store.session_tasks.list_report(limit=0)[0]
    (occupant,) = _children_for(agent, first.task_id)
    # tick 返回不代表异步 runner 已完成启动记账；先固定阻塞位置，再等权威容量收敛。
    # 不重试五拍中的失败，也不把 pending 索引当作正常容量等待。
    scheduler.prepare_tick(now=base + _TICK_GAP_SECONDS)
    assert child_entered.wait(timeout=30), 'capacity test child did not enter the run barrier'
    deadline = time.monotonic() + 30
    while True:
        try:
            settled = creation_capacity_counts(agent, '')[0] == 1
        except RuntimeError:
            settled = False
        if settled:
            break
        assert time.monotonic() < deadline, agent.subagents.load(occupant.id).status
        time.sleep(0.01)
    # 原 owner 策略只留一个子代理名额，已有孩子占满；第二个任务沿原受理路径进入后台。
    agent.owner_policy = replace(agent.owner_policy, max_subagents=1)
    ask(agent, paths, "second", "整理另一组资料")
    tasks, errors = store.session_tasks.list_report(limit=0)
    assert not errors
    (second,) = [task for task in tasks if task.task_id != first.task_id]
    (wake,) = _thread_wakes(store, second.target_thread_id)
    held, base = None, base + 2 * _TICK_GAP_SECONDS
    for index in range(5):
        before = len(calls)
        scheduler.tick(now=base + index * _TICK_GAP_SECONDS)
        state, error = store.wakes.attempts.state_report(wake.wake_signal_id)
        assert error is None
        # 每一拍都真的领到并走到容量准入，按预期等待记账，不是没轮到。
        assert [call for call in calls[before:] if call["thread"] == second.target_thread_id and not call["child"]]
        assert state.uncounted_count == index + 1, state
        assert state.uncounted_reason_code == "admission:im_subagent_capacity"
        assert (state.total_count, state.same_cause_count, state.reason_code) == (0, 0, "")
        assert quarantine_decision(state) is None
        current = store.session_tasks.load(second.task_id)
        assert current.status not in {"done", "failed", "cancelled"}
        assert current.task_run_id == second.task_run_id
        held = held or current.status
        assert current.status == held
        assert [signal.wake_signal_id for signal in _thread_wakes(store, second.target_thread_id)] == [wake.wake_signal_id]
        assert not _children_for(agent, second.task_id)
    assert store.wakes.attempts.quarantined_count() == 0

    # 已有孩子由原调度监督与 runner 正常跑完，名额自然释放；之后由同一任务、同一唤醒继续委派。
    release_child.set()

    deadline, index = time.monotonic() + 120, 5
    # 完成正文不等于canonical与索引均提交；先观察真实释放，避免把本测试混成pending-index并发故障测试。
    while True:
        try:
            settled = creation_capacity_counts(agent, '')[0] == 0
        except RuntimeError:
            settled = False
        if settled and agent.subagents.load(occupant.id).status == 'DONE':
            break
        assert time.monotonic() < deadline, agent.subagents.load(occupant.id).status
        time.sleep(0.05)
    while not _children_for(agent, second.task_id):
        assert time.monotonic() < deadline, agent.subagents.load(occupant.id).status
        scheduler.tick(now=base + index * _TICK_GAP_SECONDS)
        index += 1
        time.sleep(0.05)
    assert agent.subagents.load(occupant.id).status == "DONE"
    (child,) = _children_for(agent, second.task_id)
    thread = store.threads.require(second.target_thread_id)
    assert thread.metadata[IM_TASK_EXECUTION_KEY]["child_run_ids"] == [child.id]
    resumed = store.session_tasks.load(second.task_id)
    assert (resumed.task_id, resumed.task_run_id, resumed.status) == (second.task_id, second.task_run_id, held)
    assert wake.wake_signal_id not in {signal.wake_signal_id for signal in store.wakes.pending(limit=0)}
    state, error = store.wakes.attempts.state_report(wake.wake_signal_id)
    assert error is None and state == WakePoisonState()
    assert store.wakes.attempts.quarantined_count() == 0


def test_capacity_wait_is_neutral_while_other_admissions_still_count():
    assert verdict_for_admission("im_subagent_capacity").kind == WAKE_VERDICT_NEUTRAL
    for code in ("host_delivery_consumed", "wake_source_unreadable", "im_subagent_capacity_v2"):
        assert verdict_for_admission(code).kind == WAKE_VERDICT_FAILURE


def test_capacity_wait_keeps_configured_neutral_stall_closeout():
    verdict = verdict_for_admission("im_subagent_capacity")
    state, now = WakePoisonState(), 1_000.0
    for _ in range(WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
        state = next_poison_state(state, verdict, now=now)
        now = state.next_attempt_at
    # 短时间连续等待只是退避，不结案、不计失败。
    assert (state.total_count, state.same_cause_count) == (0, 0)
    assert quarantine_decision(state) is None
    # 原 24 小时不计数窗口仍按原规则结案，原因保留为这次等待。
    state = next_poison_state(state, verdict, now=state.neutral_since + WAKE_UNCOUNTED_STALL_SECONDS)
    decision = quarantine_decision(state)
    assert decision is not None and decision.reason_code == "admission:im_subagent_capacity"
    assert (decision.total_count, decision.same_cause_count) == (0, 0)
