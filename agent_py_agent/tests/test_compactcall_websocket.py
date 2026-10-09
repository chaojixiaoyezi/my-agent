# LLM: 用真实OAuth/WS组包、主回合worker与辅助调用，只替换凭据入口、socket及合成动态事实；不读生产材料。
# 模块用途: 逐字段核对订阅主请求和两条压缩路径的完整握手与response.create字节。
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.oauth import OAuthResponsesBackend
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import ConversationHistorySeed
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.gateway_compact_context import build_gateway_compact_load_request
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.registry import _search_deferred_tool_result
from agent_py_agent.agent.tooling.tool_search_state import pending_carried_loaded_tool_names
from agent_py_agent.tests.test_compact_remote_provider import BLOCK
from agent_py_agent.tests.test_midturn_compact_prefix import _prepare_run


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


# LLM: 原注册表真的搜索并产生typed加载信封；延迟种类只用于合成测试，权限仍由原快照约束。
# 函数用途: 准备临时agent与已搜索的粘性工具，不依赖生产owner或真实凭据。
def _setup(tmp_path, monkeypatch, remote):
    agent, thread, prior = _prepare_run(tmp_path, remote)
    agent.config.max_tokens = 65536
    agent.config.model_context_window_tokens = 262144
    agent.config.model_reasoning_effort = "high"
    agent.config.memory_compact_auto_trigger_percent = 40
    old = agent.backend
    agent.backend = OAuthResponsesBackend(BackendOptions(
        api_base="https://chatgpt.example.test/backend-api/codex", api_key="", model_name=old.model_name,
        max_tokens=65536, reasoning_control="effort", reasoning_levels=("low", "medium", "high"),
    ), auth_ref={"mode": "chatgpt", "path": "unused", "provider_id": "fake", "generation": "g", "binding": "b"})
    agent.backend.probe_tool_capability = old.probe_tool_capability
    monkeypatch.setattr("agent_py_agent.agent.settings.model_oauth.request_credentials",
                        lambda *_args: ("synthetic", {"ChatGPT-Account-ID": "synthetic-account"}))
    category = next(spec.category for spec in agent.tools.specs() if spec.name == "read_file")
    agent.tools.catalog_deferred_categories = [category]
    outcome = _search_deferred_tool_result(agent.tools, "read_file", limit=1, snapshot=agent.tools.runtime_snapshot())
    assert outcome.ok and outcome.result_envelope["tool_search"]["loaded_tool_names"] == ["read_file"]
    records = [{"tool": "tool_search", "tool_round": 1, "ok": True, "params": {"query": "read_file"},
                "tool_result_envelope": outcome.result_envelope, "output": outcome.output}]
    records.append({"tool": "list_files", "tool_round": 2, "ok": True, "output": "synthetic"})
    assert pending_carried_loaded_tool_names(records) == {"read_file"}
    original = PromptBuilder.prepare_render_input

    def prepare(builder, request):
        return replace(original(builder, request), persona_updates="# 人格增量\n合成差异",
                       deployment_context="# 当前部署\n/synthetic/runtime")

    monkeypatch.setattr(PromptBuilder, "prepare_render_input", prepare)
    return agent, thread, prior, records


# LLM: 真WS客户端connect和send是观察点；保存完整序列化字节及全部握手参数，终态事件保持原解析形状。
# 类用途: 一个不联网的socket，业务回合可触发真实压缩、辅助摘要返回合法结果。
class _Socket:
    def __init__(self, probe, handshake):
        self.probe, self.handshake = probe, handshake
        self.events = []

    def send(self, wire):
        payload = json.loads(wire)
        compact = self.probe.compacting or payload["input"][-1].get("type") == "compaction_trigger"
        entry = {**self.handshake, "wire": wire, "body": payload, "worker": threading.current_thread().name, "compact": compact}
        self.probe.calls.append(entry)
        if compact:
            self.probe.finished = True
            output = BLOCK["item"] if self.probe.remote else {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": self.probe.summary}]}
        elif self.probe.live and not self.probe.finished:
            search = len(self.probe.calls) == 1
            output = {"type": "function_call", "name": "tool_search" if search else "read_file",
                      "call_id": f"tool-{len(self.probe.calls)}",
                      "arguments": '{"query":"read_file","limit":1}' if search else '{"path":"fake.txt"}'}
        else:
            output = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "完成"}]}
        self.events = [json.dumps({"type": "response.output_item.done", "item": output}),
                       json.dumps({"type": "response.completed", "response": {"status": "completed", "output": []}})]

    def recv(self, timeout=None):
        return self.events.pop(0)

    def close(self):
        pass

    def close_socket(self):
        pass


class _Probe:
    def __init__(self, monkeypatch, remote, live):
        self.calls, self.remote, self.live = [], remote, live
        self.compacting, self.finished = False, False
        self.summary = "[compact-live-handoff.v1]\ncurrent_progress: 已读取\nuser_constraints: 保留要求\ncompleted: 文件\nfailures: 无\nunresolved: 继续\nnext_step: 核对"
        monkeypatch.setattr("websockets.sync.client.connect", self.connect)
        from agent_py_agent.agent.conversation import compact_request_budget
        original = compact_request_budget.generate_auxiliary_model_response

        def generate(request):
            self.compacting = True
            try:
                return original(request)
            finally:
                self.compacting, self.finished = False, True

        monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", generate)

    def connect(self, url, **options):
        return _Socket(self, {"url": url, "options": deepcopy(options)})


def _run(agent, thread, prior, records):
    return agent.run("本轮要求", params=_run_params(agent, thread, prior, records))


def _run_params(agent, thread, prior, records):
    return RunParams(
        save=False, context_scope="conversation", request_id="compactcall2", run_id="main-run",
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id},
        carried_archive_tool_calls=records, conversation_history_seed=ConversationHistorySeed(canonical_messages=prior),
        compact_context=AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE,
            resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)),
    )


def _assert_pair(main, compact):
    assert main["url"] == compact["url"] and main["url"].startswith("wss://")
    assert main["options"] == compact["options"], "完整WS握手"
    a, b = main["body"], compact["body"]
    assert a["prompt_cache_key"] == b["prompt_cache_key"] == main["options"]["additional_headers"]["session-id"]
    assert "read_file" in {tool["name"] for tool in a["tools"]}
    # 对不存在的字段同样比存在性，不把None和缺失当成相同。
    for field in sorted((a.keys() | b.keys()) - {"input"}):
        assert (field in a, _encode(a.get(field))) == (field in b, _encode(b.get(field))), field
    assert a["reasoning"] == {"effort": "high"}
    for omitted in ("max_output_tokens", "parallel_tool_calls", "text", "truncation"):
        assert omitted not in a and omitted not in b
    assert a["include"] == ["reasoning.encrypted_content"] and a["store"] is False
    assert "合成差异" in str(a["input"]) and "/synthetic/runtime" in str(a["input"])
    print("compactcall2_fields", {field: a.get(field, "absent") for field in (
        "prompt_cache_key", "tool_choice", "parallel_tool_calls", "reasoning", "text", "include", "store",
        "truncation", "max_output_tokens")}, "tools", len(a["tools"]), len(b["tools"]),
        "workers", main["worker"], compact["worker"], "bytes", len(main["wire"]), len(compact["wire"]))


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "text"])
def test_subscription_ws_live_route_and_full_prefix(tmp_path, monkeypatch, remote):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, remote)
    probe = _Probe(monkeypatch, remote, live=True)
    monkeypatch.setattr(agent.tools.tools["read_file"], "execute",
                        lambda _params: ToolHandlerOutcome(tool="read_file", ok=True, output="x" * 280_000))
    result = _run(agent, thread, prior, records)
    (tmp_path / "all-wires.json").write_text(json.dumps(probe.calls, ensure_ascii=False), encoding="utf-8")
    assert result.response == "完成"
    index = next(i for i, call in enumerate(probe.calls) if call["compact"])
    main, compact = probe.calls[index - 1:index + 1]
    (tmp_path / "wire-pair.json").write_text(json.dumps([main, compact], ensure_ascii=False), encoding="utf-8")
    _assert_pair(main, compact)
    assert _encode(compact["body"]["input"][:len(main["body"]["input"])]) == _encode(main["body"]["input"])
    assert all(call["body"]["tools"] == probe.calls[0]["body"]["tools"] for call in probe.calls)
    assert any(item.get("type") == "function_call" and item.get("name") == "tool_search"
               for item in compact["body"]["input"])
    assert main["worker"] != threading.current_thread().name, "真实主模型guard线程必须跑到"


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "text"])
def test_subscription_ws_preflight_route_and_full_prefix(tmp_path, monkeypatch, remote):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, remote)
    agent.config.memory_compact_auto_trigger_percent = 90
    for number in range(32):
        agent.conversation_store.messages.append({"thread_id": thread.thread_id,
            "role": "user" if number % 2 == 0 else "assistant", "content": "合成历史" * 1000, "metadata": {}})
    prior = provider_history_messages_from_rows(agent.conversation_store.messages.recent(thread.thread_id, limit=0))
    probe = _Probe(monkeypatch, remote, live=False)
    assert _run(agent, thread, prior, records).response == "完成"
    load_request = build_gateway_compact_load_request(SimpleNamespace(
        agent=agent, request={}, request_id="compactcall2", on_chunk=None), "本轮要求",
        _run_params(agent, thread, prior, records), records)
    assert load_request.model_surface.loaded_tool_names == ("read_file",)
    agent.config.memory_compact_auto_trigger_max_tokens = 100000
    # 不复制ContextVar，预检辅助调用必须凭明确thread_id重新绑定，不能靠主worker残留scope。
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="preflight-no-context") as executor:
        prepared = executor.submit(prepare_conversation_context, agent, agent.conversation_store, thread,
            options=ConversationCompactOptions(model_surface=load_request.model_surface)).result()
    assert prepared.compacted and prepared.thread.compact_generation == 1
    main, compact = probe.calls[0], probe.calls[-1]
    (tmp_path / "wire-pair.json").write_text(json.dumps([main, compact], ensure_ascii=False), encoding="utf-8")
    _assert_pair(main, compact)
    # 开头压缩只挑canonical旧历史；当前要求、工作区/部署事实在尾部，不应混入旧历史。
    assert compact["worker"].startswith("preflight-no-context")
    a, b = main["body"]["input"], compact["body"]["input"]
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if _encode(x) != _encode(y)), min(len(a), len(b)))
    print("preflight_first_input_difference", first, "canonical_history", len(prior), "counts", len(a), len(b))
    covered = resolve_compact_summary_view(agent, prepared.thread, THREAD_COMPACT_SCOPE).source_message_ids
    assert first == len(covered) and 0 < first <= len(prior)
    assert _encode(b[:first]) == _encode(a[:first])
    assert "合成差异" in str(b[first:]) and "/synthetic/runtime" not in str(b[first:])


def test_sticky_loading_does_not_grant_tools_or_cross_thread_records(tmp_path, monkeypatch):
    agent, _, _, records = _setup(tmp_path, monkeypatch, True)
    loaded = pending_carried_loaded_tool_names(records)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["list_files"])
    specs = agent.tools.model_visible_specs(allowed_tools=["list_files"], loaded_tool_names=loaded, runtime_snapshot=snapshot)
    assert {spec.name for spec in specs} == {"list_files"}
    assert pending_carried_loaded_tool_names([]) == set()
