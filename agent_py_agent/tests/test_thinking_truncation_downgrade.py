"""LLM: 「只思考被输出上限截断」续跑降档的合同单测。

真机证据（gwreq-1791538381）：45 分钟、60 次模型调用、末两次调用只有思考（零正文零工具调用），
各约 160 秒顶满 65536 上限，turn_end_reason=max-tokens、runtime_reason=MODEL_RESPONSE_TRUNCATED
（来源 model_provider）；原续跑提示"接着写"会把模型引回继续展开推理，两次预算用完整轮失败。

规则（通用，不针对具体供应商）：
- 识别只看结构化事实：长度截断三件套（truncated + unfinished + MODEL_RESPONSE_TRUNCATED +
  model_provider）加零工具调用、零可见正文；不解析思考内容。
- 续跑那一次给紧邻下一次调用降一档（max→high→medium），只影响一次、下一次恢复；至多降一次。
- 供应商不支持强度参数记 unsupported、没有更低档位记 already_lowest、本轮已降过记
  already_downgraded，三种情况都照常续跑。
- 正文截断保持原提示、不降档；开关关闭时完全原行为（连提示都不变）。
"""

from __future__ import annotations

import types

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _THINKING_ONLY_TRUNCATION_RESUME,
    _TRUNCATED_OUTPUT_RESUME,
    _TRUNCATED_OUTPUT_RESUME_COUNT,
    ToolLoopRepairCounters,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.settings.reasoning_effort import (
    clear_next_call_reasoning_level,
    next_call_reasoning_level,
    request_reasoning_options,
    set_next_call_reasoning_level,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)

# ---------------------------------------------------------------- helpers


# LLM: 假 agent 只需降档决策与档位解析真正读取的字段：config（默认档位/开关）与 backend（控制方式）。
# 函数用途: 构造裁决层测试的最小宿主外壳。
def _agent(*, effort: str = "max", downgrade: bool = True, control: str = "effort"):
    backend = types.SimpleNamespace(reasoning_control=control, name="fake-backend")
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            enable_tools=True,
            model_reasoning_effort=effort,
            model_truncation_thinking_downgrade=downgrade,
        ),
        backend=backend,
    )


# LLM: 供应商归一后的真实截断响应（带 runtime 三件套，与 response_completion 同形）；text 空
#   代表"只思考截断"。裁决层只读结构化字段，不解析正文。
# 函数用途: 构造一次「零工具调用」截断裁决请求与它的 params。
def _request(*, text: str = "", counters=None, agent=None):
    params = types.SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=[],
    )
    response = ModelResponse(
        text=text,
        backend="openai_compatible",
        truncated=True,
        runtime_status="unfinished",
        runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )
    return _NoToolCallsRequest(
        agent=agent if agent is not None else _agent(),
        params=params,
        response=response,
        counters=counters or ToolLoopRepairCounters(),
        has_protected_marker=False,
    ), params


# ---------------------------------------------------------------- 1. 只思考截断：降档 + 专门提示


# LLM: 只思考截断必须走"先推进再继续"的专门提示，并把紧邻下一次调用降一档、记次数与 trace。
# 函数用途: 验证首次只思考截断返回 continue、专门提示、降档 high、counters 与 trace 就位。
def test_thinking_only_truncation_downgrades_next_call() -> None:
    request, params = _request(text="")
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    instruction = str(params.tool_context[-1])
    assert instruction == _THINKING_ONLY_TRUNCATION_RESUME
    assert "先用一个工具动作或简短答复推进" in instruction
    assert "不要再展开长推理" in instruction
    assert decision.counters.truncated_output_repairs == 1
    assert decision.counters.truncation_thinking_downgrades == 1
    assert next_call_reasoning_level(params) == "high"
    assert params.live_archive_state["truncation_thinking_downgrade_trace"] == [
        {"from_level": "max", "to_level": "high", "outcome": "downgraded"}
    ]


# LLM: 降档至多一次：本轮已降过时第二次截断不再降、不再挂覆盖，只如实记 already_downgraded。
# 函数用途: 验证第二次只思考截断不重复降档、trace 记 already_downgraded。
def test_thinking_only_truncation_second_time_does_not_downgrade_again() -> None:
    counters = ToolLoopRepairCounters(truncation_thinking_downgrades=1)
    request, params = _request(text="", counters=counters)
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    assert str(params.tool_context[-1]) == _THINKING_ONLY_TRUNCATION_RESUME
    assert decision.counters.truncation_thinking_downgrades == 1
    assert next_call_reasoning_level(params) == ""
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    assert [row["outcome"] for row in trace] == ["already_downgraded"]


# LLM: 有可见正文的截断不是"只思考"，必须保持原"接着写"提示且一个档位都不降。
# 函数用途: 验证正文截断走原提示、不降档、不写 trace。
def test_visible_text_truncation_keeps_original_resume_without_downgrade() -> None:
    request, params = _request(text="我已经写完了第一段，还剩两段")
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    assert str(params.tool_context[-1]) == _TRUNCATED_OUTPUT_RESUME
    assert "先用一个工具动作或简短答复推进" not in str(params.tool_context[-1])
    assert decision.counters.truncation_thinking_downgrades == 0
    assert next_call_reasoning_level(params) == ""
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state


# LLM: 开关关闭时行为和改动前完全一样：原提示、不降档、不写 trace。
# 函数用途: 验证开关关的只思考截断保持原行为。
def test_downgrade_disabled_keeps_original_behaviour() -> None:
    request, params = _request(text="", agent=_agent(downgrade=False))
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    assert str(params.tool_context[-1]) == _TRUNCATED_OUTPUT_RESUME
    assert decision.counters.truncation_thinking_downgrades == 0
    assert next_call_reasoning_level(params) == ""
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state


# LLM: 供应商不支持强度参数时只如实记 unsupported、照常续跑，不挂覆盖。
# 函数用途: 验证 control=none 的只思考截断：专门提示 + unsupported + 零降档。
def test_unsupported_control_records_unsupported_and_resumes() -> None:
    request, params = _request(text="", agent=_agent(control="none"))
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    assert str(params.tool_context[-1]) == _THINKING_ONLY_TRUNCATION_RESUME
    assert decision.counters.truncation_thinking_downgrades == 0
    assert next_call_reasoning_level(params) == ""
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    assert [row["outcome"] for row in trace] == ["unsupported"]


# LLM: 已是最低档（medium 及以下）时只如实记 already_lowest、照常续跑，不挂覆盖。
# 函数用途: 验证 medium 档的只思考截断：专门提示 + already_lowest + 零降档。
def test_already_lowest_records_already_lowest_and_resumes() -> None:
    request, params = _request(text="", agent=_agent(effort="medium"))
    decision = _no_tool_calls_decision(request)

    assert decision.action == "continue"
    assert str(params.tool_context[-1]) == _THINKING_ONLY_TRUNCATION_RESUME
    assert decision.counters.truncation_thinking_downgrades == 0
    assert next_call_reasoning_level(params) == ""
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    assert [row["outcome"] for row in trace] == ["already_lowest"]


# LLM: 降档不改变续跑预算：预算用尽仍按原终态收口，也不触发降档决策。
# 函数用途: 验证超限的只思考截断直接 break、响应原样、不降档。
def test_resume_budget_exhausted_breaks_unchanged() -> None:
    counters = ToolLoopRepairCounters(truncated_output_repairs=_TRUNCATED_OUTPUT_RESUME_COUNT)
    request, params = _request(text="", counters=counters)
    decision = _no_tool_calls_decision(request)

    assert decision.action == "break"
    assert decision.response.runtime_status == "unfinished"
    assert decision.response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert params.tool_context == []
    assert decision.counters.truncation_thinking_downgrades == 0
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state


# ---------------------------------------------------------------- 2. 一次性覆盖：读/写/清


# LLM: 覆盖只作用于紧邻下一次请求：挂上后（宿主开始下一次模型生成、序号越过武装点）请求档位
#   变为降档值，清除后恢复用户配置档位。
# 函数用途: 验证覆盖的读/写/清与 request_reasoning_options 的优先读取。
def test_override_applies_to_next_request_then_recovers() -> None:
    params = types.SimpleNamespace(
        live_archive_state={"_model_turn_sequence": 1},
        task_attributes={},
        context_scope="task_local",
    )
    agent = types.SimpleNamespace(config=types.SimpleNamespace(model_reasoning_effort="max"))
    backend = types.SimpleNamespace(reasoning_control="effort")

    assert request_reasoning_options(agent, params, backend, forced=False) == (False, "max")
    assert set_next_call_reasoning_level(params, "high") is True
    # 覆盖绑定武装序号：宿主开始下一次模型生成后（序号 +1）它才生效。
    params.live_archive_state["_model_turn_sequence"] = 2
    assert request_reasoning_options(agent, params, backend, forced=False) == (False, "high")
    clear_next_call_reasoning_level(params)
    assert request_reasoning_options(agent, params, backend, forced=False) == (False, "max")


# LLM: 伪 params（无 live_archive_state）下覆盖 helper 必须全部退化为无操作、绝不抛异常。
# 函数用途: 验证覆盖 helper 对伪 params 与非法档位的安全边界。
def test_override_helpers_are_safe_on_pseudo_params() -> None:
    pseudo = types.SimpleNamespace()
    assert next_call_reasoning_level(pseudo) == ""
    assert set_next_call_reasoning_level(pseudo, "high") is False
    clear_next_call_reasoning_level(pseudo)
    assert set_next_call_reasoning_level(types.SimpleNamespace(live_archive_state={}), "bogus") is False


# ---------------------------------------------------------------- 3. fake LLM：真工具循环端到端

_TINY_REQUEST_TIMEOUT = 5.0
# 裁决层要求协议快照与运行快照同源；测试只用一个只读工具即可。
_TOOL_SPEC = make_test_model_spec("read_file")


# LLM: 假后端每次调用都返回「只思考 + 长度截断」的真实归一形态（text 空 + runtime 三件套），
#   并记录每次真正收到的推理档位——端到端断言只读这两个结构化事实。
# 类用途: 模拟"推理先顶到输出上限、零正文零工具调用"的供应商。
class _ThinkingTruncationBackend:
    name = "thinking-truncation-backend"
    supports_provider_request_options = True
    reasoning_control = "effort"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_efforts: list[str] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        options = kwargs.get("request_options")
        self.seen_efforts.append(str(getattr(options, "reasoning_effort", "") or ""))
        return ModelResponse(
            text="",
            backend=self.name,
            truncated=True,
            runtime_status="unfinished",
            runtime_reason="MODEL_RESPONSE_TRUNCATED",
            runtime_source="model_provider",
        )


# LLM: 只记录真正发给模型的 user task 段；测试不复制宿主 prompt 模板，只断言续跑指令确实
#   出现在下一次出站请求里（与门槛5 端到端测试同一记录方式）。
# 类用途: 记录每次 prompt 组装出的出站文本，供断言宿主指令是否被带上。
class _RecordingPrompts:
    __test__ = False

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, user_prompt, memories, *, inject=None, **kwargs):
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


# LLM: 端到端循环用的最小宿主外壳：backend / config（含档位与开关）/ prompts / 子代理 run 标记。
# 类用途: 让真工具循环在本测试里跑在假后端上。
class _LoopAgent(types.SimpleNamespace):
    __test__ = False

    def __init__(self, backend: object, prompts: _RecordingPrompts, *, downgrade: bool = True) -> None:
        super().__init__(
            backend=backend,
            config=types.SimpleNamespace(
                request_timeout=_TINY_REQUEST_TIMEOUT,
                enable_tools=True,
                max_tool_rounds=0,
                model_reasoning_effort="max",
                model_truncation_thinking_downgrade=downgrade,
            ),
            prompts=prompts,
            _current_subagent_run_id="",
        )


# LLM: 每次调用一个新的 params 实例（live_archive_state 是同一 run 的结构化事实容器，
#   不能被测试之间串用）。native 协议是工具轮真实形态，采用它。
# 函数用途: 构造一次工具模型轮所需的运行参数。
def _loop_params(*, run_id: str) -> ToolLoopExecuteParams:
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
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(
            (_TOOL_SPEC,),
            run_id=run_id,
        ),
        tool_ir_history=[],
        save=False,
        delivery_contract={},
    )


# LLM: 循环里工具轮执行入口一旦被调用即说明续跑重放了工具（本剧本不该发生）；prompt 组装走
#   真实渲染函数，其余（工具执行）按门槛5 端到端测试同一方式短路。
# 函数用途: 打桩工具轮与 prompt 组装，返回记录工具执行请求的列表。
def _patch_loop(monkeypatch, agent) -> list[object]:
    from agent_py_agent.agent.agent_core import _tool_loop_service

    executed: list[object] = []
    monkeypatch.setattr(
        _tool_loop_service,
        "_run_tool_round",
        lambda _agent, request: (executed.append(request), (request.tool_rounds, None))[1],
    )
    monkeypatch.setattr(
        _tool_loop_service,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: _tool_loop_service._render_tool_loop_prompt(agent, loop_params),
    )
    return executed


# LLM: 端到端主剧本：连续两次"只思考 + 截断"后按原终态收口；第二次调用降一档、第三次恢复，
#   且续跑绝不进入工具执行路径。断言只读结构化事实与真实出站档位。
# 函数用途: 验证真工具循环里的降档-恢复全链路。
def test_loop_downgrades_second_call_then_recovers(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core import _tool_loop_service

    backend = _ThinkingTruncationBackend()
    prompts = _RecordingPrompts()
    agent = _LoopAgent(backend, prompts, downgrade=True)
    params = _loop_params(run_id="run-downgrade")
    executed = _patch_loop(monkeypatch, agent)

    loop_result = _tool_loop_service.execute_tool_loop(agent, params)

    assert backend.calls == 3, "两次续跑预算：第 3 次按原终态收口"
    assert backend.seen_efforts == ["max", "high", "max"], (
        "第 1 次用用户档位；第 2 次续跑降一档 high；第 3 次恢复用户档位"
    )
    assert executed == [], "续跑绝不进入工具执行路径"
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    assert [row["outcome"] for row in trace] == ["downgraded", "already_downgraded"]
    assert loop_result.final_response.runtime_status == "unfinished"
    assert loop_result.final_response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert len(prompts.built) == 3
    assert "先用一个工具动作或简短答复推进" in prompts.built[1]


# LLM: 开关关闭时档位一次都不降、trace 不出现、提示仍是原来的"接着写"。
# 函数用途: 验证开关关的端到端行为与改动前一致。
def test_loop_downgrade_disabled_keeps_original_behaviour(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core import _tool_loop_service

    backend = _ThinkingTruncationBackend()
    prompts = _RecordingPrompts()
    agent = _LoopAgent(backend, prompts, downgrade=False)
    params = _loop_params(run_id="run-disabled")
    executed = _patch_loop(monkeypatch, agent)

    loop_result = _tool_loop_service.execute_tool_loop(agent, params)

    assert backend.calls == 3
    assert backend.seen_efforts == ["max", "max", "max"], "开关关：档位一次都不降"
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state
    assert executed == []
    assert loop_result.final_response.runtime_status == "unfinished"
    assert "先用一个工具动作或简短答复推进" not in prompts.built[1]


# LLM: 正文截断（有可见正文）不降档、用原提示续跑；下一次调用档位保持用户配置。
# 类用途: 模拟"先写正文、被输出上限截断"的供应商，第二枪给出完整终答。
class _TextTruncationThenDoneBackend(_ThinkingTruncationBackend):
    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        options = kwargs.get("request_options")
        self.seen_efforts.append(str(getattr(options, "reasoning_effort", "") or ""))
        if self.calls == 1:
            return ModelResponse(
                text="我已经写完了第一段",
                backend=self.name,
                truncated=True,
                runtime_status="unfinished",
                runtime_reason="MODEL_RESPONSE_TRUNCATED",
                runtime_source="model_provider",
            )
        return ModelResponse(text="全部完成了。", backend=self.name)


# 函数用途: 验证正文截断在真循环里保持原提示、零降档。
def test_loop_visible_text_truncation_keeps_original_resume(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core import _tool_loop_service

    backend = _TextTruncationThenDoneBackend()
    prompts = _RecordingPrompts()
    agent = _LoopAgent(backend, prompts, downgrade=True)
    params = _loop_params(run_id="run-text")
    _patch_loop(monkeypatch, agent)

    loop_result = _tool_loop_service.execute_tool_loop(agent, params)

    assert backend.calls == 2
    assert backend.seen_efforts == ["max", "max"], "正文截断不降档"
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state
    assert "请直接从被截断处接着写" in prompts.built[1]
    assert loop_result.final_response.text == "全部完成了。"
