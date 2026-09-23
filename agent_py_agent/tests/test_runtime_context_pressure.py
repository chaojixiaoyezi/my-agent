from __future__ import annotations

"""LLM: regression tests for runtime context pressure triggers.

给人看的解释：
这些测试确认长任务里的工具上下文裁剪不会变成第二套隐形压缩；
保存型运行一旦裁剪旧工具记录，就会回到统一 compact/resume 链路。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import (
    invalidate_provider_context_observation,
    model_visible_context_budget,
    model_visible_context_snapshot,
    model_visible_context_tokens,
    preflight_context_pressure_response,
    record_provider_context_observation,
    safe_inline_tool_result_tokens,
)
from agent_py_agent.agent.agent_core.model.usage import (
    provider_visible_input_token_usage,
)
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.tool_context_window import (
    _bounded_carried_tool_index,
    build_conversation_terminal_tool_fold,
    conversation_message_with_terminal_tool_fold,
    conversation_terminal_tool_fold_projection,
    window_tool_context_params,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


class _AgentStub:
    config = AgentConfig(auto_save_memory=True)
    backend = SimpleNamespace(context_window_tokens=128_000, name="fake")


def test_carried_tool_index_keeps_latest_action_when_middle_is_omitted() -> None:
    records = [
        {
            "tool": "read_file",
            "ok": True,
            "parameters": {"path": f"src/old-{index}.py"},
        }
        for index in range(120)
    ]
    records.append(
        {
            "tool": "write_file",
            "ok": True,
            "parameters": {"path": "reports/latest.md", "mode": "overwrite"},
        }
    )

    rendered = "\n".join(_bounded_carried_tool_index(records, 1_200))

    assert len(rendered) <= 1_200
    assert "omitted_middle_tool_index_entries" in rendered
    assert "121: tool=write_file status=ok" in rendered
    assert "reports/latest.md" not in rendered  # scalar values stay behind typed parameter keys
    assert "parameter_keys=path,mode" in rendered


def test_terminal_tool_fold_is_deterministic_bounded_and_redacted() -> None:
    agent = SimpleNamespace(
        config=AgentConfig(conversation_terminal_tool_fold_max_chars=1_400)
    )
    records = [
        {
            "tool": "write_file" if index % 2 == 0 else "run_command",
            "ok": index != 5,
            "scoped_call_id": f"call-{index}",
            "parameters": {
                "path": f"work/file-{index}.txt",
                "credentials": {"api_key": "terminal-fold-secret-value"},
            },
            "model_summary": f"完成第 {index} 项工具工作",
        }
        for index in range(12)
    ]

    first = build_conversation_terminal_tool_fold(agent, records, built_at_epoch=1_000)
    second = build_conversation_terminal_tool_fold(agent, records, built_at_epoch=1_000)
    rendered_hot = conversation_message_with_terminal_tool_fold(
        "本轮完成。",
        {"terminal_tool_fold": first},
        current_epoch=1_200,
    )
    rendered_cold = conversation_message_with_terminal_tool_fold(
        "本轮完成。",
        {"terminal_tool_fold": first},
        current_epoch=1_301,
    )
    hot_projection = conversation_terminal_tool_fold_projection(first, current_epoch=1_200)
    cold_projection = conversation_terminal_tool_fold_projection(first, current_epoch=1_301)

    assert first == second
    assert first["schema"] == "conversation_terminal_tool_fold.v2"
    assert first["tool_call_count"] == 12
    assert first["successful_tool_call_count"] == 11
    assert first["non_successful_tool_call_count"] == 1
    assert len(str(first["text"])) <= 1_400
    assert len(str(first["hot_text"])) <= 1_400
    assert first["fold_after_epoch"] == 1_300
    assert "terminal-fold-secret-value" not in str(first)
    assert "call-0" in str(first["text"])
    assert "projection: hot_tail" in rendered_hot
    assert "projection: cold_fold" in rendered_cold
    assert rendered_hot.startswith("本轮完成。")
    assert hot_projection["projection"] == "hot_tail"
    assert cold_projection["projection"] == "cold_fold"
    assert "hot_text" not in hot_projection
    assert "fold_after_epoch" not in hot_projection


def test_terminal_tool_fold_v1_is_read_as_cold_without_hot_tail() -> None:
    legacy = {
        "schema": "conversation_terminal_tool_fold.v1",
        "tool_call_count": 1,
        "successful_tool_call_count": 1,
        "non_successful_tool_call_count": 0,
        "text": "[conversation-terminal-tool-fold]\n- legacy-cold-fold",
    }

    rendered = conversation_message_with_terminal_tool_fold(
        "旧会话正文",
        {"terminal_tool_fold": legacy},
        current_epoch=1,
    )

    assert "legacy-cold-fold" in rendered
    assert "hot_tail" not in rendered


def test_terminal_tool_hot_tail_preserves_more_recent_detail_than_cold_fold() -> None:
    marker = "HOT-TAIL-RECENT-DETAIL"
    agent = SimpleNamespace(config=AgentConfig())
    fold = build_conversation_terminal_tool_fold(
        agent,
        [
            {
                "tool": "read_file",
                "ok": True,
                "scoped_call_id": "call-rich-summary",
                "model_summary": ("较长但仍有用的工具细节" * 80) + marker,
            }
        ],
        built_at_epoch=1_000,
    )

    hot = conversation_terminal_tool_fold_projection(fold, current_epoch=1_100)
    cold = conversation_terminal_tool_fold_projection(fold, current_epoch=1_400)

    assert marker in hot["text"]
    assert marker not in cold["text"]
    assert hot["projection"] == "hot_tail"
    assert cold["projection"] == "cold_fold"


def test_terminal_tool_hot_tail_zero_uses_cold_fold_immediately() -> None:
    agent = SimpleNamespace(
        config=AgentConfig(conversation_terminal_tool_hot_tail_seconds=0)
    )
    fold = build_conversation_terminal_tool_fold(
        agent,
        [{"tool": "read_file", "ok": True, "scoped_call_id": "call-cold-now"}],
        built_at_epoch=1_000,
    )

    projection = conversation_terminal_tool_fold_projection(fold, current_epoch=1_000)

    assert fold["hot_text"] == ""
    assert fold["fold_after_epoch"] == 1_000
    assert projection["projection"] == "cold_fold"


def test_terminal_tool_fold_can_be_disabled_without_changing_public_body() -> None:
    agent = SimpleNamespace(
        config=AgentConfig(conversation_terminal_tool_fold_enabled=False)
    )

    fold = build_conversation_terminal_tool_fold(
        agent,
        [{"tool": "read_file", "ok": True, "scoped_call_id": "call-1"}],
    )

    assert fold == {}
    assert conversation_message_with_terminal_tool_fold("公开正文", {}) == "公开正文"


def test_tool_context_window_requests_compact_for_saved_runs() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    overflow = params.live_archive_state["tool_context_window_overflow"]
    assert overflow["omitted_count"] > 0
    assert overflow["original_chars"] > 512_000
    assert params.tool_context[0].startswith("[tool-context-window]")


def test_tool_context_window_scales_with_model_compact_threshold() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 35_000) for idx in range(5)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    assert "tool_context_window_overflow" not in params.live_archive_state
    assert not params.tool_context[0].startswith("[tool-context-window]")


def test_tool_context_window_does_not_request_compact_for_unsaved_runs() -> None:
    params = SimpleNamespace(
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=False,
    )

    window_tool_context_params(_AgentStub(), params)

    assert "tool_context_window_overflow" not in params.live_archive_state
    assert params.tool_context[0].startswith("[tool-context-window]")


def test_authoritative_conversation_windows_without_requesting_second_compact() -> None:
    params = SimpleNamespace(
        context_scope="conversation",
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        tool_context=[f"entry-{idx}-" + ("x" * 100_000) for idx in range(6)],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    window_tool_context_params(_AgentStub(), params)

    assert params.tool_context[0].startswith("[tool-context-window]")
    assert "tool_context_window_overflow" not in params.live_archive_state


def test_preflight_context_pressure_uses_tool_context_window_signal() -> None:
    params = SimpleNamespace(
        context_scope="default",
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={
            "tool_context_window_overflow": {
                "omitted_count": 12,
                "original_chars": 80_000,
                "preserved_count": 5,
            }
        },
    )
    request = SimpleNamespace(agent=_AgentStub(), params=params, prompt="短 prompt", tool_rounds=9)

    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "preflight"
    assert response.text.startswith("[RUN_CONTEXT_PRESSURE]")
    assert project_user_reply(response.text).internal_signal is True
    assert project_user_reply(response.text).content == ""
    assert "tool_context_window_overflow=true" in response.text
    assert "tool_context_window_overflow" not in params.live_archive_state


def test_preflight_uses_configured_threshold_without_a_second_ceiling(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, memory_compact_auto_trigger_percent=70),
        backend=SimpleNamespace(context_window_tokens=1000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="default",
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={},
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="系统上下文", tool_rounds=3)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 699,
    )
    assert preflight_context_pressure_response(request) is None

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 700,
    )
    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert "compact_threshold=700" in response.text


def test_preflight_checks_explicit_shared_window_against_sent_output_cap(monkeypatch) -> None:
    backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="test-key", model_name="test-model",
        context_window_tokens=1_000, max_tokens=300,
    ))
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True, model_context_window_tokens=1_000,
            model_context_window_explicit=True, memory_compact_auto_trigger_percent=90,
        ),
        backend=backend,
    )
    params = SimpleNamespace(
        context_scope="default", live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="测试", tool_rounds=0)
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _value: 699,
    )
    assert preflight_context_pressure_response(request) is None
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _value: 700,
    )
    response = preflight_context_pressure_response(request)
    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert "request_output_reserve=300" in response.text
    assert "request_input_ceiling=700" in response.text
    snapshot = model_visible_context_snapshot(agent, params, "测试")
    assert snapshot.compact_trigger_tokens == 900
    assert snapshot.current_tokens == 700


def test_preflight_does_not_invent_output_reserve_without_wire_cap(monkeypatch) -> None:
    backend = OpenAIResponsesBackend(BackendOptions(
        api_base="https://example.invalid", api_key="test-key", model_name="test-model",
        context_window_tokens=1_000, max_tokens=300,
    ))
    backend.auth_ref = {"mode": "chatgpt"}
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True, model_context_window_tokens=1_000,
            model_context_window_explicit=True, memory_compact_auto_trigger_percent=90,
        ),
        backend=backend,
    )
    params = SimpleNamespace(
        context_scope="default", live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="测试", tool_rounds=0)
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _value: 700,
    )
    assert preflight_context_pressure_response(request) is None
    agent.config.model_context_window_explicit = False
    backend.auth_ref = {}
    assert preflight_context_pressure_response(request) is None


def test_configured_250k_and_1m_windows_admit_only_fitting_requests(monkeypatch) -> None:
    estimate = {"tokens": 0}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _value: estimate["tokens"],
    )
    for window in (250_000, 1_000_000):
        backend = HttpBackend(BackendOptions(
            api_base="https://example.invalid", api_key="test-key", model_name="test-model",
            context_window_tokens=window, max_tokens=window // 5,
        ))
        agent = SimpleNamespace(
            config=AgentConfig(
                auto_save_memory=True, model_context_window_tokens=window,
                model_context_window_explicit=True, memory_compact_auto_trigger_percent=90,
            ),
            backend=backend,
        )
        request = SimpleNamespace(
            agent=agent, prompt="测试", tool_rounds=0,
            params=SimpleNamespace(
                context_scope="default", live_archive_state={},
                tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
            ),
        )
        estimate["tokens"] = window * 4 // 5 - 1
        assert preflight_context_pressure_response(request) is None
        estimate["tokens"] += 1
        response = preflight_context_pressure_response(request)
        assert response is not None
        assert f"request_input_ceiling={window * 4 // 5}" in response.text


def test_preflight_native_counts_tool_schemas_before_first_tool_call(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            enable_tools=True,
            tool_protocol="native",
            model_name="native-test-model",
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=1_000,
        ),
        backend=SimpleNamespace(context_window_tokens=1_000, name="anthropic_compatible"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={},
        tool_context=[],
        tool_ir_history=[],
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools",
        lambda _agent, _params: [
            {
                "name": "large_native_tool",
                "description": "schema-" + ("x" * 8_000),
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            }
        ],
    )
    request = SimpleNamespace(
        agent=agent,
        params=params,
        prompt="短 prompt",
        tool_rounds=0,
    )

    visible = model_visible_context_tokens(agent, params, request.prompt)
    response = preflight_context_pressure_response(request)

    assert visible >= 900
    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert response.runtime_source == "preflight"
    assert f"model_visible_tokens={visible}" in response.text


def test_live_context_snapshot_reuses_exact_total_and_exposes_no_content(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            enable_tools=True,
            tool_protocol="native",
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=128_000,
        ),
        backend=SimpleNamespace(context_window_tokens=128_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={},
        tool_context=["private-guidance"],
        tool_ir_history=[],
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools",
        lambda _agent, _params: [
            {
                "name": "secret_tool_name",
                "description": "secret schema content",
                "input_schema": {"type": "object"},
            }
        ],
    )

    snapshot = model_visible_context_snapshot(agent, params, "private prompt")
    public = snapshot.to_public_dict()

    assert snapshot.current_tokens == model_visible_context_tokens(
        agent,
        params,
        "private prompt",
    )
    assert snapshot.context_window_tokens == 128_000
    assert snapshot.compact_trigger_tokens == 115_200
    assert sum(
        (
            snapshot.prompt_tokens,
            snapshot.messages_tokens,
            snapshot.runtime_guidance_tokens,
            snapshot.tool_schema_tokens,
        )
    ) == snapshot.current_tokens
    assert set(public) == {
        "schema",
        "estimated",
        "context_window_tokens",
        "compact_trigger_tokens",
        "current_tokens",
        "prompt_tokens",
        "messages_tokens",
        "runtime_guidance_tokens",
        "tool_schema_tokens",
        "protocol",
    }
    assert "private" not in repr(public)
    assert "secret" not in repr(public)


def test_unsaved_model_call_keeps_configured_trigger_without_enabling_compact(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=128_000,
        ),
        backend=SimpleNamespace(context_window_tokens=128_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=False,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 120_000,
    )

    snapshot = model_visible_context_snapshot(agent, params, "presentation prompt")
    response = preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt="presentation prompt")
    )

    assert snapshot.context_window_tokens == 128_000
    assert snapshot.compact_trigger_tokens == 115_200
    assert snapshot.current_tokens == 120_000
    assert response is None


def test_inline_tool_result_budget_reuses_current_compact_headroom(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=100_000,
        ),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="default",
        tool_context=[],
        tool_ir_history=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
    )
    agent._current_run_params = params
    agent._current_user_prompt = "当前会话"

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 60_000,
    )
    budget = model_visible_context_budget(agent)

    assert budget.context_window_tokens == 100_000
    assert budget.compact_trigger_tokens == 90_000
    assert budget.current_tokens == 60_000
    assert budget.remaining_to_compact_tokens == 30_000
    assert safe_inline_tool_result_tokens(agent) == 30_000

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: 89_000,
    )
    assert safe_inline_tool_result_tokens(agent) == 1_000


def test_provider_visible_usage_distinguishes_anthropic_and_openai_cache_shapes() -> None:
    anthropic = SimpleNamespace(
        usage={
            "input_tokens": 10_000,
            "cache_read_input_tokens": 20_000,
            "cache_creation_input_tokens": 10_000,
        }
    )
    openai = SimpleNamespace(
        usage={
            "prompt_tokens": 40_000,
            "prompt_tokens_details": {"cached_tokens": 20_000},
        }
    )

    assert provider_visible_input_token_usage(anthropic) == 40_000
    assert provider_visible_input_token_usage(openai) == 40_000


def test_provider_observation_uses_actual_baseline_plus_conservative_growth(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=100_000,
        ),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    raw = {"tokens": 100_000}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: raw["tokens"],
    )
    response = SimpleNamespace(
        usage={
            "input_tokens": 10_000,
            "cache_read_input_tokens": 20_000,
            "cache_creation_input_tokens": 10_000,
        }
    )

    initial = model_visible_context_snapshot(agent, params, "prompt")
    assert record_provider_context_observation(
        agent,
        params,
        raw_estimated_tokens=100_000,
        context_surface_fingerprint=initial.context_surface_fingerprint,
        response=response,
    )
    raw["tokens"] = 105_000
    snapshot = model_visible_context_snapshot(agent, params, "prompt")
    assert snapshot.raw_estimated_tokens == 105_000
    assert snapshot.current_tokens == 45_000
    assert sum(
        (
            snapshot.prompt_tokens,
            snapshot.messages_tokens,
            snapshot.runtime_guidance_tokens,
            snapshot.tool_schema_tokens,
        )
    ) == 45_000
    assert (
        preflight_context_pressure_response(
            SimpleNamespace(agent=agent, params=params, prompt="prompt")
        )
        is None
    )

    raw["tokens"] = 165_000
    assert model_visible_context_tokens(agent, params, "prompt") == 105_000
    assert preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt="prompt")
    ) is not None


def test_provider_observation_invalidates_when_connection_changes(monkeypatch) -> None:
    backend = SimpleNamespace(
        context_window_tokens=100_000, name="fake", model_name="same-model",
        api_base="https://first.invalid", api_key="first-secret", custom_headers={},
    )
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, model_context_window_tokens=100_000),
        backend=backend,
    )
    params = SimpleNamespace(
        context_scope="conversation", save=True,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    raw = {"tokens": 80_000}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: raw["tokens"],
    )
    initial = model_visible_context_snapshot(agent, params, "prompt")
    assert record_provider_context_observation(
        agent, params, raw_estimated_tokens=80_000,
        context_surface_fingerprint=initial.context_surface_fingerprint,
        response=SimpleNamespace(usage={"input_tokens": 40_000}),
    )
    raw["tokens"] = 85_000
    assert model_visible_context_tokens(agent, params, "prompt") == 45_000
    backend.api_base = "https://second.invalid"
    assert model_visible_context_tokens(agent, params, "prompt") == 85_000
    backend.api_base = "https://first.invalid"
    assert model_visible_context_tokens(agent, params, "prompt") == 45_000
    backend.api_key = "second-secret"
    assert model_visible_context_tokens(agent, params, "prompt") == 85_000
    assert "first-secret" not in str(params.live_archive_state)


def test_provider_observation_falls_back_after_rewrite_or_missing_usage(monkeypatch) -> None:
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=100_000,
        ),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    raw = {"tokens": 100_000}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: raw["tokens"],
    )
    initial = model_visible_context_snapshot(agent, params, "prompt")
    assert record_provider_context_observation(
        agent,
        params,
        raw_estimated_tokens=100_000,
        context_surface_fingerprint=initial.context_surface_fingerprint,
        response=SimpleNamespace(usage={"prompt_tokens": 40_000}),
    )

    raw["tokens"] = 80_000
    assert model_visible_context_tokens(agent, params, "prompt") == 80_000
    assert record_provider_context_observation(
        agent,
        params,
        raw_estimated_tokens=80_000,
        context_surface_fingerprint=initial.context_surface_fingerprint,
        response=SimpleNamespace(usage={}),
    ) is False
    raw["tokens"] = 105_000
    assert model_visible_context_tokens(agent, params, "prompt") == 105_000

    assert invalidate_provider_context_observation(params) is False


def test_provider_observation_survives_reconstructed_background_slice(
    tmp_path,
    monkeypatch,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "provider-calibration"})
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=100_000,
        ),
        backend=SimpleNamespace(
            context_window_tokens=100_000,
            name="fake",
            model_name="fake-model",
        ),
        conversation_store=store,
    )
    raw = {"tokens": 100_000}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _payload: raw["tokens"],
    )
    first_params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        task_attributes={"conversation_thread_id": thread.thread_id},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    first_snapshot = model_visible_context_snapshot(agent, first_params, "prompt")

    assert record_provider_context_observation(
        agent,
        first_params,
        raw_estimated_tokens=first_snapshot.raw_estimated_tokens,
        context_surface_fingerprint=first_snapshot.context_surface_fingerprint,
        response=SimpleNamespace(usage={"prompt_tokens": 40_000}),
    )
    persisted = store.threads.load(thread.thread_id)
    assert persisted is not None
    assert persisted.provider_context_observation["compact_generation"] == 0

    fresh_params = SimpleNamespace(
        context_scope="conversation",
        save=True,
        task_attributes={"conversation_thread_id": thread.thread_id},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={},
    )
    raw["tokens"] = 90_000
    # Reconstructed slices use a conservative ratio floor, so a 40/100 provider
    # observation becomes 45K rather than falling back to the raw 90K estimate.
    assert model_visible_context_tokens(agent, fresh_params, "prompt") == 45_000
    raw["tokens"] = 110_000
    assert model_visible_context_tokens(agent, fresh_params, "prompt") == 55_000
    # A different stable prompt surface cannot borrow the earlier calibration.
    assert model_visible_context_tokens(agent, fresh_params, "changed prompt") == 110_000
