"""observe 采样开关：默认关时行为与原来完全一致；打开后只给 observe 点位按自然小时限流。

覆盖（2026-09-28 dev 派活的任务 4）：
- 开关登记为决策通用布尔字段；
- 开关关：无论成功多少次都不跳过；
- 开关开：本小时成功满 N 次起跳过，并给出结构化原因码 observe_sampled_out；
- 失败/超时不占名额（只有成功路径记样本）；
- 跨自然小时重新计数；
- apply 点位不受影响；
- 实验路径不做采样；
- 样本计数不改变 reach_counts 的到达/调用/未调用诊断口径。

记账点：真实成功路径在 `decision_service._invoke_call` 的 success 分支调用 `note_observe_sample_success`。
下面 decide 级用例把「真正发模型请求」换成返回合法响应的替身，其余（期限、身份复核、采样判定、记账、结果组装）都是真实现。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import decision_point_limits as limits
from agent_py_agent.agent.conversation import decision_reach_counts as counts
from agent_py_agent.agent.conversation.decision_reach_counts import (
    decision_reach_summary,
    miss_reason_label,
    note_decision_reach,
    note_observe_sample_success,
    observe_sample_success_count,
)
from agent_py_agent.agent.conversation.decision_service import (
    DecisionOutcome,
    DecisionStage,
    _decide_outcome,
    _DecideCall,
    _observe_sampled_out,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    GENERAL_FIELDS,
    decision_field_schema,
    validate_decision_field,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError

POINT = "delivery_quality"


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(counts, "_PENDING", {})
    monkeypatch.setattr(counts, "_LAST_FLUSH", {})


# LLM: 最小宿主只需要 home_paths 里的计数文件路径、config_dir 与 owner 身份三元组（decision_owner_ref 要用）；
#   它不代表真实 HomePaths，测试不得据此断言生产路径解析。
# 函数用途: 构造带规范计数路径的最小宿主。
def _agent(tmp_path):
    home = SimpleNamespace(
        owner_decision_reach_counts_json=tmp_path / "decision" / "reach_counts.json",
        config_dir=tmp_path, owner_provider="local", owner_kind="main", owner_id="local",
    )
    return SimpleNamespace(home_paths=home, config=SimpleNamespace(decision_skip_records_enabled=True))


# 函数用途: 造一份 effective 视图（含采样开关与点位模式），与真实投影的字段形状一致。
def _settings(*, enabled: bool, mode: str = "observe"):
    return {"effective": {"observe_sampling_enabled": enabled, "points": {POINT: {"effective_mode": mode}}}}


# 函数用途: 造宿主与保真身份参数对；身份 helper 只读结构化字段，owner 引用由真实 decision_owner_ref 算出。
def _host(tmp_path):
    from agent_py_agent.agent.conversation.decision_service import decision_owner_ref

    agent = _agent(tmp_path)
    params = SimpleNamespace(
        thread_id="thread", run_id="run", task_id="task",
        task_attributes={"agent_thread_id": "thread", "agent_run_id": "run", "agent_task_id": "task"},
    )
    stage = DecisionStage("op", decision_owner_ref(agent), "thread", "run", "task",
                          time.monotonic(), time.monotonic() + 5.0, scope="thread")
    return agent, params, stage


# 函数用途: 造与本次请求绑定一致的响应，供替身模型调用返回（不联网）。
def _stub_response(request, backend):
    from agent_py_agent.agent.backends.decision_protocol import DecisionResponse

    return DecisionResponse(request.binding, request.input_digest, backend.model_name, backend.model_name, (),
                            b'{"input_tokens":1,"output_tokens":1}', usage_reported=True)


# 函数用途: 造一次 decide 调用的打包参数；state/questions 必须是 JSON 数据，否则 DecisionRequest 会判 invalid_input。
def _call(stage, *, experiment: bool = False) -> _DecideCall:
    return _DecideCall(stage, POINT, {"note": "sampling-test"}, {"q1": "?"}, "rev", ("source-ref",), None, False)


# LLM: 把“真正发模型请求”这一步换成返回合法响应的替身；其余 _invoke_call 步骤保持真实现。
# 函数用途: 给一个用例装好 decide 的替身链路（路由、连接摘要、冷却、后端解析、模型调用）。
def _patch_decide(monkeypatch, agent, settings, row):
    import agent_py_agent.agent.conversation.decision_model_call as dmc
    from agent_py_agent.agent.conversation import decision_service as svc

    monkeypatch.setattr(svc, "_route", lambda *a, **k: ("observe", None, (settings, row, "rev", object())))
    # 本用例只验采样：把“设置仍有效/连接摘要”固定住，避免真实现去读盘上的会话设置（需要 conversation_store）。
    monkeypatch.setattr(svc, "_snapshot", lambda *a, **k: (settings, row, "rev", SimpleNamespace()))
    monkeypatch.setattr(svc, "_stale", lambda *a, **k: "")
    monkeypatch.setattr(svc, "connection_revision", lambda config: "conn")
    monkeypatch.setattr(svc, "cooldown_state", lambda key, revision, *, retry=False: (False, 0.0))
    monkeypatch.setattr(svc, "decision_backend_from_profile", lambda config: SimpleNamespace(model_name="m"))
    monkeypatch.setattr(dmc, "invoke_decision_model_call",
                        lambda agent_arg, params_arg, request, backend, **kwargs: _stub_response(request, backend))


def test_sampling_switch_is_a_registered_general_boolean_field():
    assert "observe_sampling_enabled" in GENERAL_FIELDS
    assert decision_field_schema("observe_sampling_enabled") == {"type": "boolean"}
    assert validate_decision_field("observe_sampling_enabled", True) is True
    assert validate_decision_field("observe_sampling_enabled", False) is False
    with pytest.raises(ModelProfileError):
        validate_decision_field("observe_sampling_enabled", "yes")


def test_sampling_off_never_skips_even_with_many_successes(tmp_path):
    agent = _agent(tmp_path)
    for _ in range(limits.OBSERVE_SAMPLED_SUCCESS_LIMIT * 5):
        note_observe_sample_success(agent, POINT)
    row = {"effective_mode": "observe"}
    assert _observe_sampled_out(agent, POINT, row, _settings(enabled=False)) is False


def test_seventh_successful_call_is_skipped_and_reported_with_a_reason(tmp_path, monkeypatch):
    """前 N 次成功照常调用；第 N+1 次采样判定命中，decide 整体不调用模型并带回结构化原因。"""
    agent, params, stage = _host(tmp_path)
    row = {"effective_mode": "observe", "timeout_seconds": 5.0, "profile_id": "profile"}
    settings = _settings(enabled=True)
    _patch_decide(monkeypatch, agent, settings, row)

    for _ in range(limits.OBSERVE_SAMPLED_SUCCESS_LIMIT):
        assert _observe_sampled_out(agent, POINT, row, settings) is False
        outcome = _decide_outcome(agent, params, _call(stage))
        assert (outcome.status, outcome.mode) == ("success", "observe"), outcome.reason

    assert _observe_sampled_out(agent, POINT, row, settings) is True
    calls: list[int] = []
    from agent_py_agent.agent.conversation import decision_service as svc

    monkeypatch.setattr(svc, "_invoke", lambda *a, **k: calls.append(1) or DecisionOutcome("observe", "success"))
    skipped = _decide_outcome(agent, params, _call(stage))
    assert calls == []
    assert (skipped.status, skipped.reason, skipped.mode) == ("skipped", "observe_sampled_out", "observe")
    assert skipped.may_apply is False
    assert "样本" in miss_reason_label("observe_sampled_out")


def test_failures_and_timeouts_do_not_consume_the_quota(tmp_path):
    agent = _agent(tmp_path)
    for _ in range(50):
        note_decision_reach(agent, POINT, "provider_failed")
        note_decision_reach(agent, POINT, "deadline")
    assert observe_sample_success_count(agent.home_paths, POINT) == 0
    assert _observe_sampled_out(agent, POINT, {"effective_mode": "observe"}, _settings(enabled=True)) is False
    # 失败/超时只能落在原因码键上，不能混进成功样本键（否则失败会吃掉观察名额）。
    summary = decision_reach_summary(agent.home_paths, since=time.time() - 3600)["points"][POINT]
    assert summary["called"] == 0
    assert {item["reason"] for item in summary["not_called"]} == {"provider_failed", "deadline"}


def test_sample_quota_boundary_is_exactly_the_named_constant(tmp_path):
    """钉住边界本身：满 N 次仍不跳，第 N+1 次才跳；否则阈值写错 1 也测不出来。"""
    agent = _agent(tmp_path)
    row = {"effective_mode": "observe"}
    settings = _settings(enabled=True)
    for index in range(limits.OBSERVE_SAMPLED_SUCCESS_LIMIT):
        assert _observe_sampled_out(agent, POINT, row, settings) is False, f"第 {index + 1} 次就跳了，阈值偏小"
        note_observe_sample_success(agent, POINT)
    assert observe_sample_success_count(agent.home_paths, POINT) == limits.OBSERVE_SAMPLED_SUCCESS_LIMIT
    assert _observe_sampled_out(agent, POINT, row, settings) is True


def test_observe_sample_limit_is_the_frozen_six(tmp_path):
    """上限是内部常量：值本身被钉住，避免有人顺手调大/调小而没人发现。"""
    assert limits.OBSERVE_SAMPLED_SUCCESS_LIMIT == 6


def test_quota_resets_in_the_next_natural_hour(tmp_path):
    agent = _agent(tmp_path)
    now = time.time()
    for _ in range(limits.OBSERVE_SAMPLED_SUCCESS_LIMIT):
        note_observe_sample_success(agent, POINT)
    assert observe_sample_success_count(agent.home_paths, POINT, now=now) == limits.OBSERVE_SAMPLED_SUCCESS_LIMIT
    assert observe_sample_success_count(agent.home_paths, POINT, now=now + 3600) == 0


def test_apply_point_is_not_sampled(tmp_path):
    agent = _agent(tmp_path)
    for _ in range(limits.OBSERVE_SAMPLED_SUCCESS_LIMIT + 3):
        note_observe_sample_success(agent, POINT)
    assert _observe_sampled_out(agent, POINT, {"effective_mode": "apply"}, _settings(enabled=True, mode="apply")) is False


def test_experiment_stage_is_not_sampled(tmp_path, monkeypatch):
    """实验路径不做 observe 采样：采样判定只在普通路由里被问一次。

    实验调用走 `decision_experiment` 的独立路由（不在本用例范围内）；产品代码里该分支由 `not stage.experiment`
    明确排除，因此这里断言的是「判定函数只在普通路径被读一次」，不伪造实验调用链。
    """
    agent, params, stage = _host(tmp_path)
    row = {"effective_mode": "observe", "timeout_seconds": 5.0, "profile_id": "profile"}
    settings = _settings(enabled=True)
    from agent_py_agent.agent.conversation import decision_service as svc

    reads: list[int] = []
    monkeypatch.setattr(svc, "observe_sample_success_count", lambda *a, **k: reads.append(1) or 999)
    assert _observe_sampled_out(agent, POINT, row, settings) is True
    assert reads == [1]  # 普通 observe 点位会读样本计数
    assert _observe_sampled_out(agent, POINT, {"effective_mode": "apply"}, _settings(enabled=True, mode="apply")) is False
    assert reads == [1]  # apply 与实验一样不读样本计数



def test_sample_counter_does_not_change_reach_diagnostics(tmp_path):
    agent = _agent(tmp_path)
    note_decision_reach(agent, POINT, "memory_count")
    for _ in range(4):
        note_observe_sample_success(agent, POINT)
    row = decision_reach_summary(agent.home_paths, since=time.time() - 3600)["points"][POINT]
    assert (row["reached"], row["called"]) == (1, 0)
    assert row["not_called"] == [{"reason": "memory_count", "label": miss_reason_label("memory_count"), "count": 1}]
