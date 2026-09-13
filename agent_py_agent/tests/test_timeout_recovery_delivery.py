"""LLM: 门槛5 超时探针的**无损交付**合同单测（取代旧的"按正文长度二选一"）。

背景（验收监督的判决）：上一版用 `len(P)>=len(Q)` 在「探针之前那一枪 P」与「探针那一枪 Q」
之间二选一，并把另一条丢掉。长度只是代理指标，**不是无损**：冗长的旧承诺/错误总结会压掉较短
的新纠正/正确结论。本文件钉住新契约：

1. 交付 = 两条已产生的合法答复按**到达顺序**合成为**有序段落**（逐字投影，只加段落边界），
   任何一段都不因正文长短、内容、语气被丢弃；宿主不读正文做语义判定。
2. 只有两种**结构化**原因会把一段排除出终答正文：该段自己被供应商输出上限截断
   （`truncated`，本就不属于合法终答，是仓库既有截断契约）、该段正文为空（没有字符可投影）。
   两者都只看该段自己的字段，与另一段写了什么无关；被排除的段仍以结构化原因记进交付账本。
3. 交付对象只从**本次裁决所在那一轮**的响应派生（`replace` 只换 text），因此主循环返回的
   `final_prompt`（本轮出站 prompt）、`final_response`、`usage` 的 request/turn 来源结构性同一，
   不会再出现"prompt 属探针那一枪、response 属上一枪"的跨枪混配。
4. 探针那一枪带工具调用 / 零工具执行的轮 / 预算用尽的轮：语义一律不变（工具照旧真执行，
   不买多余枪，不重放工具）。

受控边界：全部用例使用 fake 后端与本地 IR/工具夹具，不含真实供应商超时；真机验收不在本文件内。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _PROVIDER_TIMEOUT_RESUME,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)

# 供应商超时预算必须极小，才能让「挂起」的 fake 后端被墙钟保护拿下（与产品默认值无关）。
_TINY_REQUEST_TIMEOUT = 0.02
# 形状一（监督指出的 bug）：P 是一段冗长的错误总结，Q 是一句短而正确的纠正/结论。
# 旧实现按长度取 P，用户永远看不到 Q —— 这两句必须都出现，且 P 在前。
_PRIOR_LONG_WRONG_SUMMARY = (
    "我重新梳理了一遍：这个仓库里凡是带 enable_ 前缀的开关都应该保持关闭，"
    "另外刚才那次超时是因为网络抖动，跟工具执行无关，剩下的工作其实已经做完了，"
    "不需要再改任何配置。"
)
_TERMINAL_SHORT_CORRECT_CONCLUSION = "纠正：enable_tools 必须是开启的。"
# 形状二：P 只承诺，Q 用文本把活干完。
_PRIOR_PROMISE = "在的，刚才超时了，我重新来。"
_TERMINAL_COMPLETE_ANSWER = "已从断点继续：read_file 的结果已核对，报告已写进 notes.md。"
_READ_ARGS = {"path": "notes.md"}
_SECOND_READ_ARGS = {"path": "other.md"}
# P / Q 用不同的供应商用量，用来证明交付对象的 usage 归属哪一轮（跨枪混配就会红）。
_PRIOR_USAGE = {"output_tokens": 3, "input_tokens": 11}
_TERMINAL_USAGE = {"output_tokens": 7, "input_tokens": 23}

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


# LLM: 假 agent 只需真实循环/生成层真正读取的字段：backend、config.request_timeout、
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


# LLM: 每个用例一个新的 params 实例（live_archive_state 是本 run 的结构化事实容器，探针登记与
#   交付账本都挂在它上面，不能被测试之间串用）。executed_tools 由调用方显式决定：它是探针资格
#   的第二个必要条件，不允许靠共享夹具默默带过。
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


# LLM: 只记录真正发给模型的出站文本；测试不复制宿主 prompt 模板，只断言探针指令出现在哪几枪。
# 类用途: 记录每次 prompt 组装结果，供"哪一枪带了宿主指令"这类结构化断言。
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


# LLM: 脚本后端按序号回放「工具调用 / 挂起超时 / 带用量的正文」。每次进入一次物理采样前先拍一次
#   工具账本指纹（证明"探针那一枪进入时账本零增量"）；每次采样的出站 prompt 与用量都留痕，
#   供"prompt/response/usage 归属同一轮"的结构化比对。
# 类用途: 模拟可按剧本复现供应商时序的假后端。
class _ScriptedBackend:
    __test__ = False

    name = "scripted-delivery-backend"

    def __init__(self, script: list[object], fingerprint=None) -> None:
        self.script = list(script)
        self.calls = 0
        self.prompts: list[str] = []
        self.usages: list[dict] = []
        self.entries: list[dict[str, object]] = []
        self._fingerprint = fingerprint

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self._fingerprint is not None:
            self.entries.append(self._fingerprint())
        step = self.script[min(self.calls, len(self.script)) - 1]
        if step == "TIMEOUT":
            self.usages.append({})
            time.sleep(0.5)  # > request_timeout -> 墙钟超时
            return ModelResponse(text="", backend=self.name)
        if isinstance(step, tuple) and step[0] == "tool":
            _, name, arguments = step
            self.usages.append({})
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[
                    {"id": f"call-{self.calls}", "name": name, "input": dict(arguments)}
                ],
            )
        if isinstance(step, tuple) and step[0] == "answer":
            _, text, usage = step
            self.usages.append(dict(usage))
            return ModelResponse(text=str(text), backend=self.name, usage=dict(usage))
        self.usages.append({})
        return ModelResponse(text=str(step), backend=self.name)


# LLM: 计数 read_file handler：handler 执行次数是「工具副作用是否被重放/是否真的执行」的最终
#   判据，它不依赖被测产品的记账代码，避免自证。
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


# LLM: 工具账本指纹只取叶子事实（已执行工具名、IR 里的 ToolResult call_id），不对活对象做
#   序列化/递归枚举（仓库铁律：活数据不序列化）。
# 函数用途: 拍一份「工具是否被再次执行」的最小结构化指纹。
def _ledger_fingerprint(params: object) -> dict[str, object]:
    history = list(getattr(params, "tool_ir_history", None) or [])
    results = [item.call_id for item in history if isinstance(item, ToolResult)]
    return {
        "executed_tools": list(getattr(params, "executed_tools", None) or []),
        "result_ids": results,
    }


# LLM: 交付账本是"这次交付由哪些段落合成"的唯一结构化记录（段落顺序 + 每段来源轮次 + 是否被
#   投影进正文 + 结构化排除原因）；测试只读这份记录与 delivered 正文，不去解析正文语义。
# 函数用途: 读当前 params 上的交付账本（没有则 None）。
def _delivery_ledger(params: object) -> dict | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    entry = state.get("_assistant_delivery_segments")
    return entry if isinstance(entry, dict) else None


# LLM: 一次真实工具轮循环的全部可观测事实（含主循环返回的出站 prompt，用于归属比对）。
# 类用途: 承载真实 _execute_tool_loop_service 跑完后的交付/账本/时序证据。
@dataclass
class _LoopRun:
    backend: _ScriptedBackend
    tool: _CountingReadTool
    params: ToolLoopExecuteParams
    prompt: str
    response: object
    rounds: int
    tool_round_calls: list[int]
    executed_one_calls: int
    agent: _FakeAgent


# LLM: 真实循环 + 真实工具执行 + 真实产品记账路径都保留，只把"哪个工具 handler"换成计数只读
#   工具（canonical ToolExecutor 真跑），因此工具副作用次数不是桩件自证。
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

    prompt, response, rounds = _execute_tool_loop_service(service, params)
    return _LoopRun(
        backend=backend,
        tool=tool,
        params=params,
        prompt=prompt,
        response=response,
        rounds=rounds,
        tool_round_calls=list(counts["rounds"]),
        executed_one_calls=int(counts["exec_one"]),
        agent=agent,
    )


def _joined(*texts: str) -> str:
    return "\n\n".join(texts)


# ---------------------------------------------------------------- (a) 长错误总结 + 短正确结论


def test_long_wrong_prior_and_short_correct_terminal_are_both_delivered(
    monkeypatch, tmp_path
) -> None:
    """(a) P 长且是错误总结、Q 短且是正确结论 -> 两条都保留（顺序、来源正确），不存在择一丢弃。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("answer", _PRIOR_LONG_WRONG_SUMMARY, _PRIOR_USAGE),
            ("answer", _TERMINAL_SHORT_CORRECT_CONCLUSION, _TERMINAL_USAGE),
        ],
        run_id="run-delivery-shape-a",
    )

    assert len(_PRIOR_LONG_WRONG_SUMMARY) > len(_TERMINAL_SHORT_CORRECT_CONCLUSION), (
        "前提：旧的错误总结明显更长——旧实现的长度 tie-break 正是会因此丢掉短结论"
    )
    assert run.backend.calls == 4, "工具 + 超时 + 重试(P) + 探针(Q)"
    # 交付正文 = 两段按到达顺序的有序投影：先 P（更早产生）后 Q（本轮），逐字不改。
    assert run.response.text == _joined(_PRIOR_LONG_WRONG_SUMMARY, _TERMINAL_SHORT_CORRECT_CONCLUSION)
    assert _PRIOR_LONG_WRONG_SUMMARY in run.response.text, "长的那一段不得被短的那段压掉"
    assert _TERMINAL_SHORT_CORRECT_CONCLUSION in run.response.text, "短的新结论同样不得被丢弃"

    ledger = _delivery_ledger(run.params)
    assert ledger is not None, "合成交付必须留下结构化段落账本"
    rows = ledger["segments"]
    assert [row["origin"] for row in rows] == [
        "provider_timeout_probe_prior",
        "terminal_response",
    ]
    assert [row["order"] for row in rows] == [0, 1]
    assert [row["delivered"] for row in rows] == [True, True], "两段都被投影进终答正文"
    assert [row["text"] for row in rows] == [
        _PRIOR_LONG_WRONG_SUMMARY,
        _TERMINAL_SHORT_CORRECT_CONCLUSION,
    ], "账本里的段落文本与交付正文逐字一致（没有被改写）"
    assert rows[0]["sequence"] < rows[1]["sequence"], "段落顺序必须是「更早产生」在前"
    assert rows[0]["turn_id"] != rows[1]["turn_id"], "两段来自不同的 request/turn，来源必须分别保留"


# ---------------------------------------------------------------- (b) 承诺 + 完整答复


def test_prior_promise_and_terminal_complete_answer_are_both_delivered(
    monkeypatch, tmp_path
) -> None:
    """(b) P 承诺、Q 完整答复 -> 两条都保留（旧实现在这里只交付 Q，承诺被丢掉）。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("answer", _PRIOR_PROMISE, _PRIOR_USAGE),
            ("answer", _TERMINAL_COMPLETE_ANSWER, _TERMINAL_USAGE),
        ],
        run_id="run-delivery-shape-b",
    )

    assert run.backend.calls == 4
    assert run.response.text == _joined(_PRIOR_PROMISE, _TERMINAL_COMPLETE_ANSWER)
    assert _PRIOR_PROMISE in run.response.text
    assert _TERMINAL_COMPLETE_ANSWER in run.response.text
    rows = _delivery_ledger(run.params)["segments"]
    assert [row["delivered"] for row in rows] == [True, True]
    assert run.response.runtime_status == "ok", "不得伪造终态"
    assert run.tool_round_calls == [1], "交付裁决绝不进入工具执行路径"
    assert run.executed_one_calls == 1 and run.tool.handler_calls == 1, "工具副作用恰好一次"


# ---------------------------------------------------------------- (c) 探针带工具调用


def test_probe_with_tool_calls_executes_tools_and_keeps_shots_separate(
    monkeypatch, tmp_path
) -> None:
    """(c) 探针那一枪带工具调用 -> 工具照旧真执行；两枪语义不串（不合成、不重放、不吞指令）。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("answer", _PRIOR_PROMISE, _PRIOR_USAGE),
            ("tool", "read_file", _SECOND_READ_ARGS),
            ("answer", _TERMINAL_COMPLETE_ANSWER, _TERMINAL_USAGE),
        ],
        run_id="run-delivery-tools",
    )

    assert run.backend.calls == 5, "工具 + 超时 + 重试 + 探针(带工具) + 收口"
    assert run.tool_round_calls == [1, 2], "探针那一枪的工具调用照既有路径执行"
    assert run.executed_one_calls == 2 and run.tool.handler_calls == 2, "两次真实工具副作用，零重放"
    assert len(run.params.executed_tools) == 2
    # 真实工作继续时终答只由收口那一枪给出：承诺不参与交付投影（既有契约，语义不串）。
    assert run.response.text == _TERMINAL_COMPLETE_ANSWER
    assert _PRIOR_PROMISE not in run.response.text
    assert _delivery_ledger(run.params) is None, "工具继续的分支不产生合成交付账本"

    with_instruction = [
        index
        for index, prompt in enumerate(run.backend.prompts, start=1)
        if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4, 5], "探针指令只回灌一次，随后留在上下文里"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1


# ---------------------------------------------------------------- (d) 归属同一 request/turn


def test_delivered_prompt_response_and_usage_share_one_request_source(
    monkeypatch, tmp_path
) -> None:
    """(d) prompt/response/usage 三者来源一致：交付对象取自终答那一轮，跨枪不再混配。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("answer", _PRIOR_LONG_WRONG_SUMMARY, _PRIOR_USAGE),
            ("answer", _TERMINAL_SHORT_CORRECT_CONCLUSION, _TERMINAL_USAGE),
        ],
        run_id="run-delivery-attribution",
    )

    # 主循环返回的 prompt 就是产出交付对象那一次 request 的出站 prompt（第 4 枪 = 探针那一枪）。
    assert run.backend.calls == 4
    assert run.prompt == run.backend.prompts[3], "final_prompt 必须是终答那一轮的出站 prompt"
    assert _PROVIDER_TIMEOUT_RESUME in run.prompt, "那正是带宿主探针指令的一枪"
    assert _PROVIDER_TIMEOUT_RESUME not in run.backend.prompts[2], "P 那一枪不带该指令"

    # usage 归属：交付对象的用量取自终答那一轮，而不是上一枪（跨枪混配会在这里红）。
    assert run.response.usage == _TERMINAL_USAGE
    assert run.response.usage != _PRIOR_USAGE

    # 交付账本把"哪一段是谁产生的"写成结构化事实：owner 是终答轮，前导段是更早那一轮。
    ledger = _delivery_ledger(run.params)
    terminal_turn_id = str(run.params.live_archive_state.get("_current_model_turn_id") or "")
    assert ledger["owner_turn_id"] == terminal_turn_id, "交付对象归属 = 终答轮（与 prompt/usage 同源）"
    rows = ledger["segments"]
    assert rows[1]["turn_id"] == terminal_turn_id
    assert rows[0]["turn_id"] != terminal_turn_id and rows[0]["turn_id"], "前导段保留自己的来源轮次"
    assert rows[0]["usage"] == _PRIOR_USAGE, "前导段的用量记在它自己的来源上，不被抹掉也不冒充本轮"
    assert rows[1]["usage"] == _TERMINAL_USAGE


def test_prior_segment_is_kept_across_a_truncated_probe_shot(monkeypatch, tmp_path) -> None:
    """附：探针那一枪被输出上限截断时，前导段不许被中间态吞掉（截断续跑后仍交付两段）。"""
    from agent_py_agent.agent.agent_core import _tool_loop_service as tls
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        ToolLoopService,
        _execute_tool_loop_service,
    )

    params = _params(run_id="run-delivery-truncated-probe")
    tool = _CountingReadTool()
    backend = _ScriptedBackend(
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            ("answer", _PRIOR_PROMISE, _PRIOR_USAGE),
            ModelResponse(text="", backend="x"),
            ("answer", _TERMINAL_COMPLETE_ANSWER, _TERMINAL_USAGE),
        ],
        fingerprint=lambda: _ledger_fingerprint(params),
    )
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    service = ToolLoopService(agent)
    real_run_tool_round = tls._run_tool_round

    def counting_tool_round(request):
        return real_run_tool_round(agent, request)

    def counting_execute_one(request):
        return execute_canonical_test_call(
            tmp_path,
            tools={tool.model_spec.name: tool},
            tool_name=request.call.tool_name,
            arguments=dict(request.call.arguments),
            call_id=request.call.call_id,
            run_id="run-delivery-truncated-probe",
        )

    monkeypatch.setattr(service, "_run_tool_round", counting_tool_round)
    monkeypatch.setattr(service, "_execute_one_tool_call", counting_execute_one)
    monkeypatch.setattr(
        tls,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: tls._render_tool_loop_prompt(agent, loop_params),
    )
    # 第 4 枪 = 探针那一枪，且被输出上限截断 -> 走既有截断续跑（前导段按原来源续挂）。
    original_generate = tls.generate_model_response

    def truncating_generate(request: ModelGenerateParams):
        response = original_generate(request)
        if int(backend.calls) == 4:
            from dataclasses import replace as _replace

            return _replace(response, text="被输出上限截断的半句", truncated=True)
        return response

    monkeypatch.setattr(tls, "generate_model_response", truncating_generate)

    prompt, response, _rounds = _execute_tool_loop_service(service, params)

    assert backend.calls == 5, "截断那一枪之后按既有截断续跑预算再走一轮"
    assert response.text == _joined(_PRIOR_PROMISE, _TERMINAL_COMPLETE_ANSWER), (
        "前导段必须跟着截断续跑一起走到真正的终答，不能被中间态丢掉"
    )
    rows = _delivery_ledger(params)["segments"]
    assert rows[0]["text"] == _PRIOR_PROMISE and rows[0]["delivered"] is True
    assert _PROVIDER_TIMEOUT_RESUME in prompt


def test_truncated_terminal_is_excluded_but_prior_segment_is_still_delivered(tmp_path) -> None:
    """附：终答被截断且截断续跑预算用尽 -> 截断段不投影，但前导段照旧交付，typed 事实不伪造。"""
    params = _params(run_id="run-delivery-truncated-terminal", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _PRIOR_PROMISE, _TERMINAL_COMPLETE_ANSWER])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    decide = SimpleNamespace(config=SimpleNamespace(enable_tools=True, max_tool_rounds=0))

    # 第 1 枪：超时被重试救回，正文是承诺 -> 放行探针并寄存该段。
    prior = generate_model_response(
        ModelGenerateParams(agent=agent, params=params, prompt="p", tool_rounds=1)
    )
    first = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=decide, params=params, response=prior, counters=ToolLoopRepairCounters()
        )
    )
    assert first.action == "continue" and first.counters.provider_timeout_resume_repairs == 1

    # 第 2 枪：真实 model turn（序号紧邻），但这一枪被供应商输出上限截断。
    from dataclasses import replace

    probe = generate_model_response(
        ModelGenerateParams(agent=agent, params=params, prompt="p", tool_rounds=1)
    )
    terminal = replace(probe, text="被输出上限截断的半句", truncated=True)
    second = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=decide,
            params=params,
            response=terminal,
            counters=replace(first.counters, truncated_output_repairs=2),
        )
    )

    assert second.action == "break"
    assert second.response.runtime_status == "unfinished", "截断终态照旧收口，不伪造 ok"
    assert second.response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert second.response.text == _PRIOR_PROMISE, "被截断的那段不投影；合法的前导段仍然交付"
    rows = _delivery_ledger(params)["segments"]
    assert [row["delivered"] for row in rows] == [True, False]
    assert rows[1]["excluded_reason"] == "output_limit_truncated", "排除原因是结构化字段，不是语义判断"
    assert rows[1]["text"] == "被输出上限截断的半句", "被排除的段照旧留在账本里，不被隐藏"
    assert second.response.usage != _PRIOR_USAGE, "交付对象仍是本轮响应（usage 归属本轮）"


# ---------------------------------------------------------------- (e) 语义不变的轮


def test_zero_tool_round_and_exhausted_budget_semantics_unchanged(monkeypatch, tmp_path) -> None:
    """(e) 零工具执行轮不买探针；预算用尽轮不再续跑：两处结构语义与旧契约逐条一致。"""
    zero_work = _drive_loop(
        monkeypatch,
        tmp_path,
        ["TIMEOUT", _TERMINAL_COMPLETE_ANSWER, _PRIOR_PROMISE],
        run_id="run-delivery-no-work",
    )
    assert zero_work.backend.calls == 2, "零工具执行轮不多买一枪"
    assert zero_work.response.text == _TERMINAL_COMPLETE_ANSWER, "那一枪就是完整终答，不得改写"
    assert _PRIOR_PROMISE not in zero_work.response.text
    assert zero_work.params.tool_context == []
    assert _delivery_ledger(zero_work.params) is None, "没有前导段就没有合成交付账本"

    # 同一轮第二次「超时 -> 重试」时探针预算已用尽：不再多买枪，指令只回灌一次。
    # 注意这一格里交付仍是那一枪自己的正文：探针登记按既有 fail-closed「紧邻」规则在中间那次
    # 超时物理尝试上过期（登记只活一个 model turn），因此**从来没有出现过两条同时可交付的候选**
    # —— 这不是被删掉的长度启发式，本变更不改这条相邻性规则。
    exhausted = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            _PRIOR_PROMISE,
            "TIMEOUT",
            _TERMINAL_COMPLETE_ANSWER,
        ],
        run_id="run-delivery-budget",
    )
    assert exhausted.backend.calls == 5, "第 5 枪之后没有第 6 枪：探针预算 LIMIT=1 未放宽"
    assert exhausted.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert exhausted.response.text == _TERMINAL_COMPLETE_ANSWER, "登记已过期 -> 按原语义交付原响应"
    assert _delivery_ledger(exhausted.params) is None, "没有同时在手的两段就没有合成交付"

    # 没有在手前导段时，预算用尽仍按原语义交付那一枪自己的正文（对象同一、正文不改写）。
    plain = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            _PRIOR_PROMISE,
            ("tool", "read_file", _SECOND_READ_ARGS),
            "TIMEOUT",
            _TERMINAL_COMPLETE_ANSWER,
        ],
        run_id="run-delivery-budget-plain",
    )
    assert plain.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert plain.response.text == _TERMINAL_COMPLETE_ANSWER, "无前导段时不做任何合成"
    assert _delivery_ledger(plain.params) is None


# ---------------------------------------------------------------- (d2) 延迟工具收口的归属


def test_deferred_tool_limit_closeout_pairs_its_own_prompt(monkeypatch, tmp_path) -> None:
    """(d2) 延迟工具调用收口到工具轮上限时，返回的 prompt 必须是**产出该回复的那一枪**的 prompt。

    旧实现里这一枪的 prompt 被 ``del final_prompt`` 丢掉，主循环于是把上一轮的 prompt（这里根本
    还没有任何模型轮，所以是空串）与这一轮的回复配对：调用方拿到的 ``prompt`` 与
    ``prompt_token_estimate`` 归属错误。本用例是归属面（非裁决面）的回归钉。
    """
    from agent_py_agent.agent.agent_core import _tool_loop_service as tls
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        ToolLoopService,
        _execute_tool_loop_service,
    )

    params = _params(run_id="run-delivery-deferred")
    # 触发「延迟工具调用 + 工具轮已到上限」这条 drain 分支：必须走真实收口生成，而不是队列回执。
    object.__setattr__(params, "context_scope", "task_local")
    params.live_archive_state["pending_deferred_tool_calls"] = [
        {"tool": "read_file", "call_id": "call-9", "arguments": dict(_READ_ARGS)}
    ]
    backend = _ScriptedBackend([("answer", "工具轮上限收口", _TERMINAL_USAGE)])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    object.__setattr__(agent.config, "max_tool_rounds", 1)
    object.__setattr__(params, "tool_rounds", 1)

    monkeypatch.setattr(
        tls,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: tls._render_tool_loop_prompt(agent, loop_params),
    )

    prompt, response, _rounds = _execute_tool_loop_service(ToolLoopService(agent), params)

    assert response is not None and response.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
    assert backend.calls == 1, "收口那一枪是真实模型生成"
    assert prompt == backend.prompts[0], "返回的 prompt 必须就是产出这条回复的那次出站 prompt"
    assert prompt.strip(), "不得再返回空/上一轮的 prompt"
    assert "最大工具轮数" in prompt, "那一次 request 带的正是宿主的上限收口指令"
    assert response.usage == _TERMINAL_USAGE, "usage 与 prompt/response 同源"
