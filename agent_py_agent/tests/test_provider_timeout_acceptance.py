"""LLM: 门槛5「可恢复超时后模型只承诺不动作 → 轮内有界续跑一次」(ebb90d0c) 的对抗性验收用例。

被测改动两处产品代码：
- 生成层 ``agent_core/tool_model_generation.py``：门槛5 重试**成功**分支登记结构化事实
  ``live_archive_state["_provider_timeout_resume_turns"][model_turn]``。
- 裁决层 ``agent_core/tool_loop/response_decision.py``：同一 model turn 上、模型零工具调用时
  至多回灌一条宿主续跑指令（``_PROVIDER_TIMEOUT_RESUME_LIMIT = 1``）。

本文件与作者单测 ``test_provider_timeout_continuation.py`` 的分工：**不复用其断言**，只做四类对抗检查：
1. 重复工具副作用：真实 ``_execute_tool_loop_service`` + 真实工具 handler 计数 + 真实产品记账
   （``_record_tool_call`` / IR 账本）。证明续跑只多发一次模型采样，``_run_tool_round``、
   工具 handler、工具账本在续跑前后完全一致；并单独构造「工具已执行但结果尚未回灌」窗口的反例。
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

import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _EMPTY_TEXT_NUDGE,
    _PROVIDER_TIMEOUT_RESUME,
    _PROVIDER_TIMEOUT_RESUME_LIMIT,
    _TRUNCATED_OUTPUT_RESUME,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _ir_last_tool_use_confirmed,
    _logical_physical_attempt_count,
    _retry_once_after_timeout,
    generate_model_response,
    provider_timeout_resume_eligible,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
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

    counts = {"rounds": [], "exec_one": 0}
    real_run_tool_round = tls._run_tool_round

    def counting_tool_round(request):
        counts["rounds"].append(request.tool_rounds)
        return real_run_tool_round(agent, request)

    def counting_execute_one(request):
        counts["exec_one"] += 1
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
    """真实循环：工具执行 1 次 → 超时 → 重试 → 承诺 → 续跑 1 次（不发工具）。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-accept-once",
    )

    assert run.backend.calls == 4, "工具采样 + 超时 + 重试 + 续跑；续跑之后没有第 5 枪"
    assert run.tool_round_calls == [1], "整轮只允许进入一次工具轮"
    assert run.executed_one_calls == 1
    assert run.tool.handler_calls == 1, "工具 handler 副作用必须恰好一次"
    assert run.rounds == 1
    assert run.response.text == _FINAL

    # 续跑指令恰好出现在一次出站请求里（= 这个机制只多买了一枪模型采样）。
    with_instruction = [
        index for index, prompt in enumerate(run.backend.prompts, start=1) if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4], "只有续跑那一枪带宿主指令，重试那一枪不带"
    assert "read:notes.md" in run.backend.prompts[3], "续跑建立在真实工具结果之上"

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
            ToolLoopRepairCounters(provider_timeout_resume_repairs=_PROVIDER_TIMEOUT_RESUME_LIMIT),
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
        provider_timeout_resume_repairs=_PROVIDER_TIMEOUT_RESUME_LIMIT
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
    """闸门2：带未确认的用户补充输入时，重发会二次投递 -> 拒绝。"""
    for state_update in (
        {"_guidance_submission_id": "sub-1"},
        {"_guidance_ack_ids": {"sub-2"}},
    ):
        params = _params(run_id=f"run-gate-ambiguous-{sorted(state_update)[0]}")
        params.live_archive_state.update(state_update)
        request, backend, _agent = _gate_request(params, tmp_path)

        assert _retry_once_after_timeout(request, ProviderTimeoutError("t", stage="wall_clock")) is None
        assert backend.calls == 0
        assert "_provider_timeout_resume_turns" not in params.live_archive_state


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
