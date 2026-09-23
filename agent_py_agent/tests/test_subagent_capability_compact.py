"""child 同一业务 turn 的原展示载体：真实运行、快照和 Compact，供应商仅用本地 fake。"""
from contextlib import nullcontext
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.subagent import run_flow
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.tests.test_decision_capability_consumer import (
    provider,
)
from agent_py_agent.tests.test_decision_capability_consumer import (
    surface as decision_surface,  # noqa: F401
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_decision_skill_projection import setup_surface
from agent_py_agent.tests.test_gateway_conversation_compact import _agent
from agent_py_agent.tests.test_subagent_runtime_compact import _OverflowThenCompleteChildBackend
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# LLM: 复用原配置、目录、快照和 Agent.run；fake 后端只制造一次 provider overflow 并返回摘要及最终回复，禁止真实网络。
# 函数用途: 为 child 和后台测试提供同一批真实可发现工具和技能，比较最终 provider 输入而非手拼预期 renderer。
@pytest.fixture
def capability_host(tmp_path, tool_surface, skill_catalog_factory):  # noqa: F811
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.config.tool_context_ptl_retry_max = 0
    registry, _, first, second, _ = tool_surface
    catalog, builder = setup_surface(tmp_path / "capabilities", skill_catalog_factory)
    builder.config = agent.config
    agent.tools, agent.prompts, agent.capability_router = registry, builder, catalog.router
    agent.current_skill_snapshot = lambda: catalog.snapshot
    agent.skill_snapshot_for_run_scope = lambda _: catalog.snapshot
    registry.register(SkillSearchTool(agent))
    backend = _OverflowThenCompleteChildBackend()
    backend.model_name = "test-model"
    backend.context_window_tokens = 1_000_000
    agent.backend = backend
    profile, _ = decision(agent)
    patch(agent, {"enabled": True, "profile_id": profile, "points.skill_tool.mode": "apply"})
    return SimpleNamespace(agent=agent, backend=backend, first=first, second=second)


# LLM: 旧历史只写入原 child/main thread，使用完成轮 metadata；不伪造 Compact checkpoint 或展示选择。
# 函数用途: 让真实 transcript Compact 有一轮可压缩材料。
def append_prior_turn(agent, thread_id):
    for role in ("user", "assistant"):
        agent.conversation_store.messages.append({"thread_id": thread_id, "role": role,
            "content": "核对来源事实" + role, "metadata": {"conversation_request_id": "prior-turn"}})


# LLM: 观察真实宿主参数进入 Agent.run 的状态，callback 对原对象的后续更新不能改写入口快照。
# 函数用途: 同时记录初始载体和最终参数，区分未评估、已评估但无建议、显式空选择及新轮。
def capture_host_runs(monkeypatch, agent):
    captured = []
    original = agent.run

    def run(prompt, *, params):
        captured.append((params, params.capability_presentation, params.capability_presentation_evaluated,
                         params.capability_presentation_turn_id))
        return original(prompt, params=params)

    monkeypatch.setattr(agent, "run", run)
    return captured


# LLM: 生产派工入口负责身份和权限；测试只向模型提供普通任务，未把 carrier 或窗口结论塞入 task attrs。
# 函数用途: 建立带旧轮历史的 canonical child，供原启动链完整执行。
def child_task(fixture, *, prior=True):
    task = fixture.agent.subagents.create_run(goal="核对来源后汇报事实", role="worker",
        allowed_tools=[fixture.first.model_spec.name, fixture.second.model_spec.name, "skill_search", "tool_search", "list_tools"])
    if prior:
        append_prior_turn(fixture.agent, task.agent_thread_id)
    return task


# LLM: provider 输入来自真实 normal/summary renderer，测试仅比较缓存前缀、原 schema 和 typed 动态卡片。
# 函数用途: 检验 Compact 与随后模型重试使用同一个展示面，且不会提前读 Skill 正文或执行工具。
def assert_retained_provider_surface(fixture, *, empty=False, required=False):
    backend = fixture.backend
    assert len(backend.model_prompts) == 2 and len(backend.summary_prompts) == 1
    layouts = [prompt_cache_layout(prompt) for prompt in (*backend.model_prompts, *backend.summary_prompts)]
    assert layouts[0].stable_prefix == layouts[1].stable_prefix == layouts[2].stable_prefix
    assert backend.model_kwargs[0]["tools"] == backend.model_kwargs[1]["tools"] == backend.summary_kwargs[0]["tools"]
    text_blocks = [[block["text"] for message in kwargs["messages"]
                    for block in message["content"] if block["type"] == "text"]
                   for kwargs in (*backend.model_kwargs, *backend.summary_kwargs)]
    cards = next(text for text in text_blocks[2] if text.startswith("# Recommended Tools"))
    assert cards in text_blocks[0] and cards in text_blocks[1]
    assert ("workspace:method-001" in cards) is (not empty or required)
    assert "method-059" not in layouts[0].stable_prefix and "BODY-ONLY" not in cards
    assert fixture.first.calls == fixture.second.calls == 0


@pytest.mark.parametrize("choice", [None, "not_needed"])
def test_child_same_turn_reuses_selection_and_exact_provider_surface(capability_host, monkeypatch, choice):
    fixture = capability_host
    task = child_task(fixture)
    calls = provider(monkeypatch, choice=choice)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    result = fixture.agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok, result
    assert len(calls) == 1 and len(captured) == 2
    first, second = captured
    assert first[1:3] == (None, False)
    assert first[3] == second[3] == first[0].attempt_id == second[0].attempt_id
    assert second[1] is not None and second[2] is True
    assert second[1].selected_skill_ids == (() if choice else ("workspace:method-001",))
    assert fixture.agent.conversation_store.threads.load(task.agent_thread_id).compact_generation == 1
    assert_retained_provider_surface(fixture, empty=bool(choice), required=bool(second[1].required_skill_ids))
    assert "capability_presentation" not in str(asdict(result))
    assert "capability_presentation" not in str(fixture.agent.subagents.load(task.id).attributes)


@pytest.mark.parametrize("mode", ["off", "observe", "failed"])
def test_child_evaluated_without_selection_never_redecides_after_compact(capability_host, monkeypatch, mode):
    fixture = capability_host
    task = child_task(fixture)
    if mode != "failed":
        patch(fixture.agent, {"points.skill_tool.mode": mode})
    calls = provider(monkeypatch, fail=RuntimeError("本地 fake 决策失败") if mode == "failed" else None)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    original = fixture.backend.generate

    def generate(prompt, **kwargs):
        response = original(prompt, **kwargs)
        if fixture.backend.summary_prompts:
            patch(fixture.agent, {"points.skill_tool.mode": "apply"})
        return response

    monkeypatch.setattr(fixture.backend, "generate", generate)
    result = fixture.agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok and len(captured) == 2
    assert len(calls) == (mode != "off")
    assert captured[0][1:3] == (None, False)
    assert captured[1][1:3] == (None, True)
    assert captured[0][3] == captured[1][3]
    assert prompt_cache_layout(fixture.backend.model_prompts[0]).stable_prefix == prompt_cache_layout(fixture.backend.model_prompts[1]).stable_prefix
    assert fixture.backend.model_kwargs[0]["tools"] == fixture.backend.model_kwargs[1]["tools"]


def test_child_compact_clears_stale_selection_even_when_connection_recovers(capability_host, monkeypatch):
    fixture = capability_host
    task = child_task(fixture)
    calls = provider(monkeypatch)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    original = fixture.backend.generate

    def generate(prompt, **kwargs):
        response = original(prompt, **kwargs)
        if response.runtime_status == "context_overflow":
            fixture.backend.api_base = "https://changed.example.test"
        elif fixture.backend.summary_prompts:
            if hasattr(fixture.backend, "api_base"):
                del fixture.backend.api_base
        return response

    monkeypatch.setattr(fixture.backend, "generate", generate)
    result = fixture.agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok and len(calls) == 1 and len(captured) == 2
    # 延迟 Compact 在第二次真实准备中清除失效面；不为入口快照提前重跑准备。
    assert captured[1][0].capability_presentation is None
    assert captured[1][0].capability_presentation_evaluated is True
    historical_cards = [[block["text"] for message in kwargs["messages"] for block in message["content"]
                         if block["type"] == "text" and block["text"].startswith("# Recommended Tools")]
             for kwargs in fixture.backend.model_kwargs]
    cards = [items[-1] for items in historical_cards]
    assert historical_cards[1][0] == historical_cards[0][0]
    assert len(historical_cards[1]) == 2
    assert "workspace:method-001" in cards[0] and "workspace:method-001" not in cards[1]
    # 摘要复用真实恢复请求，包括原builder的无候选占位段；失效卡片不能复活。
    summary_cards = [block["text"] for message in fixture.backend.summary_kwargs[0]["messages"]
                     for block in message["content"]
                     if block["type"] == "text" and block["text"].startswith("# Recommended Tools")]
    assert summary_cards == [cards[1]]
    assert "workspace:method-001" not in summary_cards[0]


def test_child_next_goal_turn_resets_presentation_with_the_same_attempt(capability_host, monkeypatch):
    from agent_py_agent.agent.backends.base import ModelResponse

    fixture = capability_host
    fixture.backend.overflow_once = False
    task = child_task(fixture, prior=False)
    store = fixture.agent.conversation_store
    store.goals.create({"thread_id": task.agent_thread_id, "task_id": task.id, "objective": "完成两段核对"})
    calls = provider(monkeypatch)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    original = fixture.backend.generate

    def generate(prompt, **kwargs):
        response = original(prompt, **kwargs)
        if len(fixture.backend.model_prompts) == 1:
            return ModelResponse(text="第一段已核对。", backend=fixture.backend.name)
        goal = store.goals.load(task.agent_thread_id)
        store.goals.update({"thread_id": task.agent_thread_id, "goal_id": goal.goal_id, "status": "complete"})
        return response

    monkeypatch.setattr(fixture.backend, "generate", generate)
    result = fixture.agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok and len(captured) == len(calls) == 2
    assert captured[0][1:3] == captured[1][1:3] == (None, False)
    assert captured[0][0].attempt_id == captured[1][0].attempt_id
    assert captured[0][3] != captured[1][3]
    assert captured[1][3] == captured[0][3] + "-goal-1"
    rows = store.messages.recent(task.agent_thread_id, limit=0)
    finals = [row for row in rows if row.metadata.get("assistant_part_id") == "final"]
    assert len(finals) == 2 and len({row.metadata["conversation_request_id"] for row in finals}) == 2


def test_child_compact_cancellation_does_not_retry_or_redecide(capability_host, monkeypatch):
    fixture = capability_host
    task = child_task(fixture)
    calls = provider(monkeypatch)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    monkeypatch.setattr(run_flow, "is_interrupted", lambda: bool(fixture.backend.model_prompts))
    result = fixture.agent.run_subagent(task.id, dry_run=False, probe=False)
    assert not result.ok
    assert len(captured) == len(calls) == len(fixture.backend.model_prompts) == 1
    assert fixture.backend.summary_prompts == []
    assert fixture.agent.conversation_store.threads.load(task.agent_thread_id).compact_generation == 0


def test_child_second_prepare_uses_cleared_current_params_instead_of_old_frozen_surface(decision_surface, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.capability import decision_recommendation as recommendations
    from agent_py_agent.agent.conversation.agent_thread import AgentThreadTurnContext
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        prepare_conversation_compact_provider_surface,
    )
    from agent_py_agent.tests.test_gateway_capability_compact import compact_surface

    fixture = decision_surface
    fixture.params.capability_presentation_turn_id = "child-turn"
    calls = provider(monkeypatch)
    selected = recommendations.recommend_capabilities(fixture.host, fixture.params, fixture.snapshot, fixture.contract)
    params = RunParams()
    run_flow._bind_subagent_presentation(params, None, "child-turn")
    params.capability_presentation_callback(selected.selection)
    frozen = compact_surface(fixture, selected.selection, params.capability_presentation_callback)
    fixture.host.backend.api_base = "https://changed.example.test"
    first = prepare_conversation_compact_provider_surface(fixture.host, frozen, run_id="summary-operation")
    assert params.capability_presentation is None and params.capability_presentation_evaluated is True
    assert first.volatile_sections == () and frozen.capability_presentation is selected.selection
    del fixture.host.backend.api_base
    observed = []
    current = AgentThreadTurnContext(fixture.params.task_attributes["agent_thread_id"], 0, False, "")

    # LLM: 本用例只隔离真实 HTTP 回归已经覆盖的恢复提交；第二次准备在CAS前，仍使用已清展示载体。
    # 函数用途: 捕获下一真实恢复准备，证明配置恢复不会让旧 frozen surface 再次显示名卡。
    def prepare(agent, task, *, turn, model_surface, **kwargs):
        assert turn.turn_id == "child-turn"
        assert kwargs["defer_compact"] is True
        observed.append(model_surface.capability_presentation)
        actual = prepare_conversation_compact_provider_surface(agent, model_surface, run_id="summary-operation")
        assert actual.volatile_sections == ()
        return AgentThreadTurnContext(
            current.thread_id, 0, False, "", compact_source=SimpleNamespace(messages=()),
        )

    monkeypatch.setattr(run_flow, "prepare_subagent_thread_turn", prepare)
    monkeypatch.setattr(model_selection, "canonical_subagent_model_scope", lambda *_: nullcontext())
    request = run_flow.SubagentOverflowCompactRequest("核对来源", "attempt", {}, current,
        [{"call_id": "already-carried"}], None, frozen,
        conversation_turn_id="child-turn", run_params=params, defer_compact=True)
    refreshed = run_flow._compact_subagent_overflowing_turn(fixture.host, SimpleNamespace(id="child"), request)
    assert refreshed.compact_generation == 0 and refreshed.compact_source is not None
    assert observed == [None] and len(calls) == 1
    assert params.capability_presentation is None and params.capability_presentation_evaluated is True


@pytest.mark.parametrize("allowed", [None, []])
def test_child_preflight_without_run_params_has_no_evaluated_carrier(allowed):
    context = SimpleNamespace(run_id="child", agent_name="worker", role="worker", allowed_tools=allowed, context_bundle={})
    surface = run_flow._subagent_compact_model_surface(context)
    assert surface.allowed_tools == (None if allowed is None else ())
    assert surface.presentation_context is None and surface.capability_presentation is None
    assert surface.capability_presentation_turn_id == "" and surface.capability_presentation_callback is None
