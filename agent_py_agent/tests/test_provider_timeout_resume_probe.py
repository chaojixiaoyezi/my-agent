"""LLM: 门槛5 续跑探针合同单测（R1 残留边界）——续跑那一枪只作「还有没有真实工具工作」的探针。

背景（验收监督指出的 R1 残留边界）：收窄后只要求「本 turn 超时被重试救回 + 本 run 工具账本
``executed_tools`` 非空」。但 ``executed_tools`` 非空只证明**做过工具**，不能证明**任务没做完**：
「调用工具 → 超时安全重试 → 重试那一枪给出完整终答」的形状里，宿主仍会多买一枪，并把最终交付
文本换成那一枪（可能只说「已完成，无需继续。」）——用户的完整终答被顶掉。

规则（与既有策略的边界）：
- 探针判定不变：①本 model turn 被生成层登记为「发生过被重试救回的供应商超时」、②本 run 工具账本
  ``executed_tools`` 非空、③这一枪零工具调用。三条都是结构化事实，门槛5 的四个 fail-closed 重试
  闸门一行未动。
- 探针那一枪**零工具调用** = 没有任何工具工作要继续 -> 走**无损交付**：{P = 探针之前那一枪,
  Q = 探针那一枪} 两条已产生的合法答复**按到达顺序合成为有序段落**（逐字投影，只加段落边界），
  任何一段都不因正文长短/内容被丢弃，宿主也不读正文做语义判定。只有该段自己的结构字段能把它
  排除出终答正文：该段被供应商输出上限截断（本就不属于合法终答）、该段正文为空（没有字符可投影）；
  被排除的段仍以结构化原因记进交付账本（``live_archive_state["_assistant_delivery_segments"]``）。
  交付对象只从**当前轮**响应派生，因此主循环返回的 prompt/response/usage 来源同一。
- 探针那一枪**带工具调用** -> 登记在入口被消费掉，工具按既有工具轮路径真的执行，工作继续；
  前导段不参与终答投影（真实工作继续时终答由收口那一枪给出）。
- 有界：``_PROVIDER_TIMEOUT_RESUME_LIMIT`` 仍为 1，与截断续跑、其它修复计数互不吃预算；
  探针预算用尽后按原语义交付原响应。
- 不重放工具：探针只追加一条宿主指令并 continue，``executed_tools`` 与 IR 工具结果都不得新增。

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
    provider_timeout_resume_eligible,
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
# 重试那一枪给出的完整终答（长）：在探针那一枪之前产生，必须作为前导段落原样保留。
_COMPLETE_ANSWER = "notes.md 里的配置项已经核对过：enable_tools 默认开启，剩余工作全部做完。"
# 探针那一枪的短正文：结构化上「零工具调用」，长度明显短于完整终答 -> 仍按序一并交付（长度不参与）。
_PROBE_STUB = "已完成。"
# 只承诺不动作的正文：形状二的 P（此时探针那一枪才把活干完）。
_PROMISE = "在的，刚才超时了，我重新来。"
# 形状二的 Q：用文本把剩余活干完（长于 _PROMISE）。
_CONTINUATION_ANSWER = "已从断点继续：read_file 的结果已核对，报告已写进 notes.md。"
# 等长剧本用的收口正文（长度相等同样不构成"择一"理由）。
_FINAL = "已从断点继续并完成剩余工作。"
# 交付段落的边界：会话运行时 用两个 assistant item 交付两段，本仓库单条正文用空行投影。
_SEGMENT_SEPARATOR = "\n\n"
_SECOND_PROMISE = "第二次：我又只回了一句承诺。"
_READ_ARGS = {"path": "notes.md"}
_SECOND_READ_ARGS = {"path": "other.md"}
# 探针正文的文案变体：长短都短于 _COMPLETE_ANSWER，行为必须完全一致（零文本匹配）。
_PROBE_TEXT_VARIANTS = (
    "已完成，无需继续。",
    "I will retry now.",
    "好的。",
    "……",
    "完全没有提到超时或重试的一句普通回答。",
)

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


# LLM: 每个用例一个新的 params 实例（live_archive_state 是本 run 的结构化事实容器，探针登记也
#   挂在它上面，不能被测试之间串用）。executed_tools 由调用方显式决定：它是探针资格的第二个
#   必要条件，不允许靠共享夹具默默带过。
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
# 类用途: 记录每次 prompt 组装结果，供「探针指令恰好出现一次」这类断言。
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


# LLM: 脚本后端按序号回放「工具调用 / 挂起超时 / 任意正文」；每次进入一次物理采样前先拍一次
#   工具账本指纹，用来证明「探针那一枪进入时账本零增量」。
# 类用途: 模拟可按剧本复现供应商时序的假后端。
class _ScriptedBackend:
    __test__ = False

    name = "scripted-probe-backend"

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
        "tool_result_ids": results,
    }


# LLM: 探针登记是本轮存活的结构化事实；测试只读它的存在性（证明「已登记 / 已消费」），
#   不去序列化里面寄存的响应对象。
# 函数用途: 读当前 params 上的探针登记（无则 None）。
def _probe_entry(params: object) -> dict | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    entry = state.get("_provider_timeout_resume_probe")
    return entry if isinstance(entry, dict) else None


# LLM: 交付账本是「这次交付由哪些段落按什么顺序合成」的唯一结构化记录；测试只读它，不解析正文语义。
# 函数用途: 读当前 params 上的交付段落账本（无则 None）。
def _delivery_ledger(params: object) -> dict | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    entry = state.get("_assistant_delivery_segments")
    return entry if isinstance(entry, dict) else None


# 函数用途: 按既有交付投影规则把两段拼成期望正文（只加边界，不改写任何一段）。
def _ordered_delivery(*texts: str) -> str:
    return _SEGMENT_SEPARATOR.join(texts)


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


# LLM: 真实循环 + 真实工具执行 + 真实产品记账路径都保留，只把「哪个工具 handler」换成计数只读
#   工具（canonical ToolExecutor 真跑），因此工具副作用次数不是桩件自证。
# 函数用途: 局部替换测试工具后运行真实循环，立即还原替换，再返回结构化证据。
def _drive_loop(monkeypatch, tmp_path: Path, script: list[object], *, run_id: str) -> _LoopRun:
    from agent_py_agent.agent.agent_core import _tool_loop_service as tls

    params = _params(run_id=run_id)
    tool = _CountingReadTool()
    backend = _ScriptedBackend(script, fingerprint=lambda: _ledger_fingerprint(params))
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    counts: dict[str, object] = {"rounds": [], "exec_one": 0}
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
        counts["exec_one"] = int(counts["exec_one"]) + 1
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
        tool_round_calls=list(counts["rounds"]),
        executed_one_calls=int(counts["exec_one"]),
        agent=agent,
    )


# LLM: 生成层的真实超时路径：第一枪挂起触发门槛5，之后按 fake 后端剧本应答。探针那一枪同样
#   走这条真实路径（它是宿主 continue 之后的一次真实 model turn，不是测试手写的响应）。
# 函数用途: 通过真实 generate_model_response 建立结构化超时事实并拿到那一枪的响应对象。
def _generate(agent: _FakeAgent, params: ToolLoopExecuteParams):
    return generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt="把剩下的工作做完",
            tool_rounds=1,
        )
    )


# LLM: 决策入口必须与真实循环用同一个（tool_loop_response_decision），否则「入口消费探针登记」
#   这条生命周期不变式不会被覆盖。
# 函数用途: 用真实决策入口裁决一条响应。
def _decide(params: ToolLoopExecuteParams, response: object, counters: ToolLoopRepairCounters):
    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=SimpleNamespace(config=SimpleNamespace(enable_tools=True, max_tool_rounds=0)),
            params=params,
            response=response,
            counters=counters,
        )
    )


# ------------------------------------------- (a) 探针零工具调用 -> 两枪按序无损交付


def test_probe_zero_tool_calls_delivers_the_retry_shot_answer_verbatim(
    monkeypatch, tmp_path
) -> None:
    """(a) 工具 → 超时 → 重试给出完整终答 → 探针零工具调用 -> 两段按到达顺序交付、零额外副作用。

    旧断言 ``run.response.text == _COMPLETE_ANSWER`` 本身是错的：它把"交付必须等于其中一条候选"
    当成了正确性，于是长度 tie-break 才能把另一条合法答复丢掉。新契约下交付正文是两段的有序投影。
    """
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB],
        run_id="run-probe-keeps-answer",
    )

    assert run.backend.calls == 4, "工具采样 + 超时 + 重试 + 探针：探针仍然发生（只多买一枪）"
    assert run.response.text == _ordered_delivery(_COMPLETE_ANSWER, _PROBE_STUB), (
        "两条合法答复都保留：先产生的在前，探针那一枪在后"
    )
    assert run.response.text.startswith(_COMPLETE_ANSWER), "前导段逐字在最前面，没有被改写或截断"
    ledger = _delivery_ledger(run.params)
    assert ledger is not None
    assert [row["text"] for row in ledger["segments"]] == [_COMPLETE_ANSWER, _PROBE_STUB]
    assert [row["delivered"] for row in ledger["segments"]] == [True, True]
    assert ledger["segments"][0]["sequence"] < ledger["segments"][1]["sequence"], "顺序 = 产生顺序"
    assert run.response.runtime_status == "ok", "不得伪造终态（runtime_status 原样）"
    assert run.tool_round_calls == [1], "探针绝不进入工具执行路径"
    assert run.executed_one_calls == 1 and run.tool.handler_calls == 1, "工具副作用恰好一次"
    assert run.params.executed_tools == ["read_file"], "探针不重放任何工具"
    assert _probe_entry(run.params) is None, "探针登记消费一次即清除"

    with_instruction = [
        index
        for index, prompt in enumerate(run.backend.prompts, start=1)
        if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4], "只有探针那一枪带宿主指令，重试那一枪不带"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert run.backend.entries[3] == run.backend.entries[2], "探针进入时账本与重试那一枪相同"
    records = run.agent._model_call_ledger.records()
    assert [(item.status, item.metadata["physical_attempt"]) for item in records] == [
        ("finished", 1),
        ("timed_out", 1),
        ("finished", 2),
        ("finished", 1),
    ], "探针是新的 logical 采样，不是同一 logical turn 的第三次物理尝试"
    assert provider_timeout_resume_eligible(run.params) is False, "事实不跨 turn 生效"


# ------------------------------------------- (b) 探针带工具调用 -> 工具真的执行、工作继续


def test_probe_with_tool_calls_executes_the_tools_and_keeps_working(monkeypatch, tmp_path) -> None:
    """(b) 工具 → 超时 → 重试只承诺 → 探针带工具调用 -> 工具真的执行，任务继续到收口。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            _PROMISE,
            ("tool", "read_file", _SECOND_READ_ARGS),
            _FINAL,
        ],
        run_id="run-probe-carries-tools",
    )

    assert run.backend.calls == 5, "工具 + 超时 + 重试 + 探针（带工具）+ 收口；没有第 6 枪"
    assert run.tool_round_calls == [1, 2], "探针那一枪的工具调用照既有路径执行"
    assert run.executed_one_calls == 2 and run.tool.handler_calls == 2, "两次真实工具副作用，零重放"
    assert len(run.params.executed_tools) == 2, "探针真的推进了工作（账本新增一次执行）"
    assert _probe_entry(run.params) is None, "带工具调用的探针同样消费掉登记，不回退交付"
    assert run.response.text == _FINAL, "探针之后模型正常收口，交付那一枪的正文"

    with_instruction = [
        index
        for index, prompt in enumerate(run.backend.prompts, start=1)
        if _PROVIDER_TIMEOUT_RESUME in prompt
    ]
    assert with_instruction == [4, 5], "探针指令只回灌一次，随后留在上下文里随后的出站请求"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert _PROVIDER_TIMEOUT_RESUME not in run.backend.prompts[2], "重试那一枪不带探针指令"


# ------------------------------------------- (c) 探针预算用尽 -> 不再续跑（原语义收口）


def test_probe_budget_exhausted_closes_with_original_semantics(monkeypatch, tmp_path) -> None:
    """(c) 同一轮两次「超时 → 重试 → 只承诺」也只放行一次探针，第二次按原语义交付原响应。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [
            ("tool", "read_file", _READ_ARGS),
            "TIMEOUT",
            _PROMISE,
            "TIMEOUT",
            _SECOND_PROMISE,
            _FINAL,
        ],
        run_id="run-probe-budget",
    )

    assert run.backend.calls == 5, "第 5 枪之后没有第 6 枪：探针预算 LIMIT=1 未放宽"
    assert run.response.text == _SECOND_PROMISE, "预算用尽后按原语义交付原响应"
    assert run.response.runtime_status == "ok", "不得伪造 unfinished/cancelled 终态"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1, "至多回灌一条探针指令"
    assert run.tool_round_calls == [1], "探针绝不进入工具执行路径"
    assert run.tool.handler_calls == 1
    assert _probe_entry(run.params) is None
    assert run.backend.entries[4] == run.backend.entries[1], "第二次重试进入时账本无增量"


# ------------------------------------------- (d) 零工具执行的轮不买探针（收窄语义不变）


def test_round_without_executed_tools_never_buys_a_probe(monkeypatch, tmp_path) -> None:
    """(d) 无工具执行 + 超时救回 + 完整终答 -> 不续跑：两枪收口、零探针指令、零登记。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        ["TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB],
        run_id="run-probe-no-work",
    )

    assert run.backend.calls == 2, "超时 + 重试；零工具执行的轮不再多买一枪"
    assert run.response.text == _COMPLETE_ANSWER
    assert _PROBE_STUB not in run.response.text
    assert run.params.executed_tools == [], "全程零工具执行 = 无未完成工作迹象"
    assert all(_PROVIDER_TIMEOUT_RESUME not in prompt for prompt in run.backend.prompts)
    assert run.params.tool_context == []
    assert _probe_entry(run.params) is None, "不具资格时不得留下任何探针登记"
    assert run.tool_round_calls == [] and run.tool.handler_calls == 0
    assert provider_timeout_resume_eligible(run.params) is False


# ------------------------------------------- (e) 判定与文案无关


def test_probe_delivery_is_text_independent(monkeypatch, tmp_path) -> None:
    """(e) 把探针正文换成任意其它文本（含英文、空承诺、乱码），交付结构完全一致。

    旧断言 ``run.response.text == _COMPLETE_ANSWER`` + ``probe_text not in ...`` 本身是错的：
    它要求"探针正文永不出现"，那正是"丢掉一条合法答复"的旧契约。
    """
    for index, probe_text in enumerate(_PROBE_TEXT_VARIANTS):
        run = _drive_loop(
            monkeypatch,
            tmp_path,
            [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, probe_text],
            run_id=f"run-probe-text-{index}",
        )

        assert run.backend.calls == 4, probe_text
        assert run.response.text == _ordered_delivery(_COMPLETE_ANSWER, probe_text), probe_text
        ledger = _delivery_ledger(run.params)
        assert [row["origin"] for row in ledger["segments"]] == [
            "provider_timeout_probe_prior",
            "terminal_response",
        ], probe_text
        assert [row["delivered"] for row in ledger["segments"]] == [True, True], probe_text
        assert run.tool_round_calls == [1], probe_text
        assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1, probe_text


def test_probe_with_blank_text_still_delivers_the_earlier_answer(monkeypatch, tmp_path) -> None:
    """(e2) 探针正文为空(仅空白)时交付探针之前那一枪：不进入 empty-text nudge 预算。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, "   "],
        run_id="run-probe-blank",
    )

    assert run.backend.calls == 4
    assert run.response.text == _COMPLETE_ANSWER, "空探针正文不得让用户收到空交付"
    assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1
    assert _probe_entry(run.params) is None
    ledger = _delivery_ledger(run.params)
    assert ledger["segments"][1]["delivered"] is False, "空段没有字符可投影"
    assert ledger["segments"][1]["excluded_reason"] == "empty_text", "排除原因是结构化字段"
    assert ledger["segments"][0]["delivered"] is True, "前导段照旧交付"


# ------------------------------------------- (f)(g)(h) 两种形状都无损、长度不参与

# 旧实现按 ``len(Q) >= len(P)`` 在这两种形状里各丢掉一条：形状一丢短结论、形状二丢承诺。
# 下面三条用例把"长度不参与交付"钉死：无论哪一段更长、或两段等长，交付正文与段落账本完全同构。


def test_probe_zero_tool_calls_keeps_the_longer_complete_answer_and_the_short_stub(
    monkeypatch, tmp_path
) -> None:
    """(f) P=完整终答(长) + Q=承诺(短) -> 两条都在：P 在前，Q 在后，逐字不改。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB],
        run_id="run-probe-shape-complete",
    )

    assert len(_COMPLETE_ANSWER) > len(_PROBE_STUB), "前提：完整终答更长（旧实现靠长度才会选它）"
    assert run.response.text == _ordered_delivery(_COMPLETE_ANSWER, _PROBE_STUB)
    assert run.response.text.index(_COMPLETE_ANSWER) < run.response.text.index(_PROBE_STUB)


def test_probe_zero_tool_calls_keeps_the_promise_and_the_longer_continuation_answer(
    monkeypatch, tmp_path
) -> None:
    """(g) P=只承诺(短) + Q=用文本把活干完(长) -> 两条都在：承诺在前，答复在后。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _CONTINUATION_ANSWER],
        run_id="run-probe-shape-continuation",
    )

    assert len(_CONTINUATION_ANSWER) > len(_PROMISE), "前提：探针那一枪的答复更长"
    assert run.response.text == _ordered_delivery(_PROMISE, _CONTINUATION_ANSWER)


def test_probe_delivery_does_not_depend_on_length_at_all(monkeypatch, tmp_path) -> None:
    """(h) 两段等长时同样不做"择一"：等长不再是一种需要默认值的平局。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-probe-tie",
    )

    assert len(_PROMISE) == len(_FINAL), "前提：本剧本等长"
    assert run.response.text == _ordered_delivery(_PROMISE, _FINAL)
    assert run.backend.calls == 4
    ledger = _delivery_ledger(run.params)
    assert [row["text"] for row in ledger["segments"]] == [_PROMISE, _FINAL], (
        "等长与否都不改变段落集合：交付决策里不存在长度比较"
    )


# ------------------------------------------- (i) 逐字保留 + 交付对象归属当前轮


def test_probe_delivery_keeps_segment_texts_verbatim_and_owns_the_terminal_response(
    tmp_path,
) -> None:
    """(i) 两段正文逐字保留、来源分别登记；交付对象取自**当前轮**（prompt/response/usage 同源）。

    旧断言 ``second.response is retried`` 本身是错的：它把"只交付 P 的同一对象"当成正确性，
    即"必须丢掉一条"。新契约下交付对象取自终答那一轮（对象同一或只换 text 的 replace），
    前导段以逐字文本 + 独立来源的形式保留，因此既没有丢弃，也没有跨枪混配。
    """
    params = _params(run_id="run-probe-identity", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    retried = _generate(agent, params)  # 超时 + 门槛5 重试那一枪（P）
    assert retried.text == _COMPLETE_ANSWER
    first = _decide(params, retried, ToolLoopRepairCounters())
    assert first.action == "continue", "放行一次探针"
    assert first.counters.provider_timeout_resume_repairs == 1
    entry = _probe_entry(params)
    assert entry is not None and entry["response"] is retried, "登记里寄存的就是那一枪的对象"
    assert entry["source"]["sequence"] == params.live_archive_state["_model_turn_sequence"], (
        "登记同时记下该段自己的来源轮次"
    )
    armed_turn_id = str(entry["source"]["turn_id"])

    probe = _generate(agent, params)  # 探针那一枪（Q）：真实 model turn，序号紧邻
    assert probe.text == _PROBE_STUB and probe is not retried
    second = _decide(params, probe, first.counters)

    assert second.action == "break"
    assert second.response is not retried, "交付对象不再冒充上一枪的对象"
    assert second.response.runtime_status == probe.runtime_status == "ok", "runtime_status 原样"
    assert second.response.backend == probe.backend
    assert second.response.text == _ordered_delivery(retried.text, probe.text), (
        "两段都逐字保留（只是按顺序投影，原文没有被改写）"
    )
    ledger = _delivery_ledger(params)
    assert [row["text"] for row in ledger["segments"]] == [retried.text, probe.text]
    assert ledger["segments"][0]["turn_id"] == armed_turn_id, "前导段来源 = 登记时记下的那一轮"
    assert ledger["owner_turn_id"] == params.live_archive_state["_current_model_turn_id"]
    assert second.counters.provider_timeout_resume_repairs == 1, "探针不再 +1（LIMIT=1 不变）"
    assert second.counters == first.counters, "交付选择不得改动任何修复计数"
    assert _probe_entry(params) is None, "登记消费一次即清除"


def test_probe_with_protected_marker_keeps_the_prior_segment_pending(tmp_path) -> None:
    """(j) 探针那一枪带受保护工具标记 -> 走既有修复路径；前导段不丢，按原来源续挂到下次交付。

    旧断言 ``_probe_entry(params) is None``（"中间态就把登记丢掉"）本身是错的：那一刻本轮并没有
    交付任何东西，丢掉登记等于丢掉一条已产生的合法答复。新契约只在**真的交付**、或探针那一枪
    走工具执行路径时终结登记。
    """
    params = _params(run_id="run-probe-marker", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER, f"[tool-record] {_PROBE_STUB}"])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    retried = _generate(agent, params)
    first = _decide(params, retried, ToolLoopRepairCounters())
    assert first.action == "continue"
    armed = _probe_entry(params)
    assert armed is not None
    armed_turn_id = str(armed["source"]["turn_id"])
    armed_sequence = int(armed["source"]["sequence"])

    # 探针那一枪（真实 model turn，序号紧邻）带受保护标记：结构化判定为「需要修复」，不是普通终答。
    probe = _generate(agent, params)
    assert probe.text == f"[tool-record] {_PROBE_STUB}"
    second = _decide(params, probe, first.counters)

    assert second.action == "continue", "受保护标记仍走既有修复路径"
    assert second.counters.protected_marker_repairs == 1
    assert second.counters.provider_timeout_resume_repairs == 1, "探针不重复计数"
    assert _delivery_ledger(params) is None, "本轮没有交付，不许产生交付账本"
    rearmed = _probe_entry(params)
    assert rearmed is not None and rearmed["response"] is retried, "前导段按原来源续挂，不被丢掉"
    assert rearmed["source"] == {"turn_id": armed_turn_id, "sequence": armed_sequence}, (
        "续挂必须保留它自己的来源轮次，不能改记到当前轮名下"
    )
    assert rearmed["armed_sequence"] == params.live_archive_state["_model_turn_sequence"], (
        "相邻性锚点跟到当前轮，下一枪的交付才算紧邻"
    )


def test_probe_with_lifecycle_status_consumes_the_rollback_without_delivering_it(tmp_path) -> None:
    """(k) 探针那一枪给出生命周期终态（如 context_overflow）-> 终态优先，不被回退候选顶掉。"""
    params = _params(run_id="run-probe-lifecycle", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    retried = _generate(agent, params)
    first = _decide(params, retried, ToolLoopRepairCounters())
    assert first.action == "continue" and _probe_entry(params) is not None

    probe = ModelResponse(
        text="",
        backend=backend.name,
        runtime_status="context_overflow",
        runtime_reason="CONTEXT_WINDOW_EXCEEDED",
        runtime_source="tool_loop",
    )
    second = _decide(params, probe, first.counters)

    assert second.action == "break"
    assert second.response is probe, "结构化生命周期终态原样交付，不改写、不替换"
    assert second.counters.provider_timeout_resume_repairs == 1
    assert _probe_entry(params) is None, "登记只消费不使用（生命周期分支不参与交付选择）"


def test_probe_registration_is_fail_closed_across_turns(tmp_path) -> None:
    """(l) 登记只在「紧邻的下一个 model turn」有效：中间插了别的模型轮 -> 丢弃，不交付陈旧答案。"""
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        arm_provider_timeout_resume_probe,
        take_provider_timeout_resume_probe,
    )

    params = _params(run_id="run-probe-stale", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB, _FINAL])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    retried = _generate(agent, params)  # 序号 2：被门槛5 重试救回的那一枪
    assert arm_provider_timeout_resume_probe(params, retried) is True
    entry = _probe_entry(params)
    assert entry is not None and entry["response"] is retried

    _generate(agent, params)  # 序号 3：中间插了一枪（本轮没走到裁决）
    later = _generate(agent, params)  # 序号 4：已经不再紧邻
    assert later is not retried
    assert take_provider_timeout_resume_probe(params) is None, "序号不紧邻不得交付陈旧答案"
    assert _probe_entry(params) is None, "陈旧登记必须被清除（消费一次）"

    # 对照组：紧邻时取回的是同一个对象，且带上它自己的来源轮次。
    # （访问器签名从"裸响应对象"改为"响应对象 + 来源"的结构化记录：旧断言本身没错，
    #   但交付需要来源才能把段落记到正确的轮次上，所以这里改读 record.response。）
    retried_again = _generate(agent, params)  # 序号 5
    assert arm_provider_timeout_resume_probe(params, retried_again) is True
    _generate(agent, params)  # 序号 6 = 紧邻的探针那一枪
    taken = take_provider_timeout_resume_probe(params)
    assert taken is not None and taken.response is retried_again
    assert taken.sequence == 5 and taken.turn_id, "来源轮次随段落一起流转"
    assert _probe_entry(params) is None
