"""共享 owner 账本被别的会话裁剪时，刚结清的长调用仍要签发来源，完整工具调用照常执行。

现场（DeepSeek Chat 流式、24 个会话共用同一 Gateway owner agent）：第二次模型调用流了约 222 秒，
写文件参数完整到达、finish_reason=tool_calls，却 tool_count=0、零修复、最终 USER_REPLY_UNAVAILABLE。
owner agent 的模型调用账只保留最近 max_records 条明细；长调用期间别的会话开满新调用后，本调用一结清
就不再算"在途"，别处下一次开调用即把它裁掉。若这发生在结清与签发来源之间，收尾拿不到调用明细、
不签发来源，来源核验把这条完整响应改写成空正文失败（MODEL_RESPONSE_PROVENANCE_MISSING）。

本文件走真实 OpenAI-compatible SSE 解析、真实 execute_tool_loop、生成收尾与来源核验；
只替换 HTTP 入口、prompt 装配和工具 handler，不联网、不写文件。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import _tool_loop_service as tls
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
)
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

_RUN_ID = "opusfix-e-run"
_MODEL = "deepseek-v4.1-flash"
_FINAL_TEXT = "文件已写好。"
# 与现场同量级（约 14K 字符）的长正文，按小片段流出。
_LONG_CONTENT = "".join(f"line {index}: {'x' * 60}\n" for index in range(200))


# 类用途: 记录参数的工具替身，经真实 canonical ToolExecutor 执行；不读写任何文件。
class _RecordingTool(BaseTool):
    __test__ = False

    def __init__(self, name: str, properties: tuple[str, ...]) -> None:
        self.calls: list[dict[str, object]] = []
        self.model_spec = make_test_model_spec(name, input_schema={
            "type": "object", "properties": {key: {"type": "string"} for key in properties},
            "required": list(properties), "additionalProperties": False,
        })
        self.runtime_policy = make_test_runtime_policy("read_only", resource_parameters=("path",))

    def execute(self, params):
        self.calls.append(dict(params))
        return ToolHandlerOutcome(self.model_spec.name, True, f"ok:{params['path']}")


# 函数用途: 生成一条 OpenAI-compatible 流式数据行（choices[0].delta 形态，可带 finish_reason 与用量）。
def _chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
    body: dict[str, object] = {"id": "chatcmpl-opusfix-e", "object": "chat.completion.chunk", "model": _MODEL,
                               "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body, ensure_ascii=False)


# 函数用途: DeepSeek 方言的完整工具流：先 reasoning_content，再单个 tool_calls（首片带 id/name，其余只带参数片段），
#   finish_reason=tool_calls 与用量同帧，最后 [DONE]。
def _tool_call_stream(call_id: str, name: str, arguments: dict[str, str]) -> list[str]:
    text = json.dumps(arguments, ensure_ascii=False)
    return [
        _chunk({"role": "assistant", "content": None, "reasoning_content": "先看目录，再一次写完文件。"}),
        _chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": ""}}]}),
        *(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": text[start:start + 97]}}]})
          for start in range(0, len(text), 97)),
        _chunk({}, "tool_calls", {"prompt_tokens": 1200, "completion_tokens": 900, "total_tokens": 2100}),
        "[DONE]",
    ]


# 函数用途: 写入成功后的普通收尾流。
def _final_text_stream() -> list[str]:
    return [
        _chunk({"role": "assistant", "content": "", "reasoning_content": "写入已成功，直接收尾。"}),
        _chunk({"content": _FINAL_TEXT}),
        _chunk({}, "stop", {"prompt_tokens": 1300, "completion_tokens": 20, "total_tokens": 1320}),
        "[DONE]",
    ]


# 类用途: 同一 owner agent 的共享模型调用账，外加"别的会话"在同一账本上开调用/结清的节奏；替身只记账，不发请求。
class _BusyOwnerLedger:
    __test__ = False

    def __init__(self) -> None:
        self.ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=self._on_record))
        self._sequence = 0

    # 函数用途: 别的会话完成一次完整调用（开始 + 结清），在共享账本上留下一条更新的明细。
    def other_session_call(self) -> None:
        self._sequence += 1
        call_id = f"other-session-call-{self._sequence}"
        self.ledger.started(ModelCallStartedParams(call_id=call_id, backend="openai_compatible", model=_MODEL,
                                                   input_tokens=1000, request_id=f"{call_id}-req", run_id=f"{call_id}-run"))
        self.ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=10, input_tokens=1000))

    # 函数用途: 长调用仍在流式时，别的会话累计开满整个明细窗口。
    def fill_window(self) -> None:
        for _ in range(self.ledger.options.max_records):
            self.other_session_call()

    # 函数用途: 本 run 的调用刚结清时，别的会话恰好开下一次调用（现场这段是遥测与调用记录落盘的间隙）。
    def _on_record(self, record) -> None:
        if record.run_id == _RUN_ID and record.status == "finished":
            self.other_session_call()


# 类用途: 按序回放 SSE 数据行的 HTTP 入口替身；可在某次请求流到一半时插入别的会话的调用。
class _SseTransport:
    __test__ = False

    def __init__(self, streams: list[list[str]], during: dict[int, object]) -> None:
        self.streams = streams
        self.during = during
        self.payloads: list[dict] = []

    def __call__(self, path, payload, headers, options=None):
        self.payloads.append(payload)
        number = len(self.payloads)
        lines = self.streams[number - 1]
        hook = self.during.get(number)

        def lines_with_hook():
            for index, line in enumerate(lines):
                if callable(hook) and index == len(lines) - 2:
                    hook()
                yield line

        return lines_with_hook()


# 函数用途: 构造一次主工具循环的运行参数（native 协议、两个真实运行快照工具）。
def _params(tools: dict[str, _RecordingTool]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="在当前目录写一个长文件",
        memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="",
        tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=None, write_boundary=None, task_attributes={},
        request_id=f"{_RUN_ID}-req", run_id=_RUN_ID, task_id=f"{_RUN_ID}-task",
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=_RUN_ID, source_protocol="native"),
        tool_runtime_snapshot=runtime_snapshot_for_tools(tools, run_id=_RUN_ID),
        tool_ir_history=[], save=False, delivery_contract={},
    )


# 类用途: 只把本轮工具规格交给出站组包，让请求像现场一样带着工具表；不另建工具目录或执行入口。
class _Registry:
    __test__ = False

    def __init__(self, tools: dict[str, _RecordingTool]) -> None:
        self._specs = [tool.model_spec for tool in tools.values()]

    def model_visible_specs(self, *, allowed_tools=None, loaded_tool_names=None, runtime_snapshot=None):
        return list(self._specs)


# 类用途: 只记录 prompt 组装结果的替身，产品渲染路径照常调用它。
class _Prompts:
    __test__ = False

    def build(self, user_prompt, memories, *, inject=None, prompt_files=None, **kwargs):
        return "\n".join([str(user_prompt), *(str(item) for item in (inject or []))])


# 函数用途: 跑一次真实工具循环：第 1 轮短调用列目录，第 2 轮长调用流出完整写文件，第 3 轮收尾。
def _drive(monkeypatch, tmp_path: Path):
    tools = {"list_files": _RecordingTool("list_files", ("path",)),
             "write_file": _RecordingTool("write_file", ("path", "content"))}
    busy = _BusyOwnerLedger()
    transport = _SseTransport(
        [_tool_call_stream("call-list", "list_files", {"path": "."}),
         _tool_call_stream("call-write", "write_file", {"path": "kv_store.py", "content": _LONG_CONTENT}),
         _final_text_stream()],
        during={2: busy.fill_window},
    )
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://example.invalid/v1", api_key="fake-unused", model_name=_MODEL, stream_enabled=True,
    ))
    backend.request_stream = transport
    backend.request_stream_iter = transport
    # request_timeout=0 走直调路径：没有保护线程自己的保留句柄，结清后的窗口确定可复现。
    agent = SimpleNamespace(
        backend=backend, prompts=_Prompts(), tools=_Registry(tools), root=str(tmp_path), _current_subagent_run_id="",
        config=SimpleNamespace(request_timeout=0, enable_tools=True, max_tool_rounds=0),
        _model_call_ledger=busy.ledger,
    )
    params = _params(tools)

    # 函数用途: 经真实 canonical ToolExecutor 执行替身工具，调用身份沿用模型给出的 call_id。
    def execute_one(loop_agent, request):
        return execute_canonical_test_call(
            tmp_path, tools=tools, tool_name=request.call.tool_name,
            arguments=dict(request.call.arguments), call_id=request.call.call_id, run_id=_RUN_ID,
        )

    with monkeypatch.context() as patch:
        patch.setattr(tls, "execute_one_tool_call", execute_one)
        patch.setattr(tls, "build_tool_loop_prompt",
                      lambda _agent, loop_params: tls._render_tool_loop_prompt(agent, loop_params))
        result = tls.execute_tool_loop(agent, params)
    return result, tools, transport, busy


def test_long_stream_tool_call_still_executes_when_other_sessions_trim_the_shared_ledger(monkeypatch, tmp_path):
    result, tools, transport, busy = _drive(monkeypatch, tmp_path)
    response = result.final_response

    # 缺陷形态：完整工具调用被来源核验改写成空正文失败，工具零执行、零修复。
    assert response.runtime_reason == "", f"完整工具调用不能被改写成 {response.runtime_reason}"
    assert tools["write_file"].calls == [{"path": "kv_store.py", "content": _LONG_CONTENT}]
    assert tools["list_files"].calls == [{"path": "."}]
    assert response.text == _FINAL_TEXT
    assert response.runtime_status == "ok"
    assert result.tool_rounds == 2
    assert [sorted(tool["function"]["name"] for tool in payload["tools"]) for payload in transport.payloads] == [
        ["list_files", "write_file"]] * 3

    # 保留只覆盖收尾：之后别的会话继续开调用，本 run 的明细照常按窗口裁掉，不会被永久钉住。
    busy.fill_window()
    assert not [record.call_id for record in busy.ledger.records() if record.run_id == _RUN_ID]
