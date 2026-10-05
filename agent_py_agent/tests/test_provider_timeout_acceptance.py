"""LLM: 门槛5「可恢复超时后模型只承诺不动作 → 轮内有界续跑一次」(ebb90d0c) 的对抗性验收用例。

被测改动两处产品代码：
- 生成层 ``agent_core/tool_model_generation.py``：门槛5 重试**成功**分支登记结构化事实
  ``live_archive_state["_provider_timeout_resume_turns"][model_turn]``。
- 裁决层 ``agent_core/tool_loop/response_decision.py``：同一 model turn 上、模型零工具调用时
  至多放行一次宿主追问（``_PROVIDER_TIMEOUT_RESUME_COUNT = 1``）。该追问（R1 残留边界后）
  只是**探针**：只回答「还有没有真实工具工作」。探针零工具调用 = 没有工具工作要继续 ->
  两枪已产生的合法答复**按到达顺序无损合成同一次交付**（逐字投影、只加段落边界，不按长度二选一、
  不读正文做语义判定；只有该段自己被截断或正文为空时才不投影，且排除原因结构化入账）；
  探针带工具调用 -> 登记被消费且不参与交付投影，工具按既有工具轮路径执行。

本文件与作者单测 ``test_provider_timeout_continuation.py`` 的分工：**不复用其断言**，只做四类对抗检查：
1. 重复工具副作用：真实 ``execute_tool_loop`` + 真实工具 handler 计数 + 真实产品记账
   （``_record_tool_call`` / IR 账本）。证明探针只多发一次模型采样、探针零工具调用时只做一次
   无损交付合成（两条候选按到达顺序逐字保留），``_run_tool_round``、
   工具 handler、工具账本在探针前后完全一致；并单独构造「工具已执行但结果尚未回灌」窗口的反例。
2. 正常回答不被无端续跑：本轮物理调用零失败（含「同一 params 里更早的物理调用失败过」的陈旧事实）
   时，同样的承诺文本必须原样交付、续跑计数 0、账本无新调用。
3. 预算独立性：截断续跑与超时续跑各自计数互不吃；超时续跑用尽后按原语义交付原响应。
4. fail-closed 四闸门的行为面复核：``exc.stage`` / ``_has_ambiguous_active_turn_input`` /
   ``_ir_last_tool_use_confirmed`` / ``_logical_physical_attempt_count`` 每条闸门都独立拒绝，
   且拒绝时**不产生任何物理尝试**、不登记续跑事实。

受控边界（不得当成真机证据）：全部用例使用 fake 后端 / fake LLM 与本地构造的 IR 历史，
不含任何真实供应商超时。真机验收步骤与判定标准见验收报告，不在本文件内执行。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _EMPTY_TEXT_NUDGE,
    _PROVIDER_TIMEOUT_RESUME,
    _PROVIDER_TIMEOUT_RESUME_COUNT,
    _TRUNCATED_OUTPUT_RESUME,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _generate_through_wall_guard,
    _ir_last_tool_use_confirmed,
    _logical_physical_attempt_count,
    _retry_once_after_timeout,
    _start_model_generation,
    generate_model_response,
    provider_timeout_resume_eligible,
)
from agent_py_agent.agent.backends import BackendOptions, ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)

# 供应商超时预算必须极小，才能让「挂起」的 fake 后端被墙钟保护拿下（与产品默认值无关）。
_TINY_REQUEST_TIMEOUT = 0.02
# 超时后模型只回一句承诺——真机 ma-port-2 的原始形状；换成任意其它文本行为必须一致。
_PROMISE = "在的，刚才超时了，我重新来。"
_FINAL = "已从断点继续并完成剩余工作。"
_READ_ARGS = {"path": "notes.md"}

_READ_SPEC = make_test_model_spec(
    "read_file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------- 测试夹具


# LLM: 假 agent 只需生成层/工具轮真正读取的字段：backend、config.request_timeout、
#   config.enable_tools、config.max_tool_rounds、prompts（被 _render_tool_loop_prompt 读取）、
#   root（产品记账路径写 blob/archive 时的 owner 根）、_current_subagent_run_id。
#   不要给测试塞真实 agent；任何产品字段缺失都应该在测试里显式暴露成 AttributeError。
# 函数用途: 构造调用真实工具轮/生成路径所需的最小宿主外壳。
class _FakeAgent(SimpleNamespace):
    __test__ = False

    def __init__(self, backend: object, prompts: object, root: Path) -> None:
        super().__init__(
            backend=backend,
            config=SimpleNamespace(
                request_timeout=_TINY_REQUEST_TIMEOUT,
                enable_tools=True,
                max_tool_rounds=0,
            ),
            prompts=prompts,
            root=str(root),
            _current_subagent_run_id="",
        )


# LLM: 每次调用一个新的 params 实例：live_archive_state 是本 run 的结构化事实容器，
#   不能被测试之间串用；native 协议 + 真实工具运行快照是工具轮的真实形态。
# 函数用途: 构造一次工具模型轮所需的运行参数。
def _params(*, run_id: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="把剩下的工作做完",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id=f"{run_id}-req",
        run_id=run_id,
        task_id=f"{run_id}-task",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id=run_id,
            source_protocol="native",
        ),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((_READ_SPEC,), run_id=run_id),
        tool_ir_history=[],
        save=False,
        delivery_contract={},
    )


# LLM: 只记录真正发给模型的出站文本；测试不复制宿主 prompt 模板，只断言结构化指令出现了几次。
# 类用途: 记录每次 prompt 组装结果，供「续跑指令恰好出现一次」这类断言语义正确性。
class _RecordingPrompts:
    __test__ = False

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, user_prompt, memories, *, inject=None, prompt_files=None, **kwargs):
        tools = kwargs.get("tools")
        tool_context = list(getattr(tools, "tool_context", ()) or ()) if tools else []
        text = "\n".join(
            [str(user_prompt)]
            + [str(item) for item in (inject or [])]
            + [str(item) for item in tool_context]
        )
        self.built.append(text)
        return text


# LLM: 脚本后端按序号回放「工具调用 / 挂起超时 / 承诺正文 / 截断正文 / 正常正文」；
#   每次进入一次物理采样前先拍一次工具账本指纹，用来证明「续跑那一枪进入时账本零增量」。
# 类用途: 模拟可按剧本复现供应商时序的假后端。
class _ScriptedBackend:
    __test__ = False

    name = "scripted-acceptance-backend"

    def __init__(self, script: list[object], fingerprint=None) -> None:
        self.script = list(script)
        self.calls = 0
        self.prompts: list[str] = []
        self.entries: list[dict[str, object]] = []
        self._fingerprint = fingerprint

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self._fingerprint is not None:
            self.entries.append(self._fingerprint())
        step = self.script[min(self.calls, len(self.script)) - 1]
        if step == "TIMEOUT":
            time.sleep(0.5)  # > request_timeout -> 墙钟超时
            return ModelResponse(text="", backend=self.name)
        if isinstance(step, tuple):
            kind = step[0]
            if kind == "tool":
                _, name, arguments = step
                return ModelResponse(
                    text="",
                    backend=self.name,
                    tool_use_blocks=[
                        {
                            "id": f"call-{self.calls}",
                            "name": name,
                            "input": dict(arguments),
                        }
                    ],
                )
            if kind == "truncated":
                return ModelResponse(text=str(step[1]), backend=self.name, truncated=True)
        return ModelResponse(text=str(step), backend=self.name)


# LLM: 计数 read_file handler：handler 执行次数就是「工具副作用是否被重放」的最终判据，
#   它不依赖任何被测产品的记账代码，避免自证。
# 类用途: 真实注册进 canonical ToolExecutor 的只读计数工具。
class _CountingReadTool(BaseTool):
    __test__ = False

    def __init__(self) -> None:
        self.handler_calls = 0
        self.model_spec = _READ_SPEC
        self.runtime_policy = make_test_runtime_policy("read_only", resource_parameters=("path",))

    def execute(self, params):
        self.handler_calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, f"read:{params['path']}")


# LLM: 工具账本指纹只取叶子事实（已执行工具名、IR 里的 ToolResult call_id），不对活对象
#   做序列化/递归枚举（仓库铁律：活数据不序列化）。
# 函数用途: 拍一份「工具是否被再次执行」的最小结构化指纹。
def _ledger_fingerprint(params: object) -> dict[str, object]:
    history = list(getattr(params, "tool_ir_history", None) or [])
    results = [item.call_id for item in history if isinstance(item, ToolResult)]
    return {
        "executed_tools": list(getattr(params, "executed_tools", None) or []),
        "tool_result_ids": results,
    }


# LLM: 一次真实工具轮循环的全部可观测事实；测试只读这些结构字段，不读模型正文做判定。
# 类用途: 承载真实 execute_tool_loop 跑完后的账本/时序证据。
@dataclass
class _LoopRun:
    backend: _ScriptedBackend
    tool: _CountingReadTool
    params: ToolLoopExecuteParams
    response: object
    rounds: int
    tool_round_calls: list[int]
    executed_one_calls: int
    agent: _FakeAgent


# LLM: 真实循环 + 真实工具执行 + 真实产品记账路径都保留，只把「哪个工具 handler」换成计数
#   只读工具（canonical ToolExecutor 真跑），因此工具副作用次数不是桩件自证。
# 函数用途: 局部替换测试工具后运行真实循环，立即还原替换，再返回结构化证据。
def _drive_loop(monkeypatch, tmp_path: Path, script: list[object], *, run_id: str) -> _LoopRun:
    from agent_py_agent.agent.agent_core import _tool_loop_service as tls

    params = _params(run_id=run_id)
    tool = _CountingReadTool()
    backend = _ScriptedBackend(script, fingerprint=lambda: _ledger_fingerprint(params))
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    counts = {"rounds": [], "exec_one": 0}
    real_run_tool_round = tls._run_tool_round

    # LLM: 包装真实工具轮，仅记录进入次数；显式核对宿主，防止多次驱动串用上一轮闭包。
    # 函数用途: 记录当前轮号后继续执行原工具轮及产品记账。
    def counting_tool_round(loop_agent, request):
        assert loop_agent is agent and request.agent is agent
        counts["rounds"].append(request.tool_rounds)
        return real_run_tool_round(loop_agent, request)

    # LLM: 只替换具体工具 handler，canonical ToolExecutor 仍实际执行，不能靠计数桩件自证成功。
    # 函数用途: 核对当前宿主并计数，再让隔离工具执行器运行真实测试工具。
    def counting_execute_one(loop_agent, request):
        assert loop_agent is agent
        counts["exec_one"] += 1
        return execute_canonical_test_call(
            tmp_path,
            tools={tool.model_spec.name: tool},
            tool_name=request.call.tool_name,
            arguments=dict(request.call.arguments),
            call_id=request.call.call_id,
            run_id=run_id,
        )

    # 每次驱动结束即还原模块替换，避免同一测试的后一次驱动包住前一次计数闭包。
    with monkeypatch.context() as loop_patch:
        loop_patch.setattr(tls, "_run_tool_round", counting_tool_round)
        loop_patch.setattr(tls, "execute_one_tool_call", counting_execute_one)
        # 只替换 prompt 装配入口，产品渲染与后续工具记账保持原路径。
        loop_patch.setattr(
            tls,
            "build_tool_loop_prompt",
            lambda _agent, loop_params: tls._render_tool_loop_prompt(agent, loop_params),
        )
        loop_result = tls.execute_tool_loop(agent, params)
        _prompt, response, rounds = loop_result.final_prompt, loop_result.final_response, loop_result.tool_rounds
    return _LoopRun(
        backend=backend,
        tool=tool,
        params=params,
        response=response,
        rounds=rounds,
        tool_round_calls=counts["rounds"],
        executed_one_calls=counts["exec_one"],
        agent=agent,
    )


# LLM: 裁决层输入必须复用生成层登记事实的同一个 params（同源 model turn 序号），
#   测试不另建一套序号来源，也不手写 live_archive_state 里的续跑事实。
# 函数用途: 用真实生成路径产生的 params 构造一次「零工具调用」裁决请求。
def _no_tool_request(
    params: ToolLoopExecuteParams,
    response: object,
    counters: ToolLoopRepairCounters | None = None,
) -> _NoToolCallsRequest:
    return _NoToolCallsRequest(
        agent=SimpleNamespace(config=SimpleNamespace(enable_tools=True)),
        params=params,
        response=response,
        counters=counters or ToolLoopRepairCounters(),
        has_protected_marker=False,
    )


# LLM: 生成层的真实超时路径：第一枪挂起触发门槛5，之后按 fake 后端剧本应答。
# 函数用途: 通过真实 generate_model_response 建立/不建立结构化续跑事实。
def _generate(agent: _FakeAgent, params: ToolLoopExecuteParams, *, tool_rounds: int = 1):
    return generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="把剩下的工作做完",
            tool_rounds=tool_rounds,
        )
    )


# LLM: 只读生成层登记的续跑事实本身（不判断、不改写），用于断言「事实的键 = model turn 序号」。
# 函数用途: 取当前 params 上的续跑事实表（无则空表）。
def _fact_entries(params: ToolLoopExecuteParams) -> dict:
    return params.live_archive_state.get("_provider_timeout_resume_turns") or {}


# ---------------------------------------------------------------- 1. 工具副作用不重复


def test_resume_adds_exactly_one_sample_and_never_replays_the_tool(
    monkeypatch, tmp_path
) -> None:
    """真实循环：工具执行 1 次 → 超时 → 重试 → 承诺 → 探针 1 次（不发工具）。

    R1 残留边界后这一枪是**探针**（只回答「还有没有真实工具工作」）：它零工具调用即
    「没有工具工作要继续」，交付把两枪的合法正文按到达顺序无损合成（承诺在前、探针那一枪在后）。
    采样次数、工具轮、账本、指令出现次数等不变式全部不变；只有"交付正文必须等于其中一条候选"
    这条旧断言随长度 tie-break 一起作废——它本身就是"丢掉一条合法答复"的来源。
    """
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-accept-once",
    )

    assert run.backend.calls == 4, "工具采样 + 超时 + 重试 + 探针；探针之后没有第 5 枪"
    assert run.tool_round_calls == [1], "整轮只允许进入一次工具轮"
    assert run.executed_one_calls == 1
    assert run.tool.handler_calls == 1, "工具 handler 副作用必须恰好一次"
    assert run.rounds == 1
    # 交付：探针零工具调用 -> 两枪的合法正文按到达顺序无损合成（见
    # response_decision._provider_timeout_probe_delivery）。长度不参与任何判定，因此本剧本
    # 等长与否都不影响段落集合与顺序。
    assert len(_PROMISE) == len(_FINAL), "前提：本剧本两段等长"
    assert run.response.text == f"{_PROMISE}\n\n{_FINAL}"

    # 探针指令恰好出现在一次出站请求里（= 这个机制只多买了一枪模型采样）。
    with_instruction = [
        index for index, prompt in enumerate(run.backend.prompts, start=1) if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4], "只有探针那一枪带宿主指令，重试那一枪不带"
    assert "read:notes.md" in run.backend.prompts[3], "探针建立在真实工具结果之上"

    # 关键时序：工具只在第 1 次采样后执行过一次，之后每一次采样进入时账本完全相同。
    assert run.backend.entries[1] == run.backend.entries[2] == run.backend.entries[3]
    assert run.backend.entries[3] == {
        "executed_tools": ["read_file"],
        "tool_result_ids": ["call-1"],
    }
    assert run.backend.entries[0] == {"executed_tools": [], "tool_result_ids": []}
    assert _ledger_fingerprint(run.params) == run.backend.entries[3], "循环结束后账本无新增执行"

    # 产品自己的调用账本同样只记 4 次物理采样；续跑是**新的 logical 调用**，不是第三次重试。
    records = run.agent._model_call_ledger.records()
    assert [(item.status, item.metadata["physical_attempt"]) for item in records] == [
        ("finished", 1),
        ("timed_out", 1),
        ("finished", 2),
        ("finished", 1),
    ]
    assert (
        records[1].metadata["logical_call_id"] == records[2].metadata["logical_call_id"]
    ), "超时与门槛5 重试属于同一 logical turn"
    assert (
        records[3].metadata["logical_call_id"] != records[1].metadata["logical_call_id"]
    ), "续跑是一枪新的模型采样，不是同一 logical turn 的第三次尝试"

    # 续跑事实只登记「被重试救回的那次物理调用」所在 turn；续跑自身不产生新资格。
    entries = _fact_entries(run.params)
    assert len(entries) == 1
    assert provider_timeout_resume_eligible(run.params) is False, "事实不跨 turn 生效"


def test_decision_step_itself_touches_no_tool_ledger(monkeypatch, tmp_path) -> None:
    """裁决层单步对抗：从「承诺响应」到「continue」之间，工具账本一个字节都不能变。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-accept-decide",
    )
    # 用同一份 params 直接重放一次裁决（真实循环已消费预算，这里只验账本不变式）。
    before = _ledger_fingerprint(run.params)
    decision = _no_tool_calls_decision(
        _no_tool_request(
            run.params,
            ModelResponse(text=_PROMISE, backend=run.backend.name),
            ToolLoopRepairCounters(provider_timeout_resume_repairs=_PROVIDER_TIMEOUT_RESUME_COUNT),
        )
    )
    assert decision.action == "break", "预算用尽时按原语义收口"
    assert _ledger_fingerprint(run.params) == before


# ---------------------------------------------------------------- 1b. 反例窗口


def test_timeout_in_executed_but_unrecorded_window_is_fail_closed(tmp_path) -> None:
    """反例：工具已执行但结果**未**回灌（最后一条 tool_use 无配对回执）时绝不重试/续跑。

    构造的时序（时间轴）：
      T0 模型请求 read_file           -> T1 host 执行了工具（副作用已发生）
      T2 host 还没写回 ToolResult（IR 里最后一条 tool_use 仍是孤儿）
      T3 下一次模型调用在墙钟上超时
      T4 门槛5 资格判定拿不到「配对回执」这个结构化事实 -> 拒绝重试 -> 原异常上抛
      => 不可能存在「重试成功 -> 登记事实 -> 续跑」这条链路，也就不可能重放工具。
    这里用「配对错位的 ToolResult」而不是「完全没有 ToolResult」：只要配对不成立就必须
    fail-closed，任何「历史里有任意结果就算确认」的弱化实现都会在这个用例上红。
    """
    params = _params(run_id="run-accept-window")
    executed = canonical_history_call(
        "read_file",
        _READ_ARGS,
        call_id="call-executed",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    other = canonical_history_call(
        "read_file",
        {"path": "other.md"},
        call_id="call-other",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-0",
        attempt_id=params.request_id,
    )
    params.tool_ir_history[:] = [
        AssistantTurn(tool_calls=[executed]),
        canonical_history_result(other, "read:other.md"),  # 回执存在，但配对的是另一条调用
    ]

    backend = _ScriptedBackend([_PROMISE])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    request = ModelGenerateParams(
        agent=agent,
        params=params,
        prompt="把剩下的工作做完",
        tool_rounds=1,
    )

    assert _ir_last_tool_use_confirmed(request) is False, "配对错位 = 未确认"
    assert (
        _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock")) is None
    )
    assert backend.calls == 0, "拒绝重试时不得产生任何物理尝试"
    assert provider_timeout_resume_eligible(params) is False
    assert "_provider_timeout_resume_turns" not in params.live_archive_state

    decision = _no_tool_calls_decision(
        _no_tool_request(params, ModelResponse(text=_PROMISE, backend=backend.name))
    )
    assert decision.action == "break"
    assert decision.counters.provider_timeout_resume_repairs == 0
    assert params.tool_context == []

    # 对照组：同样的夹具只把配对补上，闸门立刻放行 —— 红/绿差异只来自配对事实。
    params_control = _params(run_id="run-accept-window-control")
    # 收窄(R1)后资格还需第二条事实：本轮确实执行过工具（本对照组的 IR 里已有配对工具结果）。
    params_control.executed_tools.append("read_file")
    confirmed = canonical_history_call(
        "read_file",
        _READ_ARGS,
        call_id="call-confirmed",
        source_protocol=params_control.tool_protocol_snapshot.source_protocol,
        run_id=params_control.run_id,
        turn_id=f"{params_control.run_id}:round-1",
        attempt_id=params_control.request_id,
    )
    params_control.tool_ir_history[:] = [
        AssistantTurn(tool_calls=[confirmed]),
        canonical_history_result(confirmed, "read:notes.md"),
    ]
    backend_control = _ScriptedBackend(["TIMEOUT", _PROMISE])
    agent_control = _FakeAgent(backend_control, _RecordingPrompts(), tmp_path)
    request_control = ModelGenerateParams(
        agent=agent_control,
        params=params_control,
        prompt="把剩下的工作做完",
        tool_rounds=1,
    )
    assert _ir_last_tool_use_confirmed(request_control) is True
    response = _generate(agent_control, params_control)
    assert backend_control.calls == 2, "已确认的 tool_use 才允许门槛5 重试"
    assert response.text == _PROMISE
    assert provider_timeout_resume_eligible(params_control) is True, "重试成功才登记事实"


# ---------------------------------------------------------------- 2. 正常回答不被无端续跑


def test_stale_fact_from_earlier_sample_never_resumes_a_healthy_answer(
    monkeypatch, tmp_path
) -> None:
    """同一 params 里更早的物理调用失败过，也不能让**本轮健康回答**被续跑。"""
    params = _params(run_id="run-accept-stale")
    params.executed_tools.append("read_file")  # 收窄(R1)：资格还需「本轮确实执行过工具」
    timeout_backend = _ScriptedBackend(["TIMEOUT", _PROMISE])
    agent = _FakeAgent(timeout_backend, _RecordingPrompts(), tmp_path)
    first = _generate(agent, params)
    assert first.text == _PROMISE and provider_timeout_resume_eligible(params) is True

    # 第二轮物理调用：供应商完全正常（零失败），模型仍回同一句「承诺」。
    healthy_backend = _ScriptedBackend([_PROMISE])
    agent.backend = healthy_backend
    second = _generate(agent, params)
    assert healthy_backend.calls == 1
    assert provider_timeout_resume_eligible(params) is False, "陈旧事实不得作用于新的 turn"

    records = agent._model_call_ledger.records()
    assert records[-1].status == "finished"
    assert [item.status for item in records] == ["timed_out", "finished", "finished"]

    before_ledger = _ledger_fingerprint(params)
    decision = _no_tool_calls_decision(_no_tool_request(params, second))
    assert decision.action == "break"
    assert decision.response is second, "原样交付，不替换响应对象"
    assert decision.counters == ToolLoopRepairCounters(), "续跑计数必须为 0"
    assert params.tool_context == [], "不得回灌任何宿主指令"
    assert _ledger_fingerprint(params) == before_ledger
    assert len(_fact_entries(params)) == 1, "事实仍在但已过期（按 turn 序号精确匹配）"


def test_healthy_loop_delivers_promise_verbatim_without_extra_sample(
    monkeypatch, tmp_path
) -> None:
    """真实循环、零供应商失败：承诺文本原样收口，只发生一次模型采样，零工具轮。"""
    run = _drive_loop(
        monkeypatch, tmp_path, [_PROMISE], run_id="run-accept-healthy"
    )

    assert run.backend.calls == 1
    assert len(run.backend.prompts) == 1
    assert _PROVIDER_TIMEOUT_RESUME not in run.backend.prompts[0]
    assert run.response.text == _PROMISE
    assert run.response.runtime_status == "ok"
    assert run.tool_round_calls == []
    assert run.executed_one_calls == 0 and run.tool.handler_calls == 0
    assert run.params.tool_context == []
    assert "_provider_timeout_resume_turns" not in run.params.live_archive_state
    records = run.agent._model_call_ledger.records()
    assert [item.status for item in records] == ["finished"], "账本无新调用、无失败"
    assert run.agent._model_call_ledger.records()[-1].metadata["physical_attempt"] == 1


# ---------------------------------------------------------------- 3. 预算独立


def test_provider_resume_does_not_consume_any_other_repair_budget(tmp_path) -> None:
    """超时续跑只在自己的格子上 +1，其它修复计数逐字段不变。"""
    params = _params(run_id="run-accept-budget")
    params.executed_tools.append("read_file")  # 收窄(R1)：资格还需「本轮确实执行过工具」
    agent = _FakeAgent(_ScriptedBackend(["TIMEOUT", _PROMISE]), _RecordingPrompts(), tmp_path)
    response = _generate(agent, params)
    assert provider_timeout_resume_eligible(params) is True

    counters = ToolLoopRepairCounters(
        protected_marker_repairs=3,
        unresolved_runtime_issue_redirects=4,
        protocol_repairs=5,
        empty_text_repairs=6,
        truncated_write_repairs=7,
        truncated_output_repairs=8,
    )
    decision = _no_tool_calls_decision(_no_tool_request(params, response, counters))

    assert decision.action == "continue"
    assert decision.counters == replace(counters, provider_timeout_resume_repairs=1), (
        "除超时续跑自己的格子外，任何计数都不许被改写"
    )


def test_exhausted_provider_budget_hands_over_to_truncated_resume(tmp_path) -> None:
    """超时续跑预算用尽后，截断续跑仍按自己的预算独立生效。"""
    params = _params(run_id="run-accept-budget-truncated")
    params.executed_tools.append("read_file")  # 收窄(R1)：资格还需「本轮确实执行过工具」
    agent = _FakeAgent(_ScriptedBackend(["TIMEOUT", _PROMISE]), _RecordingPrompts(), tmp_path)
    _generate(agent, params)
    assert provider_timeout_resume_eligible(params) is True

    # 同一模型调用既「有超时资格」又「正文被输出上限截断」：先让超时预算用尽。
    truncated = ModelResponse(text="这是被输出上限截断的半句答复", backend="x", truncated=True)
    counters = ToolLoopRepairCounters(
        provider_timeout_resume_repairs=_PROVIDER_TIMEOUT_RESUME_COUNT
    )
    decision = _no_tool_calls_decision(_no_tool_request(params, truncated, counters))

    assert decision.action == "continue"
    assert decision.counters == replace(
        counters, truncated_output_repairs=1
    ), "截断续跑按自己的预算计数，且不吃超时预算"
    assert params.tool_context == [_TRUNCATED_OUTPUT_RESUME]
    assert _PROVIDER_TIMEOUT_RESUME not in params.tool_context


def test_second_timeout_cycle_in_one_turn_resumes_only_once(monkeypatch, tmp_path) -> None:
    """真实循环：两次「超时 → 重试 → 只承诺」也只续跑一次，第二次承诺原样交付。"""
    second_promise = "第二次：我又只回了一句承诺。"
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, "TIMEOUT", second_promise, _FINAL],
        run_id="run-accept-twice",
    )

    assert run.backend.calls == 5
    # 续跑指令只会被裁决层**追加一次**；它会留在上下文里随后的出站请求（所以第 5 枪仍带它，
    # 但第 5 枪不是再一次续跑）。
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert all(prompt.count(_PROVIDER_TIMEOUT_RESUME) <= 1 for prompt in run.backend.prompts)
    assert _PROVIDER_TIMEOUT_RESUME in run.backend.prompts[3]
    assert _PROVIDER_TIMEOUT_RESUME not in run.backend.prompts[2], "重试那一枪不带续跑指令"
    assert run.response.text == second_promise, "预算用尽后按原语义交付原响应"
    assert run.response.runtime_status == "ok", "不得伪造 unfinished/cancelled 终态"
    assert run.tool_round_calls == [1], "续跑绝不进入工具执行路径"
    assert run.tool.handler_calls == 1
    assert run.backend.entries[4] == run.backend.entries[1], "第二次重试进入时账本无增量"


def test_empty_text_leaves_the_slot_to_the_existing_empty_text_nudge(tmp_path) -> None:
    """超时资格 + 空正文：让位给既有 empty-text nudge，两条预算不互相记账。"""
    params = _params(run_id="run-accept-empty")
    params.executed_tools.append("read_file")
    agent = _FakeAgent(_ScriptedBackend(["TIMEOUT", _PROMISE]), _RecordingPrompts(), tmp_path)
    _generate(agent, params)
    assert provider_timeout_resume_eligible(params) is True

    decision = _no_tool_calls_decision(
        _no_tool_request(params, ModelResponse(text="   ", backend="x"))
    )

    assert decision.action == "continue"
    assert params.tool_context == [_EMPTY_TEXT_NUDGE]
    assert _PROVIDER_TIMEOUT_RESUME not in params.tool_context
    assert decision.counters.empty_text_repairs == 1
    assert decision.counters.provider_timeout_resume_repairs == 0


def test_resume_reaches_the_production_decision_entry(tmp_path) -> None:
    """经真实入口 tool_loop_response_decision 复核资格链路（不是只有内部函数生效）。"""
    params = _params(run_id="run-accept-entry")
    params.executed_tools.append("read_file")  # 收窄(R1)：资格还需「本轮确实执行过工具」
    agent = _FakeAgent(_ScriptedBackend(["TIMEOUT", _PROMISE]), _RecordingPrompts(), tmp_path)
    response = _generate(agent, params)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=SimpleNamespace(config=SimpleNamespace(enable_tools=True, max_tool_rounds=0)),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )
    assert decision.action == "continue"
    assert decision.calls == [], "续跑不得派生工具调用"
    assert decision.counters.provider_timeout_resume_repairs == 1
    assert params.tool_context == [_PROVIDER_TIMEOUT_RESUME]


# ---------------------------------------------------------------- 3b. 行为刻画（设计取舍）


def test_no_tool_work_round_delivers_that_sample_verbatim(monkeypatch, tmp_path) -> None:
    """收窄(R1)后的行为：零工具执行 + 超时救回 + 完整终答 -> 不续跑，那一枪的正文原样交付。

    时序：第 1 枪超时 → 门槛5 重试那一枪给出完整答复「答案是 2。」→ 本轮从未执行过工具
    （纯问答轮，无未完成工作迹象）→ 资格判定第二条件不成立 → 不续跑 → 用户看到的就是
    「答案是 2。」。收窄前这里会多买一枪，把该正文顶掉（见 R1 验收发现）。
    """
    complete_answer = "答案是 2。"
    resumed_answer = "已完成，无需继续。"
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        ["TIMEOUT", complete_answer, resumed_answer],
        run_id="run-accept-narrowed-qa",
    )

    assert run.backend.calls == 2, "超时 + 重试；零工具执行的轮不再多买一枪"
    assert run.response.text == complete_answer, "重试那一枪的完整答复必须原样交付"
    assert resumed_answer not in run.params.tool_context
    assert run.params.tool_context == []
    assert run.params.executed_tools == [], "全程零工具执行 = 无未完成工作迹象"
    assert run.tool_round_calls == []
    assert provider_timeout_resume_eligible(run.params) is False
    assert all(_PROVIDER_TIMEOUT_RESUME not in prompt for prompt in run.backend.prompts)


# ---------------------------------------------------------------- 4. fail-closed 四闸门行为面


def _gate_request(params: ToolLoopExecuteParams, tmp_path: Path):
    backend = _ScriptedBackend([_PROMISE])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    request = ModelGenerateParams(
        agent=agent,
        params=params,
        prompt="把剩下的工作做完",
        tool_rounds=1,
    )
    return request, backend, agent


def test_gate_stream_stage_refuses_retry(tmp_path) -> None:
    """闸门1：first_event / stream_idle 归外层退避链，本处不重试、不登记事实。"""
    for stage in ("first_event", "stream_idle"):
        params = _params(run_id=f"run-gate-stage-{stage}")
        request, backend, _agent = _gate_request(params, tmp_path)
        sequence_before = params.live_archive_state.get("_model_turn_sequence")

        assert (
            _retry_once_after_timeout(request, ProviderTimeoutError("t", stage=stage)) is None
        ), stage
        assert backend.calls == 0, stage
        assert params.live_archive_state.get("_model_turn_sequence") == sequence_before
        assert "_provider_timeout_resume_turns" not in params.live_archive_state


def test_gate_ambiguous_active_turn_input_refuses_retry(tmp_path) -> None:
    """闸门2：已随调用发出、既未确认也未退回的补充输入，重发会二次投递 -> 拒绝。"""
    params = _params(run_id="run-gate-ambiguous-submission")
    params.live_archive_state.update({"_guidance_submission_id": "sub-1"})
    request, backend, _agent = _gate_request(params, tmp_path)

    assert _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock")) is None
    assert backend.calls == 0
    assert "_provider_timeout_resume_turns" not in params.live_archive_state


def test_gate_reserved_only_active_turn_input_allows_retry(tmp_path) -> None:
    """闸门2 的边界：只注入、没有在途提交的预留（或失败调用已退回的）不歧义，超时后照常重试一次。"""
    params = _params(run_id="run-gate-reserved-only")
    params.live_archive_state.update({"_guidance_ack_ids": {"sub-2"}})
    request, backend, _agent = _gate_request(params, tmp_path)

    retried = _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock"))
    assert retried is not None
    assert backend.calls == 1


def test_gate_unconfirmed_tool_use_refuses_retry(tmp_path) -> None:
    """闸门3：孤儿 tool_use（无任何配对回执）一律不重试。"""
    params = _params(run_id="run-gate-orphan")
    call = canonical_history_call(
        "read_file",
        _READ_ARGS,
        call_id="call-orphan",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    params.tool_ir_history[:] = [AssistantTurn(tool_calls=[call])]
    request, backend, _agent = _gate_request(params, tmp_path)

    assert _ir_last_tool_use_confirmed(request) is False
    assert _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock")) is None
    assert backend.calls == 0
    assert "_provider_timeout_resume_turns" not in params.live_archive_state


def test_gate_second_physical_attempt_refuses_a_third(tmp_path) -> None:
    """闸门4：同一 logical turn 已有两次物理尝试时拒绝第三次（无第三次超时补救）。"""
    params = _params(run_id="run-gate-attempts")
    backend = _ScriptedBackend(["TIMEOUT", _PROMISE])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    request = ModelGenerateParams(
        agent=agent,
        params=params,
        prompt="把剩下的工作做完",
        tool_rounds=1,
    )

    # 真实路径：第 1 次物理尝试墙钟超时 -> 门槛5 重试（第 2 次物理尝试）成功并登记事实。
    response = generate_model_response(request)
    assert backend.calls == 2
    assert response.text == _PROMISE
    records = agent._model_call_ledger.records()
    assert [item.status for item in records] == ["timed_out", "finished"]
    assert records[0].metadata["logical_call_id"] == records[1].metadata["logical_call_id"]
    assert _logical_physical_attempt_count(request) == 2
    assert len(_fact_entries(params)) == 1
    sequence_after_retry = params.live_archive_state.get("_model_turn_sequence")

    # 同一 logical turn 的第三次尝试：闸门4 拒绝，且不产生任何物理调用/新事实格子。
    assert (
        _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock")) is None
    )
    assert backend.calls == 2, "拒绝第三次尝试"
    assert params.live_archive_state.get("_model_turn_sequence") == sequence_after_retry
    assert len(_fact_entries(params)) == 1
    assert response.text == _PROMISE


# LLM: Loopback only; each route holds either the first SSE event, streams valid deltas, or slowly supplies JSON bytes.
# 类用途: 为主模型与 Compact auxiliary 的超时层提供不触网的真实 HTTP 慢响应证据。
class _CompactTimeoutHandler(BaseHTTPRequestHandler):
    def _write(self, value: bytes) -> bool:
        try:
            self.wfile.write(value)
            self.wfile.flush()
        except OSError:
            return False
        return True

    # LLM: Consume the request body before replying; routing uses only the local URL path, not prompt content.
    # 函数用途: 将请求分流给 SSE 或 JSON 慢响应，不在入口里叠加计时分支。
    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(size)
        mode = self.path.split("/")[1]
        if json.loads(body).get("stream"):
            self._serve_sse(mode)
            return
        self._serve_slow_json()

    # LLM: Keep stream setup separate from scenario timing so every test uses the same HTTP/SSE framing.
    # 函数用途: 发送统一 SSE 响应头，再按场景发首事件或慢滴流。
    def _serve_sse(self, mode: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        finished = {
            "late": self._serve_late_first_event,
            "endless": self._serve_endless_drip,
        }.get(mode, self._serve_data_drip)()
        if finished:
            self._finish_sse()

    # LLM: Comments keep the socket active but must not count as semantic provider progress.
    # 函数用途: 延迟首个有效 data 行，期间只吐注释保活。
    def _serve_late_first_event(self) -> bool:
        for _ in range(4):
            if not self._write(b": keepalive\n\n"):
                return False
            time.sleep(0.4)
        return self._write(_CAB_SSE_CHUNK)

    # LLM: Every valid data row arrives within the idle window, isolating the absence of a total deadline.
    # 函数用途: 每 0.45 秒推送有效 data，直到服务端主动结束。
    def _serve_data_drip(self) -> bool:
        if not self._write(_CAB_SSE_CHUNK):
            return False
        time.sleep(0.45)
        for _ in range(5):
            if not self._write(_CAB_SSE_CHUNK):
                return False
            time.sleep(0.45)
        return True

    # LLM: cabfix: this route keeps sending valid events far past any reasonable test deadline, so the
    # absence of a total limit would hang the suite. It stops early only when the client closes the socket.
    # 函数用途: 无限推送有效 data（每 0.05 秒），直到客户端断开或超过安全上限。
    def _serve_endless_drip(self) -> bool:
        for _ in range(400):
            if not self._write(_CAB_SSE_CHUNK):
                return False
            time.sleep(0.05)
        return True

    # LLM: Separate terminators make the bounded healthy-stream fixture deterministic.
    # 函数用途: 写入 stop 与 [DONE] 终止事件。
    def _finish_sse(self) -> None:
        if self._write(_CAB_SSE_DONE):
            self._write(b"data: [DONE]\n\n")

    # LLM: Every body fragment arrives inside the socket read timeout while whole-response time exceeds it.
    # 函数用途: 分片写完合法 JSON，验证非流式请求是否有独立总期限。
    def _serve_slow_json(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_CAB_JSON_RESPONSE)))
        self.end_headers()
        for offset in range(0, len(_CAB_JSON_RESPONSE), 8):
            if not self._write(_CAB_JSON_RESPONSE[offset:offset + 8]):
                return
            time.sleep(0.4)

    def log_message(self, *_args: object) -> None:
        return


_CAB_SSE_CHUNK = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
_CAB_SSE_DONE = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
_CAB_JSON_RESPONSE = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'


# LLM: Bind an ephemeral loopback-only server and always release it after the test.
# 函数用途: 提供真实 socket 但绝不连接外网、真实 Gateway 或模型的慢响应服务。
@pytest.fixture

def _compact_timeout_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CompactTimeoutHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


# LLM: Use a test-only key string and per-scenario path; no environment or real provider configuration is read.
# 函数用途: 构造连接 loopback 假服务的真实 OpenAI-compatible 后端。
def _cab_backend(server, route: str, *, stream: bool) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(BackendOptions(
        api_base=f"http://127.0.0.1:{server.server_address[1]}/{route}",
        api_key="test-only-not-a-credential",
        model_name="cab-loopback",
        request_timeout=1,
        connect_timeout=1,
        max_tokens=8,
        stream_enabled=stream,
    ))


# LLM: Reuse the normal model request, ledger and backend path; only the loopback endpoint is fake.
# 函数用途: 通过主模型生成入口执行一次本地服务请求并返回实测耗时。
def _cab_main_call(agent: _FakeAgent, run_id: str, prompt: str):
    params = replace(_params(run_id=run_id), user_prompt=prompt)
    request = ModelGenerateParams(agent=agent, params=params, prompt=prompt, tool_rounds=1)
    started = time.monotonic()
    response = generate_model_response(request)
    return response, time.monotonic() - started


# LLM: Compact uses its real auxiliary ledger and backend dispatch; only the destination is loopback.
# 函数用途: 通过 auxiliary_model_call 执行一次本地 Compact 样式请求并返回实测耗时。
def _cab_auxiliary_call(backend, prompt: str, *, agent=None):
    agent = agent or _cab_auxiliary_agent(backend)
    request = AuxiliaryModelCallRequest(agent=agent, prompt=prompt, purpose="conversation_compact_summary")
    started = time.monotonic()
    response = generate_auxiliary_model_response(request)
    return response, time.monotonic() - started


# LLM: Auxiliary calls now share the main path's dynamic-budget config surface; tests must supply the same
# two timeout bounds so the input estimate is actually reachable. request_timeout stays 1s to keep the
# distinction visible: base read timeout vs. dynamic first-event budget vs. absolute deadline.
# 函数用途: 构造带动态超时上下限配置的辅助调用假 Agent，供首事件预算与总期限用例复用。
def _cab_auxiliary_agent(backend, *, dynamic_min: int = 3, dynamic_max: int = 4):
    return SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(
            model_name="cab-loopback", request_timeout=1, max_tokens=8, my_agent_owner_id="",
            dynamic_timeout_min=dynamic_min, dynamic_timeout_max=dynamic_max,
            estimated_output_tokens_per_second=100_000,
        ),
    )


# LLM: cabfix2 的联动用例要调真实的 request_timeout 与 dynamic_timeout_max（默认 240 / 10800），
#   用来证明"配置放开后辅助调用的流式硬期限也同步放宽"；与上面的缩放版本分开，避免改坏既有断言。
# 函数用途: 构造按生产默认档配置的辅助调用假 Agent（可覆盖 request_timeout / dynamic_max）。
def _cab_auxiliary_agent_dynamic(*, request_timeout: int = 240, dynamic_max: int = 10800):
    return SimpleNamespace(
        backend=None,
        config=SimpleNamespace(
            model_name="cab-loopback", request_timeout=request_timeout, max_tokens=8, my_agent_owner_id="",
            dynamic_timeout_min=30, dynamic_timeout_max=dynamic_max,
            estimated_output_tokens_per_second=20.0,
        ),
    )


def test_main_stream_input_estimate_allows_late_first_event(_compact_timeout_server, tmp_path):
    """主模型输入估算可把首事件窗口扩到 3s，超过基础 1s 后仍能收到首事件。"""
    backend = _cab_backend(_compact_timeout_server, "late", stream=True)
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    agent.config.request_timeout = 1
    agent.config.dynamic_timeout_min = 3
    agent.config.dynamic_timeout_max = 4
    agent.config.estimated_output_tokens_per_second = 100_000
    response, elapsed = _cab_main_call(agent, "cab-main-late", "长历史片段。" * 800)
    assert response is not None
    assert elapsed > 1.3, f"main_first_event_elapsed={elapsed:.3f}s"
    print(f"CAB main stream late-first wait={elapsed:.3f}s outcome=completed; dynamic budget >=3s")


def test_compact_stream_uses_input_estimate_for_late_first_event(_compact_timeout_server):
    """cabfix 修法①：辅助流现在也按输入估算首事件预算，晚首事件不再被基础 1s 过早判超时。"""
    backend = _cab_backend(_compact_timeout_server, "late", stream=True)
    agent = _cab_auxiliary_agent(backend, dynamic_min=3, dynamic_max=4)
    started = time.monotonic()
    response, _ = _cab_auxiliary_call(
        backend, "长历史片段。" * 800, agent=agent,
    )
    elapsed = time.monotonic() - started
    assert response is not None
    assert elapsed > 1.3, f"compact_first_event_elapsed={elapsed:.3f}s"
    print(f"CAB compact stream late-first wait={elapsed:.3f}s outcome=completed; dynamic budget >=3s")


@pytest.mark.parametrize("entrypoint", ["main", "compact"])
def test_valid_sse_drips_keep_each_stream_path_alive(_compact_timeout_server, tmp_path, entrypoint):
    """每 0.45s 有有效 data 的 1s idle 流可越过多个窗口，直到服务端结束。"""
    backend = _cab_backend(_compact_timeout_server, "drip", stream=True)
    started = time.monotonic()
    if entrypoint == "main":
        agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
        agent.config.request_timeout = 1
        agent.config.dynamic_timeout_min = 1
        agent.config.dynamic_timeout_max = 1
        agent.config.estimated_output_tokens_per_second = 100_000
        response, elapsed = _cab_main_call(agent, "cab-main-drip", "短提示")
    else:
        response, elapsed = _cab_auxiliary_call(backend, "短历史")
    assert response is not None
    assert elapsed > 2.0
    print(f"CAB {entrypoint} stream valid-drip wait={elapsed:.3f}s outcome=completed >2 idle windows")


def test_main_nonstream_slow_body_is_cut_by_outer_total_guard(_compact_timeout_server, tmp_path, monkeypatch):
    """主模型非流式慢滴流由外层 1s wall_clock 总期限收口，而不是等 JSON EOF。"""
    from agent_py_agent.agent.agent_core import tool_model_generation as generation

    monkeypatch.setattr(generation, "_retry_once_after_timeout", lambda *_args: None)
    backend = _cab_backend(_compact_timeout_server, "drip", stream=False)
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    agent.config.request_timeout = 1
    agent.config.dynamic_timeout_min = 1
    agent.config.dynamic_timeout_max = 1
    agent.config.estimated_output_tokens_per_second = 100_000
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as error:
        _cab_main_call(agent, "cab-main-json-drip", "短提示")
    elapsed = time.monotonic() - started
    assert error.value.stage == "wall_clock"
    assert 0.7 <= elapsed < 2.5, f"main_nonstream_elapsed={elapsed:.3f}s"
    print(f"CAB main nonstream slow-body wait={elapsed:.3f}s outcome=wall_clock timeout")


def test_compact_nonstream_slow_body_is_cut_by_absolute_deadline(_compact_timeout_server, monkeypatch):
    """cabfix 修法②：辅助非流式调用有绝对期限，慢滴流在期限处结构化超时（wall_clock）。"""
    from agent_py_agent.agent.conversation import auxiliary_model_call as cab

    # 把兜底窗口压到 1.5 秒，让"到期"在测试时间内可观测；判据是"有没有绝对期限"，不是具体数值。
    monkeypatch.setattr(cab, "AUXILIARY_CALL_FLOOR_SECONDS", 1.5)
    backend = _cab_backend(_compact_timeout_server, "drip", stream=False)
    agent = _cab_auxiliary_agent(backend)
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as error:
        _cab_auxiliary_call(backend, "短历史", agent=agent)
    elapsed = time.monotonic() - started
    assert error.value.stage == "wall_clock", f"stage={error.value.stage}"
    assert 1.0 <= elapsed < 3.0, f"compact_nonstream_elapsed={elapsed:.3f}s"
    print(f"CAB compact nonstream slow-body wait={elapsed:.3f}s outcome=wall_clock absolute deadline")


def test_compact_nonstream_timeout_is_recorded_as_timed_out(_compact_timeout_server, monkeypatch):
    """cabfix 修法②的收口要求：超时必须以 timed_out 落在模型调用账上，而不是只抛异常。"""
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.conversation import auxiliary_model_call as cab

    monkeypatch.setattr(cab, "AUXILIARY_CALL_FLOOR_SECONDS", 1.5)
    backend = _cab_backend(_compact_timeout_server, "drip", stream=False)
    agent = _cab_auxiliary_agent(backend)
    with pytest.raises(ProviderTimeoutError):
        _cab_auxiliary_call(backend, "短历史", agent=agent)
    records = model_call_ledger(agent).records()
    timeout_records = [item for item in records if item.status == "timed_out"]
    assert timeout_records, f"应有一条 timed_out 记录，实际状态: {[item.status for item in records]}"
    assert timeout_records[-1].timeout_stage == "wall_clock"


def test_compact_stream_endless_valid_events_stop_at_total_deadline(_compact_timeout_server, monkeypatch):
    """cabfix 修法③：一直有有效事件的辅助流不再无限续期，在总时限处停止。"""
    from agent_py_agent.agent.conversation import auxiliary_model_call as cab

    # 流式总时限压到 2 秒：服务端一直发有效事件，旧行为会一直续期直到服务端自己停。
    monkeypatch.setattr(cab, "AUXILIARY_STREAM_TOTAL_TIMEOUT_SECONDS", 2.0)
    backend = _cab_backend(_compact_timeout_server, "endless", stream=True)
    agent = _cab_auxiliary_agent(backend)
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as error:
        _cab_auxiliary_call(backend, "短历史", agent=agent)
    elapsed = time.monotonic() - started
    assert error.value.stage in {"wall_clock", "stream_idle"}, f"stage={error.value.stage}"
    assert 1.5 <= elapsed < 6.0, f"compact_stream_endless_elapsed={elapsed:.3f}s"
    print(f"CAB compact stream endless-drip wait={elapsed:.3f}s outcome={error.value.stage} at total deadline")


# --- cabfix2：硬期限必须在首次排程就算进去、且不得早于首包预算（g2/cab 返工） --------


# LLM: cabfix2 覆盖的是初审查出的真实缺陷：watchdog.start() 原先只按 idle_deadline 排首次
#   定时器，而 touch() 不重排定时器——idle（首包预算）远大于 hard（流式总时限）时，硬顶在
#   首包阶段形同虚设。这里把几何缩放后直接盯住 watchdog 本身，不依赖网络时序。
# 类用途: 记录 watchdog 到点时是否真的调用了 abort（不需要真实响应对象）。
class _RecordingGuard:
    # 函数用途: 建一个只记录 abort 是否发生的假 guard。
    def __init__(self) -> None:
        self.aborted = threading.Event()

    # 函数用途: 记录一次中止请求。
    def abort(self) -> None:
        self.aborted.set()


# 函数用途: 按给定 idle/hard 建一个 watchdog，返回（watchdog, guard, 起始时刻）。
def _cabfix2_watchdog(idle_seconds: float, hard_seconds: float | None):
    from agent_py_agent.agent.backends.gateway_helpers import _StreamIdleWatchdog

    guard = _RecordingGuard()
    started = time.monotonic()
    watchdog = _StreamIdleWatchdog(
        guard,
        300.0,  # 基础空闲窗口给大些，让 idle 与 hard 的先后完全由入参决定
        idle_deadline=started + idle_seconds,
        hard_deadline=None if hard_seconds is None else started + hard_seconds,
    )
    return watchdog, guard, started


# 函数用途: 首包预算(缩放为 2s) 大于总时限(缩放为 0.6s) 时，硬期限仍在首次排程生效。
def test_watchdog_hard_deadline_earlier_than_idle_fires_at_start():
    watchdog, guard, _ = _cabfix2_watchdog(idle_seconds=2.0, hard_seconds=0.6)
    watchdog.start()
    try:
        fired = guard.aborted.wait(timeout=1.6)
        assert fired, "hard 早于 idle 时，首次排程就必须覆盖 hard（旧缺陷：只按 idle 排）"
        assert watchdog.timed_out, "到点必须记 timed_out（不是 failed）"
        assert watchdog.timeout_stage in {"first_event", "stream_idle"}
    finally:
        watchdog.cancel()


# 函数用途: 硬期限晚于 idle 时，仍按 idle 到点（不因为加了 min 而改变原语义）。
def test_watchdog_idle_earlier_than_hard_fires_at_idle():
    watchdog, guard, _ = _cabfix2_watchdog(idle_seconds=0.5, hard_seconds=3.0)
    watchdog.start()
    try:
        assert guard.aborted.wait(timeout=1.5), "idle 更早时应在 idle 处到点"
        assert watchdog.timed_out
    finally:
        watchdog.cancel()


# 函数用途: 没有硬期限（主模型/旧调用方）时行为原样——只按 idle 排，不会被 hard 影响。
def test_watchdog_without_hard_deadline_keeps_idle_only_semantics():
    watchdog, guard, _ = _cabfix2_watchdog(idle_seconds=0.5, hard_seconds=None)
    watchdog.start()
    try:
        assert guard.aborted.wait(timeout=1.5)
        assert watchdog.timed_out and watchdog.timeout_stage == "first_event"
    finally:
        watchdog.cancel()


# 函数用途: 流式总时限与配置联动——输入量足够大时，硬期限 ≥ 首包预算 + request_timeout。
def test_auxiliary_stream_total_deadline_covers_first_event_budget():
    from agent_py_agent.agent.conversation import auxiliary_model_call as cab

    agent = _cab_auxiliary_agent_dynamic(request_timeout=240, dynamic_max=10800)
    # 输入量要真的到 20 万 token 档，预算才会超过默认 1800 秒总时限（估算按字符粗算，留足余量）。
    request = SimpleNamespace(agent=agent, prompt="历史片段。" * 60_000, messages=[], tools=[],
                              system_instruction="", response_schema=None)
    input_tokens = cab._auxiliary_input_tokens(request)
    first_event = cab._auxiliary_first_event_budget(request, input_tokens)
    assert input_tokens >= 200_000, f"前提：本用例输入量应到 20 万 token 档，实际 {input_tokens}"
    assert first_event is not None and first_event > cab.AUXILIARY_STREAM_TOTAL_TIMEOUT_SECONDS, (
        f"前提：该输入的首包预算应超过默认流式总时限，实际 {first_event}"
    )
    total = cab._auxiliary_absolute_timeout(request, streaming=True, first_event=first_event)
    assert total >= first_event + 240, (
        f"流式硬期限必须≥首包预算+request_timeout，实际 total={total} first_event={first_event}"
    )
    # 非流式口径不变：max(request_timeout, 240)。
    assert cab._auxiliary_absolute_timeout(request, streaming=False, first_event=first_event) == 240.0


# 函数用途: 一次辅助调用只估算一次输入——记账和时限计划共用同一个数（流式 + 动态超时是原先估三遍的形态）。
def test_auxiliary_call_estimates_input_once_for_ledger_and_timeouts(monkeypatch):
    from agent_py_agent.agent.conversation import auxiliary_model_call as cab
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )

    materials = []
    monkeypatch.setattr(cab, "estimate_tokens", lambda value: materials.append(value) or 200_000)
    seen = []
    response = ModelResponse("摘要", "fake")

    def generate(prompt, *, on_chunk=None, request_options=None):
        seen.append(request_options)
        return response

    agent = _cab_auxiliary_agent_dynamic(request_timeout=240, dynamic_max=10800)
    agent.backend = SimpleNamespace(name="fake", stream_enabled=True, generate=generate)
    assert generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent, "压缩这段历史")) is response
    dict_materials = [m for m in materials if isinstance(m, dict)]
    assert len(dict_materials) == 1, f"一次辅助调用只应估算一次输入，实际 {len(dict_materials)} 次"
    record, = agent._model_call_ledger.records()
    assert record.input_tokens == 200_000
    first_event = cab._auxiliary_first_event_budget(SimpleNamespace(agent=agent), 200_000)
    assert first_event is not None
    options, = seen
    assert options.first_event_timeout_seconds == first_event
    assert options.total_deadline_seconds == cab._auxiliary_absolute_timeout(
        SimpleNamespace(agent=agent), streaming=True, first_event=first_event,
    )


# 函数用途: 主模型形态（不设 total_deadline）仍不产生硬期限，deadline 为 None。
def test_main_path_request_has_no_total_deadline():
    from agent_py_agent.agent.backends.http import HttpBackend, StreamCall, StreamCallOptions

    backend = object.__new__(HttpBackend)
    backend.api_base = "http://127.0.0.1:9"
    backend.api_key = "k"
    backend.custom_headers = {}
    backend.session_header = ""
    backend.request_timeout = 5
    backend.connect_timeout = 1
    plain = backend._gateway_request(StreamCall(path="/v1/chat", payload={}, headers={}))
    assert plain.deadline is None, "主模型路径不应设硬期限"
    bounded = backend._gateway_request(
        StreamCall(path="/v1/chat", payload={}, headers={}),
        options=StreamCallOptions(total_deadline_seconds=1800.0),
    )
    assert bounded.deadline is not None, "显式给了总期限才设"


# --- cabfix2 护栏：生产流式链上必须有人接受 options（第 1 档），否则静默丢掉硬期限 --------


# LLM: request_stream_lines 按被调方签名分三档：① 接受 options（首包预算+总期限一起送达）
#   ② 只接受 total_deadline_seconds ③ 只接受 first_event_timeout_seconds。第 ③ 档会**静默丢掉**
#   硬期限。生产链上真正会被传进去的目标是**各后端类自己的流式入口**（HttpBackend 子类覆写或
#   继承的 request_stream / request_stream_iter）——gateway_helpers 的 post_stream 系收发的是
#   已经构造好的 GatewayRequest（选项在那之前就由 _gateway_request 消化成 deadline 了），
#   不是 request_stream_lines 的被调方。
# 函数用途: 从一个后端类里取出它的两个流式入口（不存在返回空列表）。
def _backend_stream_entries(value, http_module):
    if not isinstance(value, type) or not issubclass(value, http_module.HttpBackend):
        return []
    entries = []
    for method in ("request_stream", "request_stream_iter"):
        entry = getattr(value, method, None)
        if entry is not None:
            entries.append(entry)
    return entries


# LLM: 模块级遍历拆成独立生成器，让两个函数的嵌套深度都落在 code-size 软限内；
#   去重语义保持"同一函数只留首次出现的标签"（跨模块重复时标签来自更早的模块）。
# 函数用途: 遍历一个后端模块里所有后端类的流式入口，产出（来源标签, 函数对象）。
def _module_stream_entries(module, http_module):
    for name, value in vars(module).items():
        for entry in _backend_stream_entries(value, http_module):
            yield f"{module.__name__}.{name}.{entry.__name__}", entry


# 函数用途: 收集生产后端所有会作为流式入口的函数对象及其来源标签。
def _production_stream_targets():
    from agent_py_agent.agent.backends import anthropic, http, openai_chat

    by_id: dict[int, tuple[str, object]] = {}
    for module in (http, openai_chat, anthropic):
        for label, entry in _module_stream_entries(module, http):
            by_id.setdefault(id(entry), (label, entry))
    return list(by_id.values())


# 函数用途: 断言生产后端的流式入口都接受 options（第 1 档），没有只认 first_event 的静默降级点。
def test_production_stream_targets_accept_options_so_hard_deadline_survives():
    from agent_py_agent.agent.backends.http import _accepts_kwarg

    targets = _production_stream_targets()
    assert targets, "应至少找到一个生产后端流式入口；找不到说明探测方式失效"
    missing = [label for label, entry in targets if not _accepts_kwarg(entry, "options")]
    assert not missing, (
        "这些生产后端流式入口不接受 options，硬期限会静默丢掉（request_stream_lines 退到第 ③ 档）："
        + ", ".join(missing)
    )


# 函数用途: 反向锁定传输层的约定——post_stream 系收的是已构造好的 GatewayRequest，其 deadline 字段承载硬期限。
def test_gateway_request_envelope_carries_deadline_for_transport():
    import inspect

    from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest

    assert "deadline" in GatewayRequest.__dataclass_fields__, "硬期限必须在信封上有字段"
    for entry_name in ("post_stream", "post_stream_iter"):
        from agent_py_agent.agent.backends import gateway_helpers

        entry = getattr(gateway_helpers, entry_name)
        params = list(inspect.signature(entry).parameters)
        assert params and params[0] == "request", (
            f"{entry_name} 的第一参数应是已构造好的 GatewayRequest，实际 {params}"
        )


# LLM: 三个分派档位的替身统一用这个小数据类承载捕获结果，替身函数保持不超过 4 个参数
#   （与产品的参数门禁同一口径，避免测试自己长出一个高参数告警）。
# 类用途: 记录流式入口替身实际收到了哪些关键字与位置参数。
@dataclass
class _StreamTargetCapture:
    first_event: object = None
    total: object = None
    options: object = None
    total_first_event: object = None
    total_positional: tuple = ()


# 函数用途: 造三个只收部分关键字、但参数个数不超过 4 的分派替身。
def _make_stream_targets(capture: _StreamTargetCapture) -> dict[str, object]:
    def options_target(path, payload, headers, *, options=None):
        capture.options = options
        return ["ok"]

    # LLM: 第 2 档替身必须同时接住两个超时关键字；不能收 **kwargs——_accepts_kwarg 把
    #   VAR_KEYWORD 也算作"接受 options"，收 **kwargs 会让分派器误走第 1 档。三个位置参数
    #   收进 *args 把参数个数从 5 降到 3（过 code-size 门禁），内容由断言核对。
    # 函数用途: 第 2 档替身——接住两个超时关键字并记录位置参数。
    def total_target(*args, first_event_timeout_seconds=None, total_deadline_seconds=None):
        capture.total = total_deadline_seconds
        capture.total_first_event = first_event_timeout_seconds
        capture.total_positional = args
        return []

    def first_event_target(path, payload, headers, *, first_event_timeout_seconds=None):
        capture.first_event = first_event_timeout_seconds
        return []

    return {"options": options_target, "total": total_target, "first_event": first_event_target}


# 函数用途: 反向锁定分派优先级——目标接受 options 时必须走第 1 档，两个超时字段一起送达。
def test_request_stream_lines_prefers_options_tier():
    from agent_py_agent.agent.backends import http

    capture = _StreamTargetCapture()
    result = http.request_stream_lines(_make_stream_targets(capture)["options"], ("p", {}, {}, 3.0, 1800.0))
    assert result == ["ok"]
    assert capture.options is not None, "接受 options 的目标必须走第 1 档"
    assert getattr(capture.options, "total_deadline_seconds", None) == 1800.0
    assert getattr(capture.options, "first_event_timeout_seconds", None) == 3.0


# 函数用途: 只接受 total_deadline_seconds 的第 2 档仍能拿到硬期限（不静默丢）。
def test_request_stream_lines_second_tier_keeps_total_deadline():
    from agent_py_agent.agent.backends import http

    capture = _StreamTargetCapture()
    http.request_stream_lines(_make_stream_targets(capture)["total"], ("p", {}, {}, 3.0, 1800.0))
    assert capture.total == 1800.0, "第 2 档必须把总期限传下去"
    assert capture.total_first_event == 3.0, "第 2 档同时送达首包预算"
    assert capture.total_positional == ("p", {}, {}), "第 2 档仍按位置把请求材料传给被调方"


# 函数用途: 第 3 档（只认 first_event）确实会丢硬期限——把这条静默降级事实钉住，防止有人误以为它也安全。
def test_request_stream_lines_third_tier_loses_total_deadline_by_design():
    from agent_py_agent.agent.backends import http

    capture = _StreamTargetCapture()
    http.request_stream_lines(_make_stream_targets(capture)["first_event"], ("p", {}, {}, 3.0, 1800.0))
    assert capture.first_event == 3.0
    assert capture.total is None and capture.options is None, "第 3 档按设计拿不到硬期限；生产后端不得停留在这一档"
