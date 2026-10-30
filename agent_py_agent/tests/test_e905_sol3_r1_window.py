# LLM: 只替换决策开关与阶段上下文；目录、候选解析、任务配置投影及材料构建走原入口，不发送网络请求。
# 模块用途: 回归子代理模型准备入口对未知窗口、混合窗口和真实小窗口的保留语义，防止异常被吞后丢失整批建议。
from __future__ import annotations

import time
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.orchestration import decision_subagent as selection
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation


# LLM: 宿主和服务商只属于临时目录，使用虚构凭据；不替换候选、模型解析、后端工厂或正式准备逻辑。
# 函数用途: 构造离线回归上下文，让正式准备入口能够走到真实候选请求限制与序列化路径。
@pytest.fixture
def window_host(tmp_path, monkeypatch):
    agent = SimpleNamespace(
        config=AgentConfig(enable_tools=False, max_tokens=32),
        home_paths=SimpleNamespace(
            root=tmp_path,
            config_dir=tmp_path / "config",
            owner_provider="local",
            owner_kind="user",
            owner_id="window-regression",
        ),
        subagents=SimpleNamespace(workspace_root=tmp_path),
        _current_run_params=SimpleNamespace(task_attributes={}),
    )
    execute_model_profile_operation(agent, "save_provider", {
        "provider_id": "window-provider",
        "provider": {
            "display_name": "窗口回归服务商",
            "api_base": "https://example.test/v1",
            "api_key": "window-test-only-not-a-real-key",
            "capabilities": ["agentic"],
        },
    })
    stage = SimpleNamespace(
        deadline=time.monotonic() + 30,
        enabled_points=("subagent_model",),
        error_code="",
    )

    # LLM: 仅提供已启用的点位设置，不替代候选范围或模型配置；修复前仍须经过真实的失败路径。
    # 函数用途: 隔离与窗口缺陷无关的决策设置读取，保留正式准备入口的点位判断。
    def read_enabled_settings(*_args, **_kwargs):
        return {
            "effective": {"points": {"subagent_model": {
                "effective_mode": "apply", "candidate_profile_ids": [],
            }}},
            "revision": {"owner": 1, "thread": 0},
        }

    # LLM: 阶段只提供期限和已启用点位；不会发出决策调用或授权真实子代理创建。
    # 函数用途: 给正式准备入口返回固定的测试阶段，避免引入与窗口无关的运行身份依赖。
    def begin_stage(*_args, **_kwargs):
        return stage

    monkeypatch.setattr(selection, "execute_decision_settings_operation", read_enabled_settings)
    monkeypatch.setattr(selection.decision_service, "begin_decision_stage", begin_stage)
    child = SimpleNamespace(id="window-child", goal="检查窗口容量", agent_thread_id="window-thread")
    return agent, selection.SubagentModelInput(raw={"goal": child.goal}, prepared=child, canonical=child)


# LLM: 通过真实模型目录管理保存 v6 原始窗口，不手工构造候选字典，不使用旧扁平导入兼容接口。
# 函数用途: 新增一个独立候选并返回其稳定编号，供准备入口解析配置及持久代次。
def _add_window_profile(agent, window):
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "save_model", {
        "profile_id": profile_id,
        "profile": {
            "provider_id": "window-provider",
            "model_backend": "openai_compatible",
            "model_name": f"window-{profile_id}",
            "model_context_window_tokens": window,
        },
    })
    return profile_id


# LLM: 不调用私有准备函数规避吞异常边界；必须返回完整建议对象，并逐一核对公开题目和本地冻结快照。
# 函数用途: 从正式准备入口取得全部候选，确认容量原值贯穿候选、请求限制、题目与状态序列化。
def _prepare_and_assert_windows(window_host, windows):
    agent, child = window_host
    expected = {_add_window_profile(agent, window): window for window in windows}
    prepared = selection.prepare_subagent_model_decision(
        agent, {"goal": child.prepared.goal}, partial(list, [child]),
    )
    assert isinstance(prepared, selection.SubagentModelDecision), "正式准备入口不得吞异常后丢弃整批建议"
    assert set(prepared.candidates) == set(expected)
    assert set(prepared.questions) == {"0"}
    assert set(prepared.children) == {"0"}
    assert prepared.state["selection_scope"] == "new_children_pending_validation"
    limits = prepared.children["0"]["candidate_request_limits"]
    assert set(limits) == set(expected)
    for profile_id, window in expected.items():
        rows = (
            prepared.candidates[profile_id],
            limits[profile_id],
            prepared.questions["0"]["criteria"][profile_id],
            prepared.state["children"]["0"]["candidate_request_limits"][profile_id],
        )
        for row in rows:
            assert row["context_window_tokens"] == window
            if window is None:
                assert row["context_window_tokens"] is None
            else:
                assert type(row["context_window_tokens"]) is int
        if window is None:
            assert limits[profile_id]["output_cap_tokens"] is None
            assert limits[profile_id]["output_cap_status"] == "unknown"


# LLM: 单候选未知容量仍能产生待验证题目；不能用默认容量替代未知，也不能靠吞异常沿用原方案假装通过。
# 函数用途: 验证单个空窗口候选可以完整通过正式子代理模型准备入口。
def test_prepare_single_null_window_keeps_unknown(window_host):
    _prepare_and_assert_windows(window_host, [None])


# LLM: 空容量不能污染另一候选或让整批消失；分别覆盖空候选在前和在后的遍历顺序。
# 函数用途: 验证未知窗口与已知窗口在同一批准备材料中分别保留原值。
@pytest.mark.parametrize("windows", [(None, 96_000), (96_000, None)], ids=["null-first", "known-first"])
def test_prepare_mixed_null_and_known_windows(window_host, windows):
    _prepare_and_assert_windows(window_host, windows)


# LLM: v6 的正整数小窗口是真实声明，覆盖下界、内部值及旧下限前一值，禁止夹到四千零九十六。
# 函数用途: 验证显式小于旧下限的正整数窗口不会在正式准备入口被放大。
@pytest.mark.parametrize("window", [1, 127, 4095])
def test_prepare_small_positive_window_is_not_enlarged(window_host, window):
    _prepare_and_assert_windows(window_host, [window])
