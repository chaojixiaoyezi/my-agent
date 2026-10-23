"""PR07/09：按最终记忆信封计费；工具预加载只改展示，不扩大快照或调用权限。

复现：在仓库根运行 python3 -m pytest agent_py_agent/tests/test_ctxopt_loop_support.py -q。
"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.conversation.authority import CONVERSATION_BACKGROUND_EVENT_REASON_ATTR
from agent_py_agent.agent.conversation.background_tool_policy import (
    BACKGROUND_CONTINUATION_REQUIRED_TOOLS,
)
from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text


# LLM: 构造公开渲染器可读取的已审核历史条目，不依赖持久记忆库或当前用户数据。
# 函数用途: 用含转义字符和元数据的条目暴露只计算正文的漏算。
def _record(index, kind="fact", content=None):
    return SimpleNamespace(entry_id=f"entry-{index}", kind=kind, role="system", updated_at=1.0,
                           attributes={"origin": "reviewed"},
                           content=content if content is not None else '核对 "资料" & 证据。\n' * 12)


@pytest.mark.parametrize("kind", ["lesson", "working_agreement", "fact"])
def test_formal_memory_context_counts_serialized_envelope_and_metadata(kind):
    records = [_record(index, kind) for index in range(70)]
    kept = loop_support._budgeted_formal_memories(records)
    limit = (loop_support._WORKING_AGREEMENT_SUB_BUDGET_CHARS if kind == "working_agreement"
             else loop_support._FORMAL_MEMORY_BUDGET_CHARS)
    assert kept and len(kept) < len(records)
    assert len(memory_context_text(kept)) <= limit


def test_formal_memory_context_preserves_hot_priority_and_one_fitting_record():
    hot = _record("hot", "hot", "高频规则" * 30)
    lesson = _record("lesson", "lesson", "明确证据" * 1700)
    kept = loop_support._budgeted_formal_memories([lesson, hot])
    assert kept == [hot, lesson]
    assert len(memory_context_text(kept)) <= loop_support._FORMAL_MEMORY_BUDGET_CHARS
    oversized_hot = _record("large-hot", "hot", "高频" * 6000)
    assert loop_support._budgeted_formal_memories([_record(1), oversized_hot]) == [oversized_hot]
    assert loop_support._budgeted_formal_memories([]) == []


# LLM: 只测试现有首轮展示计算入口；快照许可为显式集合，线程与恢复名单独立传递。
# 函数用途: 提供最小运行值对象，避免把 fake 注册成功误当原生工具展示成功。
def _loaded(monkeypatch, *, prompt="继续核对", source="cli", attrs=None, allowed=None,
            selected=(), carried=(), thread=(), methods=(), available=None):
    from agent_py_agent.agent.capability import method_carry

    available = set(available if available is not None else {
        *BACKGROUND_CONTINUATION_REQUIRED_TOOLS, "scripts", "workflows", "custom_plugin",
    })
    monkeypatch.setattr(loop_support, "thread_loaded_tools", lambda *_: set(thread))
    monkeypatch.setattr(method_carry, "prompt_method_ids", lambda *_: tuple(methods))
    params = SimpleNamespace(source=source, allowed_tools=allowed, user_prompt=prompt,
                             task_attributes=attrs or {})
    snapshot = SimpleNamespace(available_tool_names=frozenset(available), runtimes=(),
                               presentation_deferred_names=frozenset({"custom_plugin"}))
    seed = SimpleNamespace(effective_contract_snapshot=None, selected_skill_ids=selected,
                           tool_runtime_snapshot=snapshot)
    result = loop_support._initial_loop_loaded_tool_names(
        SimpleNamespace(), params, seed, SimpleNamespace(loaded_tool_names=set(carried)))
    assert params.allowed_tools is allowed
    assert snapshot.available_tool_names == frozenset(available)
    return result


@pytest.mark.parametrize("allowed", [None, sorted(BACKGROUND_CONTINUATION_REQUIRED_TOOLS)])
def test_background_context_preloads_focused_tools_not_permission_ceiling(monkeypatch, allowed):
    loaded = _loaded(monkeypatch, source="background_main_agent", allowed=allowed,
                     attrs={CONVERSATION_BACKGROUND_EVENT_REASON_ATTR: "scheduled_progress_report"})
    assert {"task_progress", "read_file", "read_artifact", "tool_search", "list_tools"} <= loaded
    assert "watch_stream" not in loaded and "package_build" not in loaded
    assert len(loaded) < len(BACKGROUND_CONTINUATION_REQUIRED_TOOLS)


def test_background_context_lifecycle_and_explicit_task_tools_are_preloaded(monkeypatch):
    loaded = _loaded(monkeypatch, source="background_main_agent", attrs={
        CONVERSATION_BACKGROUND_EVENT_REASON_ATTR: "subagent_runner_finished",
        "required_tool_names": ["custom_plugin"],
    })
    assert {"list_agents", "read_file", "custom_plugin"} <= loaded
    assert "watch_stream" not in loaded


@pytest.mark.parametrize("signal", ["selected", "method", "attributes", "skill", "workflow", "工作流", "脚本", "named"])
def test_authoring_schema_preload_uses_current_run_signals(monkeypatch, signal):
    kwargs = {"selected": ("builtin:method",)} if signal == "selected" else {}
    if signal == "method":
        kwargs["methods"] = ("builtin:method",)
    if signal == "attributes":
        kwargs["attrs"] = {"workflow_id": "workflow-1"}
    if signal in {"skill", "workflow", "工作流", "脚本", "named"}:
        kwargs["prompt"] = "请使用 " + ("custom_plugin" if signal == "named" else signal)
    assert {"skill_search", "scripts", "workflows"} <= _loaded(monkeypatch, **kwargs)


def test_authoring_preload_cannot_widen_scope_and_keeps_recovery(monkeypatch):
    loaded = _loaded(monkeypatch, prompt="创建 workflow", allowed=["read_file", "skill_search"],
                     carried=["read_artifact"], thread=["read_file"])
    assert loaded == {"read_file", "read_artifact", "skill_search"}
    assert _loaded(monkeypatch, prompt="你好", carried=["read_file"]) == {"read_file"}
    assert _loaded(monkeypatch, prompt="技能名不是工具权", available=[]) == set()


@pytest.mark.parametrize("restricted", [False, True])
def test_loop_support_authoring_first_generate_and_compact_share_native_schemas(tmp_path, monkeypatch, restricted):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
        prepare_conversation_compact_provider_surface,
    )
    from agent_py_agent.tests.test_gateway_conversation_compact import _agent, _SummaryBackend

    agent = _agent(tmp_path, context_tokens=128_000)
    backend = _SummaryBackend()
    agent.backend = backend
    observed = {}
    original = loop_support._initial_loop_loaded_tool_names

    # LLM: 包装真实入口只记录实际加载结果，不替代注册、权限裁决或原生Schema解析。
    # 函数用途: 保存业务调用使用的展示事实，随后交给正式Compact入口复核。
    def capture(host, params, seed, reconstructed):
        names = original(host, params, seed, reconstructed)
        observed["loaded"] = tuple(sorted(names))
        return names

    monkeypatch.setattr(loop_support, "_initial_loop_loaded_tool_names", capture)
    allowed = ["read_file", "tool_search"] if restricted else None
    agent.run("创建一个 workflow + skill", params=RunParams(save=False, allowed_tools=allowed))
    assert backend.calls == 1
    generated = {row["name"]: row for row in backend.kwargs[0]["tools"]}
    authoring = {"skill_search", "scripts", "workflows"}
    if restricted:
        assert generated.keys() == set(allowed)
        assert not authoring.intersection(observed["loaded"])
    else:
        assert authoring <= generated.keys() and authoring <= set(observed["loaded"])
    surface = prepare_conversation_compact_provider_surface(
        agent, ConversationCompactModelSurface(
            allowed_tools=tuple(allowed) if allowed is not None else None,
            loaded_tool_names=observed["loaded"],
        ), run_id="compact-authoring-test",
    )
    compact = {row["name"]: row for row in surface.tools}
    assert compact.keys() == generated.keys()
    for name in authoring.intersection(generated):
        assert compact[name] == generated[name]


@pytest.mark.parametrize("reason", ["subagent_runner_finished", "thread_goal_continue"])
def test_loop_support_preload_consumes_real_background_trigger_and_goal_attrs(tmp_path, reason):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        GoalRuntimeContext,
        _background_model_inputs,
        _run_params,
    )
    from agent_py_agent.tests.test_gateway_conversation_compact import _agent

    agent = _agent(tmp_path, context_tokens=128_000)
    request = BackgroundRunRequest(thread_id="thread-preload", reason=reason, wake_signal={
        "wake_signal_id": "wake-preload", "source_agent_id": "finished-child",
    })
    goal_context = GoalRuntimeContext(task_objective="核对孩子结果")
    if reason == "thread_goal_continue":
        goal_context = replace(goal_context, goal=SimpleNamespace(goal_id="goal-preload"))
    params = _run_params(request.thread_id, request, agent, goal_context=goal_context)
    _, _, trigger = _background_model_inputs(request, task_objective=goal_context.task_objective, wake_prompt="继续")
    params = replace(params, turn_trigger=trigger)
    assert CONVERSATION_BACKGROUND_EVENT_REASON_ATTR not in params.task_attributes
    if reason == "subagent_runner_finished":
        assert trigger.reason == reason and trigger.wake_signal_id == "wake-preload"
    else:
        assert params.task_attributes["thread_goal_id"] == "goal-preload"
    before = params.allowed_tools
    snapshot = SimpleNamespace(available_tool_names=frozenset(BACKGROUND_CONTINUATION_REQUIRED_TOOLS),
                               runtimes=(), presentation_deferred_names=frozenset())
    seed = SimpleNamespace(effective_contract_snapshot=None, selected_skill_ids=(), tool_runtime_snapshot=snapshot)
    loaded = loop_support._initial_loop_loaded_tool_names(agent, params, seed, SimpleNamespace(loaded_tool_names=set()))
    expected = ({"send_guidance", "cancel_subagents", "resolve_capability_requests"}
                if reason == "subagent_runner_finished" else {"get_goal", "update_goal"})
    assert expected <= loaded
    assert params.allowed_tools == before
    assert "package_build" not in loaded
