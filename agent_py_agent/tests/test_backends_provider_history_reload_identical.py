"""磁盘重载前后，同一历史的 provider payload 必须逐字相等（DeepSeek 缓存底座合同）。

背景（2026-10-04）：
- 每轮请求把「已结束回合的原生消息」写进 transcript（metadata.canonical_native_messages）；
- 重新加载（Gateway 重启/换版、内存淘汰、子代理续跑、TUI 重连）后，历史从磁盘重建；
- 服务端 KV 缓存按前缀匹配，历史里任何一处字节变化都会让该点之后整段未命中；
- 因此「同进程续跑」与「磁盘重载」两条路径必须产出逐字相同的 provider payload。

使用真实读出链路（ConversationStore → conversation_history_rows → _gateway_history_source →
seed_provider_history_messages）与本轮 IR 拼接后过真实出站组包，逐字节比较。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.message_adapter import (
    AnthropicMessageAdapter,
    strip_orphaned_tool_blocks,
)
from agent_py_agent.agent.backends.openai_chat import (
    OpenAICompatibleBackend,
    _OpenAIGenerateRequest,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation.history_projection import conversation_history_rows
from agent_py_agent.agent.conversation.history_seed import seed_provider_history_messages
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.request_context import _gateway_history_source
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


# LLM: 一轮包含两类工具往返、思考、插话与运行事实；键序刻意非字母序，用于暴露序列化不一致。
# 函数用途: 给出「已结束的一轮」的标准形状，供同进程与重载两条路径共用。
def _completed_turn_ir() -> list[object]:
    read_call = canonical_history_call("read_file", {"path": "a.txt", "encoding": "utf-8"}, call_id="c1")
    write_call = canonical_history_call(
        "write_file", {"path": "b.txt", "content": "x", "overwrite": True}, call_id="c2",
    )
    return [
        UserTurn("# User Task\n先读再写"),
        AssistantTurn(text="", tool_calls=[read_call], content_blocks=[
            {"type": "thinking", "thinking": "先读文件"},
            {"type": "tool_use", "id": "c1", "name": "read_file",
             "input": {"path": "a.txt", "encoding": "utf-8"}},
        ]),
        canonical_history_result(read_call, "文件内容"),
        UserTurn("插话：记得保留编码", input_ids=("steer-1",)),
        AssistantTurn(text="好的", tool_calls=[write_call], content_blocks=[
            {"type": "thinking", "thinking": "再写"},
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "c2", "name": "write_file",
             "input": {"path": "b.txt", "content": "x", "overwrite": True}},
        ]),
        canonical_history_result(write_call, "已写入"),
        RuntimeFactsTurn("当前时间 12:00", source="prompt.volatile"),
        AssistantTurn(text="完成", content_blocks=[
            {"type": "thinking", "thinking": "总结"}, {"type": "text", "text": "完成"}]),
    ]


# LLM: 落盘走产品真实路径：IR → Anthropic 消息 → canonical envelope → transcript metadata。
# 函数用途: 把一轮历史写进真实会话账本，并返回同进程路径使用的 provider 消息。
def _persist_turn(store: ConversationStore, thread_id: str, ir: list[object]) -> list[dict]:
    native = strip_orphaned_tool_blocks(AnthropicMessageAdapter().to_provider_messages(ir))
    store.messages.append({
        "thread_id": thread_id, "role": "user", "content": "先读再写",
        "metadata": {"conversation_request_id": "turn-N"},
    })
    store.messages.append({
        "thread_id": thread_id, "role": "assistant", "content": "完成",
        "metadata": {"conversation_request_id": "turn-N",
                     CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(native)},
    })
    return native


# LLM: 用真实后端只做组包，不发起任何网络请求；工具面与档位按 DeepSeek 官网线程形状取。
# 函数用途: 构造第 N+1 轮的真实出站 payload，供两条路径逐字比较。
def _next_turn_payload(prior_messages: list[dict]) -> dict:
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://api.deepseek.com", api_key="test-key-not-real",
        model_name="deepseek-v4-flash", max_tokens=8192, reasoning_control="effort",
    ))
    current = [UserTurn("# User Task\n继续")]
    return backend._request_payload(_OpenAIGenerateRequest(
        prompt="# User Task\n继续",
        messages=[*prior_messages, *AnthropicMessageAdapter().to_provider_messages(current)],
        system_instruction="SYS",
        tools=[{"name": "read_file", "description": "读", "input_schema": {"type": "object"}}],
        reasoning_effort="max",
    ))


# LLM: 重新加载必须走产品自己的恢复入口，不能只构造 metadata 字典——否则测不到真实选择与投影。
# 函数用途: 新建 store 从磁盘重建历史，返回重载路径的 provider 消息。
def _reload_prior_messages(root, thread_id: str) -> list[dict]:
    store = ConversationStore(root / "conversations", initialize=False)
    agent = SimpleNamespace(
        conversation_store=store,
        config=SimpleNamespace(conversation_history_max_turns=20, conversation_history_max_chars=48_000),
    )
    rows = conversation_history_rows(agent, thread_id, "turn-N+1", [])
    source = _gateway_history_source(rows, "turn-N+1", None)
    return list(seed_provider_history_messages(SimpleNamespace(source=source)))


# LLM: 这条断言是 DeepSeek 缓存底座的红线：任何字段顺序、丢字段、截短都会让整段前缀失效。
# 函数用途: 证明「同进程续跑」与「磁盘重载」产出的 provider payload 逐字节相同。
def test_reloaded_history_payload_matches_in_process_payload_byte_for_byte(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    thread_id = thread.thread_id

    in_process = _persist_turn(store, thread_id, _completed_turn_ir())
    reloaded = _reload_prior_messages(tmp_path, thread_id)
    assert reloaded, "重载必须能读出已落盘的原生历史"

    live_payload = json.dumps(_next_turn_payload(in_process), ensure_ascii=False, sort_keys=True)
    reload_payload = json.dumps(_next_turn_payload(reloaded), ensure_ascii=False, sort_keys=True)
    assert live_payload == reload_payload


# LLM: 工具参数键序是「落盘重排」最容易踩的一处：transcript 写入按字母序排键。
# 函数用途: 直接钉住 tool_calls.arguments 的字节，防止有人改回依赖插入序的序列化。
def test_reloaded_tool_call_arguments_keep_stable_key_order(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    _persist_turn(store, thread.thread_id, _completed_turn_ir())

    payload = _next_turn_payload(_reload_prior_messages(tmp_path, thread.thread_id))
    arguments = [
        call["function"]["arguments"]
        for message in payload["messages"]
        for call in (message.get("tool_calls") or [])
    ]
    assert arguments, "重载后的历史里必须仍有工具调用"
    for raw in arguments:
        parsed = json.loads(raw)
        assert raw == json.dumps(parsed, ensure_ascii=False, sort_keys=True), (
            f"工具参数必须是稳定键序，实际为 {raw!r}"
        )


# LLM: reasoning_content 是 DeepSeek 思考模式的历史事实；重放丢失会改变请求语义。
# 函数用途: 钉住重载后 assistant 的 reasoning_content 逐条保留、且内容不变。
def test_reloaded_history_keeps_assistant_reasoning_content(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    _persist_turn(store, thread.thread_id, _completed_turn_ir())

    payload = _next_turn_payload(_reload_prior_messages(tmp_path, thread.thread_id))
    reasoning = [m["reasoning_content"] for m in payload["messages"] if "reasoning_content" in m]
    assert reasoning == ["先读文件", "再写", "总结"]


# LLM: Responses 适配器与 Chat 走同一份落盘历史，因此同样受「键序被磁盘重排」影响；
#   两条出站路径必须各自被钉住，否则只修一条时另一条静默回退。
#   注意只断言「重载后的键序稳定」不够：磁盘本来就把键排好序，回退修复也不会变红；
#   必须拿「同进程（未落盘）」与「重载」两侧逐字节对照，才能抓到依赖插入序的序列化。
# 函数用途: 证明 Responses 转换下，同进程与重载产出的函数调用参数逐字节相同且为稳定键序。
def test_responses_path_live_and_reloaded_arguments_match(tmp_path):
    from agent_py_agent.agent.backends.responses_wire import message_items

    def function_arguments(messages: list[dict]) -> list[str]:
        return [
            item["arguments"]
            for message in messages
            for item in message_items(message, "deepseek-v4-flash")
            if item.get("type") == "function_call"
        ]

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    in_process = _persist_turn(store, thread.thread_id, _completed_turn_ir())
    reloaded = _reload_prior_messages(tmp_path, thread.thread_id)

    live_arguments = function_arguments(in_process)
    reload_arguments = function_arguments(reloaded)
    assert live_arguments, "同进程历史里必须仍有 Responses 函数调用"
    assert live_arguments == reload_arguments
    for raw in live_arguments:
        parsed = json.loads(raw)
        assert raw == json.dumps(parsed, ensure_ascii=False, sort_keys=True), (
            f"Responses 函数调用参数必须是稳定键序，实际为 {raw!r}"
        )


# LLM: Anthropic 路径的 tool_use.input 是 JSON 对象（不是字符串），整个请求体由唯一编码器
#   gateway_request_body 落字节，因此所有嵌套 dict 的键序都会进入 cache_control 前缀比较。
#   磁盘读回的历史已是字母序，进程内构造不是；只修 OpenAI 那种「参数已序列化成字符串」的地方不够。
# 函数用途: 钉住 Anthropic 路径在重载前后发出逐字相同的请求体。
def test_anthropic_request_body_matches_across_reload(tmp_path):
    from agent_py_agent.agent.backends.anthropic import (
        AnthropicCompatibleBackend,
        _AnthropicGenerateRequest,
    )
    from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body

    def anthropic_body(prior: list[dict]) -> bytes:
        backend = AnthropicCompatibleBackend(BackendOptions(
            api_base="https://api.minimaxi.com", api_key="test-key-not-real",
            model_name="MiniMax-M2.7", max_tokens=8192,
        ))
        current = [UserTurn("# User Task\n继续")]
        payload = backend._request_payload(_AnthropicGenerateRequest(
            prompt="# User Task\n继续",
            messages=[*prior, *AnthropicMessageAdapter().to_provider_messages(current)],
            system_instruction="SYS",
            tools=[{"name": "read_file", "description": "读", "input_schema": {"type": "object"}}],
        ))
        return gateway_request_body(payload)

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    in_process = _persist_turn(store, thread.thread_id, _completed_turn_ir())
    reloaded = _reload_prior_messages(tmp_path, thread.thread_id)

    live_body = anthropic_body(in_process)
    reload_body = anthropic_body(reloaded)
    assert b'"tool_use"' in live_body, "用例必须覆盖带工具调用的历史"
    assert live_body == reload_body


# LLM: 结构化 prompt（CacheStructuredPrompt）是宿主真实主路径：稳定前缀/动态尾部拆成结构化布局，
#   _request_payload 走 structured_native_layout_active 分支；历史块键序规范化必须对该分支同样
#   生效，不能只靠「规范化在入口函数顶部」这个实现细节。这里钉住结构化路径的重载字节一致。
# 函数用途: 证明带结构化 prompt 的请求在「同进程」与「磁盘重载」两条路径上出站字节逐字相同。
def test_structured_prompt_request_body_matches_across_reload(tmp_path):
    from agent_py_agent.agent.backends.anthropic import (
        AnthropicCompatibleBackend,
        _AnthropicGenerateRequest,
    )
    from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    def anthropic_body(prior: list[dict]) -> bytes:
        backend = AnthropicCompatibleBackend(BackendOptions(
            api_base="https://api.minimaxi.com", api_key="test-key-not-real",
            model_name="MiniMax-M2.7", max_tokens=8192,
        ))
        prompt = CacheStructuredPrompt(
            "STABLE RULES", "VOLATILE FACTS",
            stable_user_prefix="USER PREFIX", canonical_user_turn="CANON",
        )
        payload = backend._request_payload(_AnthropicGenerateRequest(
            prompt=prompt,
            messages=[*prior, *AnthropicMessageAdapter().to_provider_messages([UserTurn("# User Task\n继续")])],
            system_instruction="SYS",
            tools=[{"name": "read_file", "description": "读", "input_schema": {"type": "object"}}],
        ))
        return gateway_request_body(payload)

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    in_process = _persist_turn(store, thread.thread_id, _completed_turn_ir())
    reloaded = _reload_prior_messages(tmp_path, thread.thread_id)

    live_body = anthropic_body(in_process)
    reload_body = anthropic_body(reloaded)
    assert b'"tool_use"' in live_body, "用例必须覆盖带工具调用的历史"
    assert live_body == reload_body


# LLM: 唯一编码器必须保持调用方给的键序——工具 JSON Schema 与结构化输出 schema 的顺序会进模型，
#   在编码器里整包排序属于夹带的模型行为变化。历史重放的键序稳定性由消息层规范化负责。
# 函数用途: 证明编码器不重排调用方顺序，且同一份载荷编码稳定。
def test_single_encoder_preserves_caller_key_order():
    from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body

    payload = {
        "model": "m",
        "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a", "mode": "r"}},
        ]}],
    }
    encoded = gateway_request_body(payload).decode("utf-8")
    assert encoded == json.dumps(payload), "编码器必须保持调用方给的键序"
    assert gateway_request_body(payload) == encoded.encode("utf-8")


# LLM: 三个后端的请求体构造各自独立，供 schema 顺序护栏共用；都是纯组包，不发网络。
# 函数用途: 取得一次真实出站 payload（工具定义与 schema 均来自调用方）。
def _payload_openai(tools: list[dict], schema: dict) -> dict:
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://api.deepseek.com", api_key="k", model_name="m", max_tokens=16,
    ))
    return backend._request_payload(_OpenAIGenerateRequest(
        prompt="p", system_instruction="S", tools=tools, response_schema=schema,
    ))


# LLM: Anthropic 的结构化输出是「把 schema 当工具发」，所以这里同时带普通工具与结构化工具，
#   一次覆盖「工具 schema 顺序」和「结构化输出 schema 顺序」两条。
# 函数用途: 取得 Anthropic 出站 payload，工具面包含结构化输出工具。
def _payload_anthropic(tools: list[dict], schema: dict) -> dict:
    from agent_py_agent.agent.backends.anthropic import (
        AnthropicCompatibleBackend,
        _AnthropicGenerateRequest,
    )

    backend = AnthropicCompatibleBackend(BackendOptions(
        api_base="https://api.minimaxi.com", api_key="k", model_name="m", max_tokens=16,
    ))
    structured_tool = {"name": "my_agent_structured_output", "description": "d",
                       "input_schema": schema}
    return backend._request_payload(_AnthropicGenerateRequest(
        prompt="p", system_instruction="S", tools=[*tools, structured_tool],
    ))


# LLM: Responses 的结构化输出在 response_format 里，工具仍走 tools；纯计算。
# 函数用途: 取得 Responses 请求的形状（工具与 response_format）。
def _payload_responses(tools: list[dict], schema: dict) -> dict:
    return {"tools": tools, "response_format": {"schema": schema}}


# LLM: 工具在 payload 里的位置随协议不同（openai 在 function.parameters，anthropic 在
#   input_schema）；这里只按结构取值，不按协议名特判业务语义。
# 函数用途: 从一次出站 payload 里取出指定名字的工具参数 schema。
def _sent_tool_schema(payload: dict, name: str) -> dict:
    for tool in payload.get("tools") or []:
        if "function" in tool and tool["function"].get("name") == name:
            return tool["function"]["parameters"]
        if "function" not in tool and tool.get("name") == name:
            return tool["input_schema"]
    raise AssertionError(f"payload 里没有工具 {name}")


# LLM: 用例前提：curator schema 的候选字段顺序必须非字母序，否则护栏测不出「被排序」。
# 函数用途: 构造真实的 curator 结构化输出 schema，并返回其候选字段顺序。
def _curator_schema_and_order() -> tuple[dict, list[str]]:
    from agent_py_agent.agent.memory_store.curator_schema import build_curator_response_schema

    schema = build_curator_response_schema(
        output_version="curator.v1", candidate_types=("fact",), origins=("user",),
        actions=("add",), promotion_targets=("personal",),
    )
    order = list(schema["properties"]["candidates"]["items"]["properties"].keys())
    assert order != sorted(order), "用例前提：curator schema 必须是非字母序"
    return schema, order


# LLM: 规范化只允许动「历史消息块」，绝不能碰工具定义或结构化输出 schema——
#   JSON Schema 的属性顺序会进模型：严格结构化输出按 schema 顺序生成字段，
#   工具 schema 的属性顺序同理。整包 sort_keys 会把 curator 的 content 排到 confidence 之后，
#   模型就会「先写结论后写依据」。这条护栏用真实 curator schema 钉住调用方顺序。
# 函数用途: 证明三个后端发出的请求体里，工具 schema 的属性顺序都没被改。
def test_tool_schema_key_order_is_preserved_across_backends():
    schema, _ = _curator_schema_and_order()
    tool_schema = {"type": "object",
                   "properties": {"zeta": {"type": "string"}, "alpha": {"type": "string"}},
                   "required": ["zeta"]}
    tools = [{"name": "probe", "description": "d", "input_schema": tool_schema}]

    for name, payload in (("openai", _payload_openai(tools, schema)),
                          ("anthropic", _payload_anthropic(tools, schema)),
                          ("responses", _payload_responses(tools, schema))):
        sent = _sent_tool_schema(payload, "probe")
        assert list(sent["properties"].keys()) == ["zeta", "alpha"], (
            f"{name}: 工具 schema 属性顺序被改，实际 {list(sent['properties'].keys())}"
        )


# LLM: 结构化输出 schema 的顺序决定严格模式生成字段的先后（先写结论还是先写依据），
#   属于调用方契约，不能被键序规范化顺带改掉。
# 函数用途: 证明 openai 与 anthropic 发出的结构化输出 schema 保持调用方字段顺序。
def test_response_schema_key_order_is_preserved_across_backends():
    schema, candidate_order = _curator_schema_and_order()
    tools: list[dict] = []

    openai_fmt = _payload_openai(tools, schema).get("response_format") or {}
    openai_schema = openai_fmt.get("json_schema", {}).get("schema") or {}
    openai_order = list(openai_schema["properties"]["candidates"]["items"]["properties"].keys())
    assert openai_order == candidate_order, f"openai: 结构化输出 schema 顺序被改，实际 {openai_order}"

    anthropic_order = list(
        _sent_tool_schema(_payload_anthropic(tools, schema), "my_agent_structured_output")
        ["properties"]["candidates"]["items"]["properties"].keys()
    )
    assert anthropic_order == candidate_order, f"anthropic: 结构化输出 schema 顺序被改，实际 {anthropic_order}"


# LLM: tool_use.input 是模型给的参数对象，键序无语义；规范化把它按排序往返固定，
#   与 _openai_function_call 同口径，让重载前后 `input` 子树字节一致。
# 函数用途: 单独钉住 tool_use.input 的规范键序。
def test_anthropic_history_normalization_sorts_tool_use_input_only():
    from agent_py_agent.agent.backends.anthropic_prompt_cache import (
        normalize_anthropic_history_blocks,
    )

    messages = [{"role": "assistant", "content": [
        {"type": "text", "text": "x"},
        {"type": "tool_use", "id": "c1", "name": "read_file",
         "input": {"path": "a", "mode": "r"}},
    ]}]
    normalized = normalize_anthropic_history_blocks(messages)

    block = normalized[0]["content"][1]
    assert list(block.keys()) == ["type", "id", "name", "input"]
    assert list(block["input"].keys()) == ["mode", "path"], "input 必须按排序往返固定"
    # 未声明块类型的对象原样返回，不猜字段
    raw = [{"role": "user", "content": [{"type": "custom_block", "b": 1, "a": 2}]}]
    assert normalize_anthropic_history_blocks(raw) == raw


# LLM: 声明表只固定已知块的键名顺序；声明之外的键必须追加保留并保持相对顺序——将来协议新增
#   字段时，丢字段比键序不稳更严重。这里钉住「不丢字段」这个合同。
# 函数用途: 钉住已知块带声明外键时键序与字段都保留（外键按原始相对顺序追加在声明键之后）。
def test_anthropic_history_normalization_keeps_undeclared_block_keys():
    from agent_py_agent.agent.backends.anthropic_prompt_cache import (
        normalize_anthropic_history_blocks,
    )

    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "c1", "name": "read_file",
         "input": {"path": "a"}, "zzz_extra": 1, "aaa_extra": 2},
    ]}]
    normalized = normalize_anthropic_history_blocks(messages)

    block = normalized[0]["content"][0]
    assert list(block.keys()) == ["type", "id", "name", "input", "zzz_extra", "aaa_extra"], (
        "声明外键必须追加保留且保持相对顺序（不是字母序、更不能丢）"
    )
    assert block["zzz_extra"] == 1 and block["aaa_extra"] == 2


# LLM: 已知边界（ck3fix，2026-10-05）：tool_result.content 是块列表时不递归规范化，嵌套块键序
#   保持原样。当前生产构造点恒为字符串，所以不触发重载分叉；将来改成递归时本用例会变红，
#   提醒同步更新 normalize_anthropic_history_blocks 的边界注释。
# 函数用途: 记录「块列表不递归」的当前行为，作为已知边界的回归钉。
def test_anthropic_history_normalization_does_not_recurse_into_tool_result_blocks():
    from agent_py_agent.agent.backends.anthropic_prompt_cache import (
        normalize_anthropic_history_blocks,
    )

    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1",
         "content": [{"text": "结果", "type": "text"}], "is_error": False},
    ]}]
    normalized = normalize_anthropic_history_blocks(messages)

    block = normalized[0]["content"][0]
    assert list(block.keys()) == ["type", "tool_use_id", "content", "is_error"]
    nested = block["content"][0]
    assert list(nested.keys()) == ["text", "type"], (
        "已知边界：块列表不递归规范化，嵌套键序保持原样；若此处变红说明改成了递归，需同步更新注释"
    )
    assert nested["text"] == "结果"
