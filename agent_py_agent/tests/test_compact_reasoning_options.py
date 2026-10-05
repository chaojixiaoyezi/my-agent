# LLM: Fake transport asserts serialized provider payloads rather than local option projections.
# 模块用途: 锁住压缩请求与主请求的缓存分区、工具面和历史前缀合同。
"""压缩辅助调用必须沿同一线程复用主请求的思考分区。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_model_generation import _provider_request_options
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.conversation.compact_request_budget import (
    generate_bounded_compact_response,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice


# LLM: This test feeds identical thread identity through the main and compact request paths.
# 函数用途: 锁住 DeepSeek max/off 档位在真实 JSON payload 中的等价换算。
@pytest.mark.parametrize(
    ("level", "expected_thinking", "expected_effort"),
    [("max", None, "max"), ("off", {"type": "disabled"}, None)],
)
@pytest.mark.parametrize(
    "purpose",
    ["conversation_compact_summary", "conversation_compact_media_digest"],
)
def test_auxiliary_payload_uses_same_thread_reasoning_partition(
    level, expected_thinking, expected_effort, purpose
):
    backend, agent, thread_id = _threaded_backend(level)
    payloads = []

    # LLM: This transport records the exact payload and returns a synthetic terminal reply.
    # 函数用途: 截获序列化结果，不建立任何网络连接。
    def request_json(_path, payload, _headers):
        payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    main_payload = _capture_main_request(SimpleNamespace(
        backend=backend,
        agent=agent,
        thread_id=thread_id,
        payloads=payloads,
        system_instruction="",
    ))
    generate_auxiliary_model_response(AuxiliaryModelCallRequest(
        agent=agent,
        prompt="compact",
        messages=[
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "无 reasoning_content 的旧轮最终答复"},
        ],
        request_id="request-cache-options",
        thread_id=thread_id,
        purpose=purpose,
    ))

    compact_payload = payloads[-1]
    assert main_payload.get("thinking") == expected_thinking
    assert main_payload.get("reasoning_effort") == expected_effort
    assert compact_payload.get("thinking") == main_payload.get("thinking")
    assert compact_payload.get("reasoning_effort") == main_payload.get("reasoning_effort")


# LLM: The real backend serializer is paired with a synthetic store that exposes only one thread's frozen effort.
# 函数用途: 构造不联网的 OpenAI-compatible fake backend 和指定档位线程。
def _threaded_backend(level):
    config = AgentConfig(
        model_backend="openai_compatible",
        model_name="deepseek-v4-flash",
        api_base="https://api.deepseek.com",
        api_key="fake",
        stream_enabled=False,
    )
    backend = get_backend("openai_compatible", config)
    thread_id = "thread-cache-prefix"
    thread = SimpleNamespace(reasoning_effort=level)
    agent = SimpleNamespace(
        backend=backend,
        config=config,
        conversation_store=SimpleNamespace(
            threads=SimpleNamespace(load=lambda requested: thread if requested == thread_id else None)
        ),
    )
    return backend, agent, thread_id


# LLM: Fake transport records the provider payload and consumes only supplied synthetic responses.
# 函数用途: 安装不联网的 JSON 传输替身，供多条压缩合同复用。
def _capture_transport(backend, responses=None):
    payloads = []

    def request_json(_path, payload, _headers):
        payloads.append(deepcopy(payload))
        if responses is not None:
            return next(responses)
        return {"choices": [{"message": {"content": "summary"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    return payloads


# LLM: Both calls use the same provider history and tools; only the assistant-owned auxiliary options differ.
# 函数用途: 捕获主请求与bounded compact的序列化payload配对。
def _single_compact_payload_pair(context: SimpleNamespace) -> tuple[dict, dict]:
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactProviderSurface,
        conversation_compact_provider_prompt,
    )

    options = _provider_request_options(
        context.backend,
        SimpleNamespace(
            agent=context.agent,
            params=SimpleNamespace(task_attributes={"conversation_thread_id": context.thread_id}),
            system_instruction=context.system_instruction,
            first_token_timeout_seconds=0,
        ),
        forced=False,
    )
    surface = ConversationCompactProviderSurface(
        stable_prompt_prefix=context.system_instruction,
        tools=tuple(context.tools),
        system_instruction=context.system_instruction,
    )
    context.backend.generate(
        conversation_compact_provider_prompt(surface, ""),
        messages=context.messages,
        tools=context.tools,
        tool_choice=ToolChoice.auto(), request_options=options,
    )
    main_payload = context.payloads[-1]
    generate_bounded_compact_response(AuxiliaryModelCallRequest(
        agent=context.agent,
        prompt=conversation_compact_provider_prompt(surface, "COMPACT_ONLY_TAIL"),
        messages=context.messages,
        tools=context.tools,
        system_instruction=context.system_instruction,
        thread_id=context.thread_id,
        purpose="conversation_compact_summary",
    ))
    return main_payload, context.payloads[-1]


# LLM: The fixture includes a complete historical tool round and verifies only a trailing Compact addition.
# 函数用途: 确认可容纳压缩请求与主请求逐项共用 tools、auto、system、messages 和档位。
@pytest.mark.parametrize("level", ["max", "off"])
def test_bounded_single_compact_keeps_auto_tools_and_thread_prefix(level):
    backend, agent, thread_id = _threaded_backend(level)
    tools = [{
        "name": "read_file",
        "description": "read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "旧问题"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "已完成思考"},
            {"type": "tool_use", "id": "call-old", "name": "read_file", "input": {"path": "a"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-old", "content": "文件内容", "is_error": False}
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "旧最终答复"}]},
        {"role": "user", "content": [{"type": "text", "text": "当前用户问题"}]},
    ]
    payloads = _capture_transport(backend)
    main_payload, compact_payload = _single_compact_payload_pair(SimpleNamespace(
        backend=backend,
        agent=agent,
        thread_id=thread_id,
        payloads=payloads,
        tools=tools,
        messages=messages,
        system_instruction="stable system",
    ))
    assert compact_payload["tools"] == main_payload["tools"]
    assert compact_payload["tool_choice"] == main_payload["tool_choice"] == "auto"
    assert compact_payload["messages"][:-1] == main_payload["messages"][:-1]
    assert compact_payload["messages"][-1]["role"] == "user"
    assert compact_payload["messages"][-1]["content"] == (
        f'{main_payload["messages"][-1]["content"]}\n\nCOMPACT_ONLY_TAIL'
    )
    assert compact_payload.get("reasoning_effort") == main_payload.get("reasoning_effort")
    assert compact_payload.get("thinking") == main_payload.get("thinking")


# LLM: The tool-loop wrapper must derive the same thread identity before the shared payload serializer applies effort.
# 函数用途: 锁住原生工具循环把主线程 ID 送入 live compact 请求的转接边界。
def test_native_tool_loop_compact_forwards_thread_reasoning_context(monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service, native_tool_protocol
    from agent_py_agent.agent.backends.tool_ir import ToolResult
    from agent_py_agent.agent.memory_archive import compact_semantic_summary
    from agent_py_agent.agent.model_guidance import provider_system_instruction

    backend, agent, thread_id = _threaded_backend("max")
    tool = {"name": "read_file", "description": "read a file", "input_schema": {"type": "object"}}
    monkeypatch.setattr(native_tool_protocol, "resolve_native_tools", lambda _agent, _params: [tool])
    requests = []

    # LLM: This spy isolates the wrapper handoff while the neighboring fake-transport tests exercise serialization.
    # 函数用途: 捕获送给 live 摘要器的请求身份和工具面。
    def capture(request):
        requests.append(request)
        return "summary"

    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", capture)
    params = SimpleNamespace(
        tool_ir_history=[
            ToolResult(call_id=f"call-{index}", tool_name="read_file", status="succeeded")
            for index in range(2)
        ],
        task_attributes={"conversation_thread_id": thread_id},
        provider_history_messages=(),
        request_id="request-cache-options",
        run_id="run-cache-options",
        task_id="task-cache-options",
        user_prompt="continue",
    )

    assert _tool_loop_service._native_tool_history_summary(agent, params) == "summary"
    assert len(requests) == 1
    assert requests[0].thread_id == thread_id
    assert requests[0].tools == (tool,)
    assert requests[0].system_instruction == provider_system_instruction(backend)


# LLM: A structured tool result is rejected without parsing prose or granting execution capability.
# 函数用途: 验证 auto 结果含工具调用时只重试一次 none/无工具请求。
def test_structured_tool_call_is_discarded_then_retried_once_without_tools():
    backend, agent, thread_id = _threaded_backend("max")
    tool = {
        "name": "read_file",
        "description": "read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    payloads = []
    responses = iter([
        {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "call-no-execute", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"content": "机械摘要重试"}, "finish_reason": "stop"}]},
    ])

    # LLM: This transport records the exact payload and returns a synthetic terminal reply.
    # 函数用途: 截获序列化结果，不建立任何网络连接。
    def request_json(_path, payload, _headers):
        payloads.append(deepcopy(payload))
        return next(responses)

    backend.request_json = request_json
    result = generate_bounded_compact_response(
        AuxiliaryModelCallRequest(
            agent=agent,
            prompt="stable task",
            tools=[tool],
            system_instruction="stable system",
            thread_id=thread_id,
            purpose="conversation_compact_summary",
        )
    )

    assert result.text == "机械摘要重试"
    assert len(payloads) == 2
    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[0]["tools"]
    assert payloads[1]["tool_choice"] == "none"
    assert "tools" not in payloads[1]
    assert payloads[1]["reasoning_effort"] == "max"
    assert "thinking" not in payloads[1]
    assert payloads[1]["messages"][-1]["content"] == "stable task"


# LLM: Fake transport captures the real OpenAI-compatible payload; thread and system are frozen in context.
# 函数用途: 构造线程主请求的可比载荷，避免测试用字段投影代替真实组包。
def _capture_main_request(context: SimpleNamespace) -> dict:
    options = _provider_request_options(
        context.backend,
        SimpleNamespace(
            agent=context.agent,
            params=SimpleNamespace(task_attributes={"conversation_thread_id": context.thread_id}),
            system_instruction=context.system_instruction,
            first_token_timeout_seconds=0,
        ),
        forced=False,
    )
    context.backend.generate("主对话", request_options=options)
    return context.payloads[-1]


# LLM: Handoff reply is a fixture with the real live-summary schema, not a free-form success surrogate.
# 函数用途: 生成只供 fake transport 使用的主回复与有效 handoff 响应。
def _live_summary_responses():
    handoff = "\n".join([
        "[compact-live-handoff.v1]",
        "current_progress: 已完成长历史读取，并把具体目标和本轮状态保留在替代摘要中。",
        "user_constraints: 所有原始要求、路径、参数和未决边界都必须继续有效，不能把工具结果当作授权。",
        "completed: 已核对现存代码结构并确认请求由同一线程档位驱动。",
        "failures: 当前无传输失败；未收到可采用的供应商摘要前不得确认任务已完成。",
        "unresolved: 尚需逐个验证压缩请求的 options 与主请求保持同一分区。",
        "next_step: 继续运行所需回归、核对提交变更，并把真实未验证项留给集成复核。",
    ])
    return iter([
        {"choices": [{"message": {"content": "主回复"}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": handoff}, "finish_reason": "stop"}]},
    ])


# LLM: Volatile sections and stable history are frozen together so the fake main request matches the live serializer input.
# 函数用途: 组装 live 主请求与摘要共享的 typed provider 面。
def _live_compact_provider_context() -> SimpleNamespace:
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.backends.tool_ir import UserTurn
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactProviderSurface,
        conversation_compact_provider_messages,
        conversation_compact_provider_prompt,
    )
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import _live_summary_prompt

    history = [UserTurn("继续当前任务")]
    tools = [{"name": "read_file", "description": "read a file",
              "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    system_instruction = "稳定系统前缀"
    surface = ConversationCompactProviderSurface(
        stable_prompt_prefix="稳定缓存前缀",
        tools=tuple(tools),
        system_instruction=system_instruction,
        volatile_sections=(("prompt.tool_recommendations", "动态工具说明"),),
    )
    provider_prompt = conversation_compact_provider_prompt(surface, "")
    provider_history = conversation_compact_provider_messages(
        "", 0, (), volatile_sections=surface.volatile_sections,
    )
    messages = [
        *provider_history,
        *AnthropicMessageAdapter().to_provider_messages(history),
    ]
    return SimpleNamespace(
        history=history,
        tools=tools,
        system_instruction=system_instruction,
        provider_prompt=provider_prompt,
        provider_history=tuple(provider_history),
        messages=messages,
        summary_instruction=_live_summary_prompt(""),
    )


# LLM: The main request uses the live surface's exact system, tools, messages, and thread options.
# 函数用途: 捕获 live 压缩前的主请求载荷。
def _capture_live_main_payload(context: SimpleNamespace, live: SimpleNamespace) -> dict:
    options = _provider_request_options(
        context.backend,
        SimpleNamespace(
            agent=context.agent,
            params=SimpleNamespace(task_attributes={"conversation_thread_id": context.thread_id}),
            system_instruction=live.system_instruction,
            first_token_timeout_seconds=0,
        ),
        forced=False,
    )
    context.backend.generate(
        live.provider_prompt,
        messages=live.messages,
        tools=live.tools,
        tool_choice=ToolChoice.auto(),
        request_options=options,
    )
    return context.payloads[-1]


# LLM: The live summary receives the same frozen provider context as the main request and only appends its instruction.
# 函数用途: 捕获实际 live 摘要请求及其合成结果。
def _capture_live_summary(context: SimpleNamespace, live: SimpleNamespace) -> str:
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        summarize_live_tool_history,
    )

    request = LiveToolHistorySummaryRequest(
        history=live.history,
        backend=context.backend,
        agent=context.agent,
        thread_id=context.thread_id,
        task_prompt="继续当前任务",
        provider_prompt=live.provider_prompt,
        provider_history_messages=live.provider_history,
        tools=tuple(live.tools),
        system_instruction=live.system_instruction,
    )
    return summarize_live_tool_history(request)


# LLM: Both actual serialized requests share one prepared provider surface and one fake transport.
# 函数用途: 返回主请求与 live 摘要的 payload 供合同断言。
def _live_compact_payload_pair(context: SimpleNamespace) -> tuple[str, dict, dict, str]:
    live = _live_compact_provider_context()
    context.payloads = _capture_transport(context.backend, _live_summary_responses())
    main_payload = _capture_live_main_payload(context, live)
    summary = _capture_live_summary(context, live)
    return summary, main_payload, context.payloads[-1], live.summary_instruction


# LLM: Live native history is replayed with its main tools; Compact contributes only a trailing user instruction.
# 函数用途: 确认 live 摘要保持主请求前缀，并在 max/off 两档继承线程选项。
@pytest.mark.parametrize("level", ["max", "off"])
def test_live_tool_summary_uses_thread_reasoning_options(level):
    backend, agent, thread_id = _threaded_backend(level)
    summary, main_payload, compact_payload, instruction = _live_compact_payload_pair(SimpleNamespace(
        backend=backend, agent=agent, thread_id=thread_id,
    ))
    assert summary.startswith("[compact-semantic-summary]")
    assert compact_payload["tools"] == main_payload["tools"]
    assert compact_payload["tool_choice"] == main_payload["tool_choice"] == "auto"
    assert compact_payload["messages"][:-1] == main_payload["messages"][:-1]
    assert compact_payload["messages"][-1]["role"] == "user"
    assert compact_payload["messages"][-1]["content"] == (
        f'{main_payload["messages"][-1]["content"]}\n\n{instruction}'
    )
    assert compact_payload.get("thinking") == main_payload.get("thinking")
    assert compact_payload.get("reasoning_effort") == main_payload.get("reasoning_effort")


# LLM: Carried archive summaries are text-only and cannot reproduce the primary tools/history cache surface.
# 函数用途: 核实该边界路径仍沿用 thread 的推理分区，且不伪称工具前缀相等。
@pytest.mark.parametrize("level", ["max", "off"])
def test_carried_text_summary_uses_thread_reasoning_options(level):
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
        SemanticSummaryConfig,
        SemanticSummaryRequest,
        summarize_carried_tool_context,
    )

    backend, agent, thread_id = _threaded_backend(level)
    payloads = []

    # LLM: This transport records the exact payload and returns a synthetic terminal reply.
    # 函数用途: 截获序列化结果，不建立任何网络连接。
    def request_json(_path, payload, _headers):
        payloads.append(deepcopy(payload))
        return {"choices": [{"message": {"content": "中段携带摘要"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    main_payload = _capture_main_request(SimpleNamespace(
        backend=backend, agent=agent, thread_id=thread_id,
        payloads=payloads, system_instruction="",
    ))
    records = [
        {"tool": "read_file", "ok": True, "parameters": {"path": f"f{i}"},
         "output_preview": f"摘要来源 {i}", "scoped_call_id": f"call-{i}"}
        for i in range(15)
    ]
    result = summarize_carried_tool_context(SemanticSummaryRequest(
        records=records,
        mechanical_entries=[f"entry {i}" for i in range(15)],
        config=SemanticSummaryConfig(),
        backend=backend,
        agent=agent,
        thread_id=thread_id,
    ))

    assert result is not None
    compact_payload = payloads[-1]
    assert compact_payload.get("thinking") == main_payload.get("thinking")
    assert compact_payload.get("reasoning_effort") == main_payload.get("reasoning_effort")
    assert "tools" not in compact_payload
    assert "tool_choice" not in compact_payload
    assert compact_payload["messages"][0]["role"] == "user"


# LLM: Once the source is split, system/history differ; only the per-thread reasoning partition remains comparable.
# 函数用途: 验证分段每次调用保持主线程 thinking/reasoning_effort，并确认它不是完整前缀缓存命中。
@pytest.mark.parametrize("level", ["max", "off"])
def test_segmented_compact_keeps_thread_reasoning_options(monkeypatch, level):
    from agent_py_agent.agent.conversation import compact_request_budget as budget_module

    backend, agent, thread_id = _threaded_backend(level)
    payloads = []

    # LLM: This transport records the exact payload and returns a synthetic terminal reply.
    # 函数用途: 截获序列化结果，不建立任何网络连接。
    def request_json(_path, payload, _headers):
        payloads.append(deepcopy(payload))
        return {"choices": [{"message": {"content": "本片摘要"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    main_payload = _capture_main_request(SimpleNamespace(
        backend=backend, agent=agent, thread_id=thread_id,
        payloads=payloads, system_instruction="main system",
    ))
    monkeypatch.setattr(budget_module, "compact_summary_budget", lambda _agent: 800)
    generate_bounded_compact_response(AuxiliaryModelCallRequest(
        agent=agent,
        prompt="源历史段。" * 3000,
        system_instruction="main system",
        thread_id=thread_id,
        purpose="conversation_compact_summary",
    ))

    segments = payloads[1:]
    assert len(segments) > 1
    assert all("tools" not in item and item.get("tool_choice") == "none" for item in segments)
    assert all(item.get("reasoning_effort") == main_payload.get("reasoning_effort") for item in segments)
    assert all(item.get("thinking") == main_payload.get("thinking") for item in segments)
    assert all(item["messages"][0]["role"] == "system" for item in segments)
    assert all(
        item["messages"][0]["content"] != main_payload["messages"][0]["content"]
        for item in segments
    )
