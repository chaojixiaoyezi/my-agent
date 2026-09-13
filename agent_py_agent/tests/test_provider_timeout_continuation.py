"""LLM: 门槛5 续跑合同单测——「可恢复供应商失败 + 本轮确实做过工作 + 模型零工具调用只承诺」
不再静默收口。

真机证据（用户 TUI 会话）：模型请求超时 → 门槛5 重试救回来 → 模型只回一句
「在的，刚才超时了，我重新来。」→ 之后什么都没发生，提示符空着。旧行为把这句话
当 plain final 直接收口：宿主不会因为自然语言再发一次请求（仓库铁律：自然语言不做
机器决策）。

规则（与既有策略的边界）：
- 触发只用结构化事实，两条同时成立才续跑（收窄 R1，见 test_provider_timeout_resume_narrowing.py）：
  ① 本轮那次物理模型调用所属的 model turn 序号被生成层登记为「发生过被重试救回的供应商
     超时」；② 本 run 的工具账本 ``executed_tools`` 非空 = 本轮确实干过活。
  绝不读模型正文——把承诺换成任意其它文本，行为完全一致（见 test_resume_is_text_independent）。
  纯问答轮（零工具执行）被超时救回后，那一枪的正文就是终答，不再被追问一次顶掉。
- 门槛5 的 fail-closed 重试资格判定一行未放宽：续跑只在「重试确实打出去且拿到了
  响应」之后发生；不具资格/重试再超时的路径照旧抛 ProviderTimeoutError。
- 有界：至多 1 次，与截断续跑（_TRUNCATED_OUTPUT_RESUME_LIMIT）分开计数、互不吃预算。
- 不重放工具：续跑只追加一条宿主指令并 continue，工具调用账（executed_tools）不变。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _PROVIDER_TIMEOUT_RESUME,
    _PROVIDER_TIMEOUT_RESUME_LIMIT,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _ir_last_tool_use_confirmed,
    _retry_once_after_timeout,
    generate_model_response,
    provider_timeout_resume_eligible,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)

# 超时预算必须是正数且极小，才能让「挂起」的假后端在墙钟保护线程里被掐断。
_TINY_REQUEST_TIMEOUT = 0.02
# 裁决层要求协议快照与运行快照同源；测试只用一个只读工具即可。
_TOOL_SPEC = make_test_model_spec("read_file")


# ---------------------------------------------------------------- helpers


# LLM: 假 agent 只需生成层真正读取的三个字段：backend / config.request_timeout /
#   _model_call_ledger（由 model_call_ledger() 惰性挂载）。不要给测试塞真实 agent。
# 函数用途: 构造调用 generate_model_response 所需的最小宿主外壳。
def _agent(backend: object) -> SimpleNamespace:
    return SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(request_timeout=_TINY_REQUEST_TIMEOUT),
        _current_subagent_run_id="",
    )


# LLM: 每次调用一个新的 params 实例（live_archive_state 是同一 run 的结构化事实容器，
#   不能被测试之间串用）。native 协议是工具轮真实形态，采用它。
#   账本预置一条「本轮已执行过工具」：收窄(R1)后它是续跑的第二个必要条件，本文件钉的正是
#   真机形状「干活干到一半超时」；纯问答轮形状（零工具执行 → 原样交付那一枪正文）由
#   test_provider_timeout_resume_narrowing.py 覆盖。
# 函数用途: 构造一次工具模型轮所需的运行参数。
def _params(*, run_id: str = "run-timeout") -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="继续把剩下的工作做完",
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
        executed_tools=["read_file"],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id=run_id,
            source_protocol="native",
        ),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(
            (_TOOL_SPEC,),
            run_id=run_id,
        ),
        tool_ir_history=[],
        save=False,
        delivery_contract={},
    )


# LLM: 第一次物理调用挂起触发墙钟超时，之后每次都直接给出模型正文（零工具调用）——
#   正是真机「重试救回来了，但模型只回一句承诺」的形状。
# 类用途: 模拟「超时 + 重试成功但只承诺」的供应商。
class _TimeoutThenPromiseBackend:
    name = "timeout-then-promise-backend"

    def __init__(self, text: str, *, fail_calls: int = 1) -> None:
        self.text = text
        self.fail_calls = fail_calls
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_calls:
            time.sleep(0.5)  # > request_timeout -> wall_clock 超时
        return ModelResponse(text=self.text, backend=self.name)


# LLM: 正常供应商：零失败、一次成功，用来证明无超时时行为一个字节都不变。
# 类用途: 模拟始终正常应答的供应商。
class _HealthyBackend:
    name = "healthy-backend"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text=self.text, backend=self.name)


def _generate(backend: object, params: ToolLoopExecuteParams):
    agent = _agent(backend)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="继续把剩下的工作做完",
            tool_rounds=1,
        )
    )
    return agent, response


# LLM: 裁决层的输入必须带上与生成层同源的 model turn 序号——params.live_archive_state
#   就是那个唯一权威容器，测试不另建一套序号来源。
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


# ---------------------------------------------------------------- 1. 生成层结构化登记


def test_retry_success_registers_structured_resume_fact() -> None:
    """超时后重试成功 -> 本轮 model turn 序号被登记为可续跑（与账本同源）。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    agent, response = _generate(backend, params)

    assert response.text == "在的，刚才超时了，我重新来。"
    assert backend.calls == 2, "门槛5 行为不变：超时后恰好重试一次"
    assert provider_timeout_resume_eligible(params) is True
    fact = params.live_archive_state["_provider_timeout_resume_turns"]
    assert isinstance(fact, dict) and len(fact) == 1
    entry = next(iter(fact.values()))
    assert entry["reason"] == "provider_timeout_retried"
    assert entry["timeout_stage"] == "wall_clock"
    # 账本交叉验证：同一 logical 分组的两次物理尝试，attempt-1 超时、attempt-2 成功。
    records: ModelCallLedger = agent._model_call_ledger
    rows = records.records()
    assert [item.metadata["physical_attempt"] for item in rows] == [1, 2]
    assert rows[0].status == "timed_out" and rows[1].status == "finished"
    assert rows[0].metadata["logical_call_id"] == rows[1].metadata["logical_call_id"]


def test_healthy_turn_registers_no_resume_fact() -> None:
    """没有任何供应商失败 -> 不登记续跑资格（触发判据不是「模型像在承诺」）。"""
    params = _params()
    backend = _HealthyBackend("在的，刚才超时了，我重新来。")
    agent, _ = _generate(backend, params)

    assert backend.calls == 1
    assert provider_timeout_resume_eligible(params) is False
    assert "_provider_timeout_resume_turns" not in params.live_archive_state
    assert agent._model_call_ledger.records()[0].status == "finished"


# ---------------------------------------------------------------- 2. 续跑 + 宿主指令


def test_timeout_then_zero_tool_call_promise_resumes_once() -> None:
    """(a) 结构化超时 + 本轮执行过工具 + 模型零工具调用只承诺 -> 恰好 1 次续跑。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    _agent_obj, response = _generate(backend, params)

    assert params.executed_tools == ["read_file"], "前提：本轮确实做过工具工作（收窄后的必要条件）"
    before = list(params.executed_tools)
    decision = _no_tool_calls_decision(_no_tool_request(params, response))

    assert decision.action == "continue", "承诺不能当成收口"
    assert decision.calls == [], "续跑不得派生任何工具调用"
    assert decision.counters.provider_timeout_resume_repairs == 1
    assert params.tool_context == [_PROVIDER_TIMEOUT_RESUME]
    instruction = params.tool_context[0]
    # 指令文案合同：不点名工具、不道歉、不复述，要求从断点继续把剩余工作做完。
    assert instruction.startswith("[provider-timeout-resume]")
    assert "从断点继续把剩余工作做完" in instruction
    assert "不要道歉" in instruction and "不要复述" in instruction
    assert "本条指令不指定任何具体工具" in instruction
    assert params.executed_tools == before, "续跑绝不重放已执行的工具"


def test_timeout_then_promise_reaches_resume_through_full_decision_entry() -> None:
    """真实入口 tool_loop_response_decision 同样放行续跑（不是只有内部函数生效）。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("好的，我马上继续。")
    _agent_obj, response = _generate(backend, params)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=SimpleNamespace(config=SimpleNamespace(enable_tools=True, max_tool_rounds=0)),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "continue"
    assert decision.counters.provider_timeout_resume_repairs == 1
    assert decision.calls == []


# ---------------------------------------------------------------- 3. 有界：用尽后原语义收口


def test_resume_budget_exhausted_closes_with_original_semantics() -> None:
    """(b) 续跑上限用尽后按原语义收口：不无限续跑，响应与计数都不再被改写。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    _agent_obj, response = _generate(backend, params)

    counters = ToolLoopRepairCounters(
        provider_timeout_resume_repairs=_PROVIDER_TIMEOUT_RESUME_LIMIT
    )
    decision = _no_tool_calls_decision(_no_tool_request(params, response, counters))

    assert decision.action == "break"
    assert decision.response is response, "原样交付模型回复，不伪造/改写终态"
    assert decision.counters == counters
    assert params.tool_context == [], "超限后不得再回灌续跑指令"


def test_second_decision_after_a_resume_does_not_resume_again() -> None:
    """同一轮里连续裁决两次：第一次续跑，第二次预算用尽按原语义收口。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    _agent_obj, response = _generate(backend, params)

    first = _no_tool_calls_decision(_no_tool_request(params, response))
    assert first.action == "continue"
    assert len(params.tool_context) == 1

    # 第二次物理模型调用会产生新的 model turn 序号（真实循环里就是再发一次请求，
    # 那一枪本身也有自己的超时预算）；这里直接复用同一响应验证预算闸。
    second = _no_tool_calls_decision(
        _no_tool_request(params, response, first.counters)
    )
    assert second.action == "break"
    assert len(params.tool_context) == 1, "至多回灌一条续跑指令"


# ---------------------------------------------------------------- 4. 正常回合行为不变


def test_healthy_turn_behaviour_is_unchanged() -> None:
    """(c) 无失败的正常收口：一个字节都不改（break + 原响应 + 空 tool_context）。"""
    params = _params()
    backend = _HealthyBackend("在的，刚才超时了，我重新来。")
    _agent_obj, response = _generate(backend, params)

    decision = _no_tool_calls_decision(_no_tool_request(params, response))

    assert decision.action == "break"
    assert decision.response.runtime_status == "ok"
    assert decision.counters.provider_timeout_resume_repairs == 0
    assert params.tool_context == []


def test_plain_promise_without_structured_failure_does_not_resume() -> None:
    """没有结构化失败时，同样的「我重新来」正文不触发续跑（对照）。"""
    params = _params()
    backend = _HealthyBackend("在的，刚才超时了，我重新来。")
    _agent_obj, _response = _generate(backend, params)

    decision = _no_tool_calls_decision(
        _no_tool_request(
            params,
            ModelResponse(text="在的，刚才超时了，我重新来。", backend="x"),
        )
    )

    assert decision.action == "break"
    assert params.tool_context == []


# ---------------------------------------------------------------- 5. 有工具调用不触发续跑


def test_tool_calls_never_trigger_provider_resume() -> None:
    """(d) 本轮有工具调用 -> 走既有工具执行三岔路，续跑分支根本不参与。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("我重新来。")
    _agent_obj, _response = _generate(backend, params)
    assert provider_timeout_resume_eligible(params) is True, "前提：本轮确有超时事实在案"

    call = canonical_history_call(
        "read_file",
        {"path": "x.md"},
        call_id="pt-c1",
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
                text="我重新来。",
                backend="x",
                tool_use_blocks=[
                    {"id": call.call_id, "name": call.tool_name, "input": dict(call.arguments)}
                ],
            ),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action != "continue", "有工具调用就不该被续跑改写成再发一次模型"
    assert decision.calls or decision.action == "break"
    assert decision.counters.provider_timeout_resume_repairs == 0
    assert params.tool_context == []


# ---------------------------------------------------------------- 6. 真实工具轮循环端到端


# LLM: 这一步把两个接缝接起来：真实生成路径（假后端 → 门槛5 重试 → 结构化登记）
#   与真实工具轮主循环（裁决 → continue → 再发一次带宿主指令的请求）。断言只读
#   结构化事实与真实出站 prompt，不断言任何自然语言判定。
# 类用途: 在真实工具轮循环里验证续跑确实重发了模型调用并带上了宿主指令。
class _LoopAgent(SimpleNamespace):
    __test__ = False

    def __init__(self, backend: object, prompts: _RecordingPrompts) -> None:
        super().__init__(
            backend=backend,
            config=SimpleNamespace(
                request_timeout=_TINY_REQUEST_TIMEOUT,
                enable_tools=True,
                max_tool_rounds=0,
            ),
            prompts=prompts,
            _current_subagent_run_id="",
        )


# LLM: 只记录真正发给模型的 user task 段；测试不复制宿主 prompt 模板，只断言续跑
#   指令确实出现在下一次出站请求里。
# 类用途: 记录每次 prompt 组装出的出站文本，供断言语义正确性。
class _RecordingPrompts:
    __test__ = False

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, user_prompt, memories, *, inject=None, prompt_files=None, **kwargs):
        text = "\n".join(
            [str(user_prompt)]
            + [str(item) for item in (inject or [])]
            + [
                str(item)
                for item in (kwargs.get("tools").tool_context if kwargs.get("tools") else [])
            ]
        )
        self.built.append(text)
        return text


def test_loop_resumes_and_sends_host_instruction(monkeypatch) -> None:
    """端到端：真实循环在超时重试后零工具调用的承诺上续跑，并真的再发一次请求。"""
    from agent_py_agent.agent.agent_core import _tool_loop_service
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        ToolLoopService,
        _execute_tool_loop_service,
    )

    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    prompts = _RecordingPrompts()
    agent = _LoopAgent(backend, prompts)
    params = _params(run_id="run-e2e")

    # 该 run 的账本已预置「本轮执行过工具」（收窄后的第二个续跑事实）；循环里不再有新工具
    # 执行，工具轮执行入口一旦被调用即说明续跑重放了工具。
    executed: list[object] = []
    service = ToolLoopService(agent)
    monkeypatch.setattr(
        service,
        "_run_tool_round",
        lambda request: (executed.append(request), (request.tool_rounds, None))[1],
    )
    monkeypatch.setattr(
        _tool_loop_service,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: _tool_loop_service._render_tool_loop_prompt(
            agent, loop_params
        ),
    )

    _prompt, response, _rounds = _execute_tool_loop_service(service, params)

    assert backend.calls == 3, (
        "超时 1 次 + 门槛5 重试 1 次 + 续跑 1 次；续跑那一枪是新的 model turn，"
        "没有超时事实，不会再有第 4 次"
    )
    # 探针那一枪零工具调用 = 本轮没有更多工具工作：两枪的合法正文按到达顺序无损合成。
    # 本剧本两枪正文相同（fake 后端对两次调用都回同一句），因此交付正文是同一句出现两次——
    # 这正是"不许因为内容/长度丢掉任何一条已产生的合法答复"的直接体现。
    assert response.text == "在的，刚才超时了，我重新来。\n\n在的，刚才超时了，我重新来。", (
        "两段逐字保留，按产生顺序交付"
    )
    assert executed == [], "续跑绝不进入工具执行路径"
    # 门槛5 的重试复用同一份 prompt（不重新组装，行为不变），所以只有 2 次组装：
    # 第 1 次正常组装、第 2 次就是续跑那一枪——它必须带上宿主续跑指令。
    assert len(prompts.built) == 2
    assert _PROVIDER_TIMEOUT_RESUME not in prompts.built[0]
    assert _PROVIDER_TIMEOUT_RESUME in prompts.built[1]
    # 结构化事实与作用域：只登记「超时那一轮」这一个 model turn（门槛5 重试复用同一轮，
    # 不产生新序号），且续跑那一轮结束后序号已前移 —— 陈旧事实不会再次触发续跑。
    turns = params.live_archive_state["_provider_timeout_resume_turns"]
    assert len(turns) == 1
    assert provider_timeout_resume_eligible(params) is False, "事实不跨 turn 生效"


# ---------------------------------------------------------------- 7. 触发与模型文案无关
def test_resume_is_text_independent() -> None:
    """(e) 把承诺换成任意其它文本（含空承诺、英文、乱码），行为完全一致。"""
    texts = [
        "在的，刚才超时了，我重新来。",
        "I will retry now.",
        "好的。",
        "……",
        "完全没有提到超时或重试的一句普通回答。",
    ]

    for text in texts:
        params = _params(run_id=f"run-text-{abs(hash(text))}")
        backend = _TimeoutThenPromiseBackend(text)
        agent, response = _generate(backend, params)
        assert provider_timeout_resume_eligible(params) is True, text

        decision = _no_tool_calls_decision(_no_tool_request(params, response))
        assert decision.action == "continue", text
        assert decision.counters.provider_timeout_resume_repairs == 1, text
        assert params.tool_context == [_PROVIDER_TIMEOUT_RESUME], text
        assert agent._model_call_ledger.records()[0].status == "timed_out", text


# ---------------------------------------------------------------- 7. 预算互相独立


def test_resume_budget_is_independent_from_truncated_output_budget() -> None:
    """续跑预算与截断续跑预算分开计数，任何一方都不吃另一方的额度。"""
    params = _params()
    backend = _TimeoutThenPromiseBackend("在的，刚才超时了，我重新来。")
    _agent_obj, response = _generate(backend, params)

    # 截断续跑已用满，超时续跑额度仍然独立可用。
    counters = ToolLoopRepairCounters(truncated_output_repairs=99)
    decision = _no_tool_calls_decision(_no_tool_request(params, response, counters))

    assert decision.action == "continue"
    assert decision.counters.provider_timeout_resume_repairs == 1
    assert decision.counters.truncated_output_repairs == 99, "不得改动截断续跑计数"


# ---------------------------------------------------------------- 8. fail-closed 未被放宽


def test_fail_closed_retry_eligibility_is_not_relaxed() -> None:
    """孤儿 tool_use 仍然不重试（门槛5 判定未放宽），因此也不会有续跑资格。"""
    params = _params(run_id="run-orphan")
    call = canonical_history_call(
        "read_file",
        {"path": "x.md"},
        call_id="pt-orphan",
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-1",
        attempt_id=params.request_id,
    )
    params.tool_ir_history[:] = [AssistantTurn(tool_calls=[call])]  # 无配对回执 = 孤儿

    agent = _agent(_TimeoutThenPromiseBackend("我重新来。"))
    request = ModelGenerateParams(
        agent=agent,
        params=params,
        prompt="继续",
        tool_rounds=1,
    )
    assert _ir_last_tool_use_confirmed(request) is False, "fail-closed 判定保持原样"
    assert provider_timeout_resume_eligible(params) is False, "孤儿场景不得有续跑资格"

    # 真实路径：孤儿 + 永远超时 -> 直接上抛，既没有第三次尝试，也没有续跑资格。
    assert (
        _retry_once_after_timeout(
            request,
            ProviderTimeoutError("timeout", stage="wall_clock"),
        )
        is None
    )
    assert provider_timeout_resume_eligible(params) is False
