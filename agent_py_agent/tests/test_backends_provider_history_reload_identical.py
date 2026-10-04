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
