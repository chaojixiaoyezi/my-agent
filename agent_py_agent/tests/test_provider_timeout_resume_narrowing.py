"""LLM: R1 收窄单测——门槛5「超时被重试救回 → 轮内续跑一次」要求**确有未完成工作**。

背景（独立验收 R1）：``ebb90d0c`` 的触发条件是纯结构化的两件事 ①本 model turn 发生过
被重试救回的供应商超时、②本枪零工具调用。它不读正文（这是对的），但缺一条「本轮确实
做过工作」的结构化条件，于是**纯问答轮**也会被续跑：重试那一枪已经给出完整终答
（例：「答案是 2。」），宿主仍会再问一次，而最终交付文本取自续跑那一枪——用户再也看不到
那句完整终答（可感的信息丢失）。

收窄（仍是零文本匹配）：``provider_timeout_resume_eligible`` 现在 = ①turn 级超时事实
AND ②本 run 工具账本 ``executed_tools`` 非空（本 run 已真实执行过工具）。两条都是结构化
字段，与模型正文、工具名、文案无关；门槛5 的四个 fail-closed 重试闸门一行未动。

本文件钉住三件事（对应任务 (a)(b)(c)）：
(a) 无工具执行 + 超时救回 + 完整终答 -> 不续跑：响应对象同一、最终文本就是那一枪的文本、
    账本无新采样；
(b) 有工具执行 + 超时救回 + 零工具调用承诺 -> 仍恰好续跑 1 次（真实
    ``_execute_tool_loop_service`` 端到端，工具副作用不重放）；
(c) 有工具执行 + 超时救回 + 正常完成（有工具调用 / 显式终态）-> 不续跑。

受控边界：全部用例使用 fake 后端与本地 IR/工具夹具，不含真实供应商超时；真机验收不在
本文件内执行。既有行为面仍由 ``test_provider_timeout_continuation.py``（13 例）与
``test_provider_timeout_acceptance.py``（15 例）覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _PROVIDER_TIMEOUT_RESUME,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _turn_has_executed_tools,
    generate_model_response,
    provider_timeout_resume_eligible,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)

# 供应商超时预算必须极小，才能让「挂起」的 fake 后端被墙钟保护拿下（与产品默认值无关）。
_TINY_REQUEST_TIMEOUT = 0.02
# 重试那一枪给出的完整终答 —— R1 里被续跑顶掉、用户再也看不到的那句话。
_COMPLETE_ANSWER = "答案是 2。"
# 续跑那一枪的正文：收窄后不可能再出现在交付路径上（纯问答轮）。
_RESUMED_ANSWER = "已完成，无需继续。"
# 干活干到一半超时的形状：模型只回一句承诺。
_PROMISE = "在的，刚才超时了，我重新来。"
_FINAL = "已从断点继续并完成剩余工作。"
_READ_ARGS = {"path": "notes.md"}
_SECOND_READ_ARGS = {"path": "other.md"}

_READ_SPEC = make_test_model_spec(
    "read_file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------- 夹具


# LLM: 假 agent 只需生成层/工具轮真正读取的字段：backend、config.request_timeout、
#   config.enable_tools、config.max_tool_rounds、prompts（_render_tool_loop_prompt 读取）、
#   root（产品记账写 blob/archive 时的 owner 根）、_current_subagent_run_id。
# 函数用途: 构造真实生成路径与真实工具轮循环所需的最小宿主外壳。
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


# LLM: 每个用例一个新的 params 实例（live_archive_state 是本 run 的结构化事实容器）。
#   ``executed_tools`` 由调用方显式决定：收窄后它就是「本轮是否确有未完成工作」这条判据的
#   唯一权威字段，测试必须显式写出它，不允许靠共享夹具默默带过。
# 函数用途: 构造一次工具模型轮所需的运行参数。
def _params(*, run_id: str, executed_tools: list[str] | None = None) -> ToolLoopExecuteParams:
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
        executed_tools=list(executed_tools or []),
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


# LLM: 只记录真正发给模型的出站文本；测试不复制宿主 prompt 模板，只断言宿主指令出现在
#   哪几枪请求里。
# 类用途: 记录每次 prompt 组装结果，供「续跑指令恰好出现一次」这类断言。
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


# LLM: 脚本后端按序号回放「工具调用 / 挂起超时 / 承诺 / 完整终答」；每次进入一次物理采样
#   前先拍一次工具账本指纹，用来证明「续跑那一枪进入时账本零增量」。
# 类用途: 模拟可按剧本复现供应商时序的假后端。
class _ScriptedBackend:
    __test__ = False

    name = "scripted-narrowing-backend"

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
        if isinstance(step, tuple) and step[0] == "tool":
            _, name, arguments = step
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[
                    {"id": f"call-{self.calls}", "name": name, "input": dict(arguments)}
                ],
            )
        return ModelResponse(text=str(step), backend=self.name)


# LLM: 计数 read_file handler：handler 执行次数是「工具副作用是否被重放」的最终判据，
#   它不依赖被测产品的记账代码，避免自证。
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
#   做序列化/递归枚举。
# 函数用途: 拍一份「工具是否被再次执行」的最小结构化指纹。
def _ledger_fingerprint(params: object) -> dict[str, object]:
    history = list(getattr(params, "tool_ir_history", None) or [])
    results = [item.call_id for item in history if isinstance(item, ToolResult)]
    return {
        "executed_tools": list(getattr(params, "executed_tools", None) or []),
        "tool_result_ids": results,
    }


# LLM: 一次真实工具轮循环的全部可观测事实；测试只读这些结构字段。
# 类用途: 承载真实 _execute_tool_loop_service 跑完后的账本/时序证据。
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
# 函数用途: 用剧本后端跑一次真实工具轮循环，返回结构化证据。
def _drive_loop(monkeypatch, tmp_path: Path, script: list[object], *, run_id: str) -> _LoopRun:
    from agent_py_agent.agent.agent_core import _tool_loop_service as tls
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        ToolLoopService,
        _execute_tool_loop_service,
    )

    params = _params(run_id=run_id)
    tool = _CountingReadTool()
    backend = _ScriptedBackend(script, fingerprint=lambda: _ledger_fingerprint(params))
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    service = ToolLoopService(agent)

    counts: dict[str, object] = {"rounds": [], "exec_one": 0}
    real_run_tool_round = tls._run_tool_round

    def counting_tool_round(request):
        counts["rounds"].append(request.tool_rounds)
        return real_run_tool_round(agent, request)

    def counting_execute_one(request):
        counts["exec_one"] = int(counts["exec_one"]) + 1
        return execute_canonical_test_call(
            tmp_path,
            tools={tool.model_spec.name: tool},
            tool_name=request.call.tool_name,
            arguments=dict(request.call.arguments),
            call_id=request.call.call_id,
            run_id=run_id,
        )

    # 实例属性赋值不会绑定 self：循环以 service._run_tool_round(request) 单参形式调用。
    monkeypatch.setattr(service, "_run_tool_round", counting_tool_round)
    monkeypatch.setattr(service, "_execute_one_tool_call", counting_execute_one)
    # 真实 prompt 组装需要完整 agent 装配；这里只替换组装入口，保留产品渲染函数本身。
    monkeypatch.setattr(
        tls,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: tls._render_tool_loop_prompt(agent, loop_params),
    )

    _prompt, response, rounds = _execute_tool_loop_service(service, params)
    return _LoopRun(
        backend=backend,
        tool=tool,
        params=params,
        response=response,
        rounds=rounds,
        tool_round_calls=list(counts["rounds"]),
        executed_one_calls=int(counts["exec_one"]),
        agent=agent,
    )


# LLM: 生成层的真实超时路径：第一枪挂起触发门槛5，之后按 fake 后端剧本应答。
# 函数用途: 通过真实 generate_model_response 建立结构化超时事实。
def _generate(agent: _FakeAgent, params: ToolLoopExecuteParams):
    return generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="把剩下的工作做完",
            tool_rounds=1,
        )
    )


# LLM: 裁决层输入必须复用生成层登记事实的同一个 params（同源 model turn 序号），不另建
#   序号来源，也不手写 live_archive_state 里的事实。
# 函数用途: 构造一次「零工具调用」裁决请求。
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


# LLM: 只读生成层登记的超时事实表本身（不判断、不改写），用来断言「事实照记、资格另判」。
# 函数用途: 取当前 params 上的超时事实表（无则空表）。
def _fact_entries(params: ToolLoopExecuteParams) -> dict:
    return params.live_archive_state.get("_provider_timeout_resume_turns") or {}


# ---------------------------------------------------------------- (a) 纯问答轮不续跑


def test_no_tool_work_timeout_rescue_keeps_that_answer_and_adds_no_sample(tmp_path) -> None:
    """(a) 无工具执行 + 超时救回 + 完整终答 -> break、响应同一、零新采样、零续跑计数。"""
    params = _params(run_id="run-narrow-qa")
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER, _RESUMED_ANSWER])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    response = _generate(agent, params)

    assert backend.calls == 2, "门槛5 行为不变：超时后恰好重试一次"
    assert response.text == _COMPLETE_ANSWER
    assert _turn_has_executed_tools(params) is False, "前提：本轮零工具执行（纯问答轮）"
    assert provider_timeout_resume_eligible(params) is False, "收窄：无工作迹象不得续跑"
    assert len(_fact_entries(params)) == 1, "超时事实照记（诊断用），只是不再构成充分资格"

    records_before = [item.call_id for item in agent._model_call_ledger.records()]
    assert len(records_before) == 2

    decision = _no_tool_calls_decision(_no_tool_request(params, response))

    assert decision.action == "break"
    assert decision.response is response, "原样交付那一枪的响应对象，不替换、不改写"
    assert decision.response.text == _COMPLETE_ANSWER, "用户看到的就是重试那一枪的完整终答"
    assert decision.counters == ToolLoopRepairCounters(), "续跑计数必须为 0"
    assert params.tool_context == [], "不得回灌任何宿主续跑指令"
    assert backend.calls == 2, "裁决不得新增物理采样"
    assert [item.call_id for item in agent._model_call_ledger.records()] == records_before


def test_no_tool_work_round_never_buys_an_extra_sample(monkeypatch, tmp_path) -> None:
    """(a) 真实循环端到端：纯问答轮超时救回后原样收口，全文就是那一枪的正文。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        ["TIMEOUT", _COMPLETE_ANSWER, _RESUMED_ANSWER],
        run_id="run-narrow-qa-loop",
    )

    assert run.backend.calls == 2, "超时 + 重试；不再有续跑那一枪"
    assert run.response.text == _COMPLETE_ANSWER
    assert _RESUMED_ANSWER not in run.response.text
    assert run.params.executed_tools == [], "全程零工具执行"
    assert run.tool_round_calls == []
    assert run.executed_one_calls == 0 and run.tool.handler_calls == 0
    assert run.params.tool_context == [], "零宿主续跑指令"
    assert all(_PROVIDER_TIMEOUT_RESUME not in prompt for prompt in run.backend.prompts)
    records = run.agent._model_call_ledger.records()
    assert [item.status for item in records] == ["timed_out", "finished"], "账本无新采样"


# ---------------------------------------------------------------- (b) 有工作的轮仍续跑一次


def test_tool_work_timeout_rescue_still_resumes_exactly_once(monkeypatch, tmp_path) -> None:
    """(b) 有工具执行 + 超时救回 + 零工具调用承诺 -> 真实循环里恰好放行 1 次探针。

    R1 残留边界后这一枪是**探针**：它零工具调用 = 没有工具工作要继续，交付走无损 tie-break
    （原样交付 P/Q 中正文更长的一条，等长时探针那一枪）。本剧本两句等长，故交付文本与旧契约
    相同；采样次数、工具轮、工具副作用次数、指令出现位置等不变式全部不变。
    """
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-narrow-work",
    )

    assert run.backend.calls == 4, "工具采样 + 超时 + 重试 + 探针；之后没有第 5 枪"
    assert run.params.executed_tools == ["read_file"], "工作迹象来自产品自己的工具账本"
    assert run.tool_round_calls == [1], "整轮只进入一次工具轮"
    assert run.executed_one_calls == 1 and run.tool.handler_calls == 1, "工具副作用恰好一次"
    assert len(_PROMISE) == len(_FINAL), "前提：本剧本命中等长 tie 默认"
    assert run.response.text == _FINAL, "等长 tie 默认 = 探针那一枪（有工作的轮按原语义收口）"

    with_instruction = [
        index
        for index, prompt in enumerate(run.backend.prompts, start=1)
        if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4], "只有探针那一枪带宿主指令，重试那一枪不带"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert "read:notes.md" in run.backend.prompts[3], "探针建立在真实工具结果之上"
    # 探针不改账本：进入探针那一枪时工具账本与重试那一枪完全相同。
    assert run.backend.entries[1] == run.backend.entries[2] == run.backend.entries[3]
    assert _ledger_fingerprint(run.params) == {
        "executed_tools": ["read_file"],
        "tool_result_ids": ["call-1"],
    }
    records = run.agent._model_call_ledger.records()
    assert [(item.status, item.metadata["physical_attempt"]) for item in records] == [
        ("finished", 1),
        ("timed_out", 1),
        ("finished", 2),
        ("finished", 1),
    ], "探针是一枪新的 logical 采样，不是同一 logical turn 的第三次物理尝试"


# ---------------------------------------------------------------- (c) 正常完成不续跑


def test_tool_work_with_tool_call_response_is_not_resumed(tmp_path) -> None:
    """(c1) 有工具执行 + 超时救回 + 重试那一枪给出工具调用 -> 走既有工具执行，不续跑。"""
    params = _params(run_id="run-narrow-toolcall", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _PROMISE])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    _generate(agent, params)
    assert provider_timeout_resume_eligible(params) is True, "前提：两条结构化事实都在案"

    call = canonical_history_call(
        "read_file",
        _READ_ARGS,
        call_id="narrow-c1",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=SimpleNamespace(config=SimpleNamespace(enable_tools=True, max_tool_rounds=0)),
            params=params,
            response=ModelResponse(
                text=_PROMISE,
                backend=backend.name,
                tool_use_blocks=[
                    {"id": call.call_id, "name": call.tool_name, "input": dict(call.arguments)}
                ],
            ),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls, "工具调用照旧执行"
    assert decision.counters.provider_timeout_resume_repairs == 0
    assert params.tool_context == [], "不得回灌续跑指令"


def test_tool_work_with_explicit_terminal_state_is_not_resumed(tmp_path) -> None:
    """(c2) 有工具执行 + 超时救回 + 重试那一枪给出显式终态 -> 按终态收口，不续跑。"""
    params = _params(run_id="run-narrow-terminal", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _PROMISE])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    response = _generate(agent, params)
    assert provider_timeout_resume_eligible(params) is True

    terminal = replace(
        response,
        runtime_status="unfinished",
        runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="tool_loop",
    )
    decision = _no_tool_calls_decision(_no_tool_request(params, terminal))

    assert decision.action == "break"
    assert decision.response is terminal, "显式终态原样交付"
    assert decision.counters == ToolLoopRepairCounters()
    assert params.tool_context == []


def test_tool_work_round_completing_normally_never_gets_the_instruction(
    monkeypatch, tmp_path
) -> None:
    """(c3) 真实循环：超时救回后模型继续调工具并正常收口 -> 全程零续跑指令。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("tool", "read_file", _SECOND_READ_ARGS),
            _FINAL,
        ],
        run_id="run-narrow-normal",
    )

    assert run.backend.calls == 4, "工具 + 超时 + 重试（工具）+ 收口；没有第 5 枪"
    assert run.tool_round_calls == [1, 2], "重试那一枪的工具调用照旧执行"
    assert run.tool.handler_calls == 2, "两次真实工具副作用，零重放"
    assert run.response.text == _FINAL
    assert _PROVIDER_TIMEOUT_RESUME not in run.params.tool_context, "零续跑指令"
    assert all(_PROVIDER_TIMEOUT_RESUME not in prompt for prompt in run.backend.prompts)
    assert provider_timeout_resume_eligible(run.params) is False
