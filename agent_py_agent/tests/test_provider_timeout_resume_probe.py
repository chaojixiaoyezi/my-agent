"""LLM: 门槛5 续跑探针合同单测（R1 残留边界）——续跑那一枪只作「还有没有真实工具工作」的探针。

背景（验收监督指出的 R1 残留边界）：收窄后只要求「本 turn 超时被重试救回 + 本 run 工具账本
``executed_tools`` 非空」。但 ``executed_tools`` 非空只证明**做过工具**，不能证明**任务没做完**：
「调用工具 → 超时安全重试 → 重试那一枪给出完整终答」的形状里，宿主仍会多买一枪，并把最终交付
文本换成那一枪（可能只说「已完成，无需继续。」）——用户的完整终答被顶掉。

规则（与既有策略的边界）：
- 探针判定不变：①本 model turn 被生成层登记为「发生过被重试救回的供应商超时」、②本 run 工具账本
  ``executed_tools`` 非空、③这一枪零工具调用。三条都是结构化事实，门槛5 的四个 fail-closed 重试
  闸门一行未动。
- 探针那一枪**零工具调用** = 没有任何工具工作要继续 -> 走**无损交付 tie-break**：在
  {P = 探针之前那一枪, Q = 探针那一枪} 之间原样交付**正文更长的那一条**，另一条不交付；两条都
  是原对象、逐字不改、``runtime_status`` 不伪造。两种真实形状（P 完整终答 / Q 承诺，与
  P 承诺 / Q 完整终答）在结构化层面不可区分，只有正文长度能作无损代理（见
  ``response_decision._provider_timeout_probe_final_response`` 的已知局限注释）。
- 探针那一枪**带工具调用** -> 登记在入口被消费掉，工具按既有工具轮路径真的执行，工作继续；
  tie-break 不介入。
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
# 重试那一枪给出的完整终答（长）：R1 残留边界里被探针正文顶掉、用户再也看不到的那句话。
_COMPLETE_ANSWER = "notes.md 里的配置项已经核对过：enable_tools 默认开启，剩余工作全部做完。"
# 探针那一枪的短正文：结构化上「零工具调用」，长度上明显短于完整终答 -> 交付 P。
_PROBE_STUB = "已完成。"
# 只承诺不动作的正文：形状二的 P（此时探针那一枪才是完整答复 -> 交付 Q）。
_PROMISE = "在的，刚才超时了，我重新来。"
# 形状二的 Q：用文本把剩余活干完（长于 _PROMISE）。
_CONTINUATION_ANSWER = "已从断点继续：read_file 的结果已核对，报告已写进 notes.md。"
# 旧契约剧本用的等长正文（tie 默认 = 探针那一枪）。
_FINAL = "已从断点继续并完成剩余工作。"
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


# LLM: 真实循环 + 真实工具执行 + 真实产品记账路径都保留，只把「哪个工具 handler」换成计数只读
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


# ------------------------------------------- (a) 探针零工具调用 -> 交付重试那一枪完整终答


def test_probe_zero_tool_calls_delivers_the_retry_shot_answer_verbatim(
    monkeypatch, tmp_path
) -> None:
    """(a) 工具 → 超时 → 重试给出完整终答 → 探针零工具调用 -> 交付那一枪原文、零额外副作用。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB],
        run_id="run-probe-keeps-answer",
    )

    assert run.backend.calls == 4, "工具采样 + 超时 + 重试 + 探针：探针仍然发生（只多买一枪）"
    assert run.response.text == _COMPLETE_ANSWER, "探针正文不得顶掉重试那一枪的完整终答"
    assert _PROBE_STUB not in run.response.text, "另一条候选不参与交付"
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
    """(e) 把探针正文换成任意其它文本（含英文、空承诺、乱码），交付结果完全一致。"""
    for index, probe_text in enumerate(_PROBE_TEXT_VARIANTS):
        run = _drive_loop(
            monkeypatch,
            tmp_path,
            [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, probe_text],
            run_id=f"run-probe-text-{index}",
        )

        assert run.backend.calls == 4, probe_text
        assert run.response.text == _COMPLETE_ANSWER, probe_text
        assert probe_text not in run.response.text, probe_text
        assert run.tool_round_calls == [1], probe_text
        assert run.params.tool_context.count(_PROVIDER_TIMEOUT_RESUME) == 1, probe_text


def test_probe_with_blank_text_still_delivers_the_earlier_answer(monkeypatch, tmp_path) -> None:
    """(e2) 探针正文为空(仅空白)时同样交付探针之前那一枪：不进入 empty-text nudge 预算。"""
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


# ------------------------------------------- (f)(g) 无损交付 tie-break 的两种形状


def test_probe_zero_tool_calls_prefers_the_longer_complete_answer(monkeypatch, tmp_path) -> None:
    """(f) P=完整终答(长) + Q=承诺(短) -> 交付 P，Q 的正文不出现在交付里。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _COMPLETE_ANSWER, _PROBE_STUB],
        run_id="run-probe-shape-complete",
    )

    assert len(_COMPLETE_ANSWER) > len(_PROBE_STUB), "前提：完整终答更长"
    assert run.response.text == _COMPLETE_ANSWER
    assert _PROBE_STUB not in run.response.text


def test_probe_zero_tool_calls_prefers_the_longer_continuation_answer(
    monkeypatch, tmp_path
) -> None:
    """(g) P=只承诺(短) + Q=用文本把活干完(长) -> 交付 Q，承诺正文不出现在交付里。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _CONTINUATION_ANSWER],
        run_id="run-probe-shape-continuation",
    )

    assert len(_CONTINUATION_ANSWER) > len(_PROMISE), "前提：探针那一枪的答复更长"
    assert run.response.text == _CONTINUATION_ANSWER, "无损 tie-break 必须命中真正的完整答复"
    assert _PROMISE not in run.response.text


def test_probe_tie_prefers_the_probe_sample_and_keeps_old_contract(monkeypatch, tmp_path) -> None:
    """(h) 两条候选等长 = 信息平局 -> 保留既有契约（交付更晚、上下文更多的探针那一枪）。"""
    run = _drive_loop(
        monkeypatch,
        tmp_path,
        [("tool", "read_file", _READ_ARGS), "TIMEOUT", _PROMISE, _FINAL],
        run_id="run-probe-tie",
    )

    assert len(_PROMISE) == len(_FINAL), "前提：本剧本等长"
    assert run.response.text == _FINAL
    assert run.backend.calls == 4


# ------------------------------------------- (i) 对象同一 + 登记生命周期


def test_probe_delivers_the_identical_response_object(tmp_path) -> None:
    """(i) 交付的是**同一个响应对象**（不复制、不改写），登记消费一次、计数不重复 +1。"""
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

    probe = _generate(agent, params)  # 探针那一枪（Q）：真实 model turn，序号紧邻
    assert probe.text == _PROBE_STUB and probe is not retried
    second = _decide(params, probe, first.counters)

    assert second.action == "break"
    assert second.response is retried, "原样交付同一个对象，不复制、不拼接、不改写"
    assert second.response.text == _COMPLETE_ANSWER
    assert second.counters.provider_timeout_resume_repairs == 1, "探针不再 +1（LIMIT=1 不变）"
    assert second.counters == first.counters, "交付选择不得改动任何修复计数"
    assert _probe_entry(params) is None, "登记消费一次即清除"


def test_probe_with_protected_marker_consumes_the_rollback_without_delivering_it(tmp_path) -> None:
    """(j) 探针那一枪带受保护工具标记 -> 走既有修复路径，只消费登记、不回退交付。"""
    params = _params(run_id="run-probe-marker", executed_tools=["read_file"])
    backend = _ScriptedBackend(["TIMEOUT", _COMPLETE_ANSWER])
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)

    retried = _generate(agent, params)
    first = _decide(params, retried, ToolLoopRepairCounters())
    assert first.action == "continue"
    assert _probe_entry(params) is not None

    # 探针那一枪带受保护标记：结构化判定为「需要修复」，不是普通终答。
    probe = ModelResponse(text=f"[tool-record] {_PROBE_STUB}", backend=backend.name)
    second = _decide(params, probe, first.counters)

    assert second.action == "continue", "受保护标记仍走既有修复路径"
    assert second.counters.protected_marker_repairs == 1
    assert second.counters.provider_timeout_resume_repairs == 1, "探针不重复计数"
    assert _probe_entry(params) is None, "非零工具调用分支只消费登记，不回退交付"


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

    # 对照组：紧邻时取回的必须是同一个对象。
    retried_again = _generate(agent, params)  # 序号 5
    assert arm_provider_timeout_resume_probe(params, retried_again) is True
    _generate(agent, params)  # 序号 6 = 紧邻的探针那一枪
    assert take_provider_timeout_resume_probe(params) is retried_again
    assert _probe_entry(params) is None
