"""Jev 后台点位独立期限（2026-09-28，my-agent-1 实测：近 48 小时后台点位超时 39%）。

背景：同一后台阶段里，排在后面的点位只拿到阶段倒计时的残值，curator_relation 19 次超时的中位耗时只有 1930ms。
锁定：
- 普通后台（owner_background）阶段：每个点位从自己的开始时刻起算完整点位预算；拿到的建议在阶段预算到点后、
  点位期限前仍可采用；
- 前台（thread）阶段：仍受阶段总上限约束（用户在等），采用期限也不超过阶段上限；
- 调用方期限：建阶段时的 caller_deadline 与本次调用的 caller_deadline 更小时取更小的，前后台都一样；
- 实验阶段不论范围都保留阶段上限。
用假时钟替换决策服务的 time，用假调用边界记下每次发送的绝对期限；不访问真实供应商。
"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_protocol import questions
from agent_py_agent.tests.test_decision_service import successful
from agent_py_agent.tests.test_decision_settings import host_at, patch


# 函数用途: 建一个开了 curator、curator_relation 与 recall 的宿主：后台点位缺省 8 秒，前台单次 5 秒、阶段 1 秒；清掉冷却状态。
@pytest.fixture
def host(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    patch(host, {"enabled": True, "profile_id": key, "points.curator.mode": "apply",
                 "points.curator_relation.mode": "apply", "points.recall.mode": "apply",
                 "background_timeout_seconds": 8, "timeout_seconds": 5, "stage_timeout_seconds": 1})
    with policy._LOCK:
        policy._FAILURES.clear()
    return host


# 函数用途: 把决策服务的单调时钟换成可拨动的假时钟，并记下每次发送时交给调用边界的绝对期限。
@pytest.fixture
def clock(monkeypatch):
    now, sent = [100.0], []
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def invoke(*args, **kwargs):
        sent.append(kwargs["deadline"])
        return successful(*args, **kwargs)

    monkeypatch.setattr(calls, "invoke_decision_model_call", invoke)
    return now, sent


# 函数用途: 在给定（宿主、参数、阶段）上对一个点位请求建议；caller_deadline 只在测试需要时传。
def _decide(ctx, point, caller_deadline=None):
    host, params, stage = ctx
    return service.decide(host, params, stage, point=point, state={}, questions=questions(),
                          candidates_revision="deadline-test", caller_deadline=caller_deadline)


# 函数用途: 建一个前台会话身份，供 thread 范围阶段使用。
def _foreground_params(host):
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    return SimpleNamespace(request_id="req", run_id="run", task_id="task",
                           task_attributes={"conversation_thread_id": thread.thread_id})


def test_background_second_point_gets_its_full_point_budget(host, clock):
    now, sent = clock
    params = SimpleNamespace(run_id="background-run", task_attributes={})
    stage = service.begin_decision_stage(host, params, operation_id="bg-stage", scope="owner_background")
    assert stage.deadline == 108.0
    first = _decide((host, params, stage), "curator")
    now[0] = 106.0  # 第一个点位用掉 6 秒，阶段倒计时只剩 2 秒
    second = _decide((host, params, stage), "curator_relation")
    assert (first.status, second.status, second.may_apply) == ("success", "success", True)
    assert sent == [108.0, 114.0] and second.deadline == 114.0
    now[0] = 110.0  # 已过阶段预算，仍在第二个点位自己的期限内；第一个点位的建议按它自己的期限已到期
    assert service.decision_outcome_is_current(host, params, stage, second)
    assert not service.decision_outcome_is_current(host, params, stage, first)
    now[0] = 114.0
    assert not service.decision_outcome_is_current(host, params, stage, second)


def test_foreground_points_stay_under_the_stage_cap(host, clock):
    now, sent = clock
    params = _foreground_params(host)
    stage = service.begin_decision_stage(host, params, operation_id="fg-stage")
    assert stage.deadline == 101.0
    now[0] = 100.5
    outcome = _decide((host, params, stage), "recall")
    assert outcome.status == "success" and sent == [101.0]  # 点位预算 5 秒被阶段上限截到 101
    now[0] = 100.9
    assert service.decision_outcome_is_current(host, params, stage, outcome)
    now[0] = 101.0
    assert not service.decision_outcome_is_current(host, params, stage, outcome)


def test_smaller_caller_deadlines_still_win(host, clock):
    now, sent = clock
    params = SimpleNamespace(run_id="background-run", task_attributes={})
    stage = service.begin_decision_stage(host, params, operation_id="bg-lease", scope="owner_background",
                                         caller_deadline=103.0)
    ctx = (host, params, stage)
    _decide(ctx, "curator")  # 100 + 8 被建阶段时的调用方期限（如 Curator 租约）103 截住
    now[0] = 102.0
    _decide(ctx, "curator_relation")  # 102 + 8 仍被 103 截住：阶段级调用方期限对后面的点位同样有效
    now[0] = 102.5
    _decide(ctx, "curator", caller_deadline=102.8)  # 本次调用的期限更小
    fg_params = _foreground_params(host)
    fg_stage = service.begin_decision_stage(host, fg_params, operation_id="fg-caller")  # 阶段上限 103.5
    _decide((host, fg_params, fg_stage), "recall", caller_deadline=103.2)
    assert sent == [103.0, 103.0, 102.8, 103.2]


@pytest.mark.parametrize("scope,experiment,expected", [
    ("owner_background", False, 114.0), ("owner_background", True, 108.0), ("thread", False, 108.0),
])
def test_only_ordinary_background_stages_drop_the_stage_cap(scope, experiment, expected):
    stage = service.DecisionStage("op", "owner", "", "run", "", 100.0, 108.0, scope=scope, experiment=experiment)
    outcome = service.DecisionOutcome("apply", "success", deadline=114.0)
    assert service._point_deadline(stage, 106.0, 8.0, None) == expected
    assert service._adoption_deadline(stage, outcome) == expected
