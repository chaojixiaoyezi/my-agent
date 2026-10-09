"""LLM: 空正文续跑预算按工具进展重置（mc3-emptybudget）的合同测试。

背景（真机）：长回合（159/192 轮工具）最后一步回复为空，整轮判 USER_REPLY_UNAVAILABLE，交接丢失。
旧语义：空正文 nudge 预算是**整轮累计 2 次、不随工具进展重置**——长回合里前面偶尔空几次就把
预算用完，后期再空一次就直接结束回合。

规则（通用，不针对模型写分支）：
- 空正文裁决前，若自上次空正文以来至少有一次工具调用**真正执行完**（executed_tools 增长，
  它只在 result.ok 时追加），预算恢复为 0（连续无进展仍守原硬上限 2 次）；
- 判据只用结构化事实（计数、工具执行记录），不读正文或思考内容；
- "工具调用发起了但没有结果"（archive_tool_calls 增长、executed_tools 不增长）不算进展；
- 与截断降档预算各自独立、互不吞掉；子代理回合走同一条裁决路径。

全部用例走真实 execute_tool_loop + 真实工具执行与产品记账，只把工具 handler 换成
隔离执行器里的只读测试工具、把网络换成脚本化假后端；断言只读结构化事实。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import _EMPTY_TEXT_NUDGE
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
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


# ---------------------------------------------------------------- helpers


# LLM: 成功工具：handler 真返回 ok=True，走产品记账后 executed_tools 增长——"真实进展"的唯一事实源。
# 类用途: 注册进隔离执行器的只读计数工具（成功路径）。
class _OkTool(BaseTool):
    __test__ = False

    def __init__(self) -> None:
        self.handler_calls = 0
        self.model_spec = _READ_SPEC
        self.runtime_policy = make_test_runtime_policy("read_only", resource_parameters=("path",))

    def execute(self, params):
        self.handler_calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, f"read:{params['path']}")


# LLM: 失败工具：handler 真返回 ok=False——"发起了但没有成功结果"；executed_tools 不增长，
#   archive_tool_calls 照常增长（产品记账对每个调用都归档）。
# 类用途: 注册进隔离执行器的只读计数工具（失败路径）。
class _FailTool(BaseTool):
    __test__ = False

    def __init__(self) -> None:
        self.handler_calls = 0
        self.model_spec = _READ_SPEC
        self.runtime_policy = make_test_runtime_policy("read_only", resource_parameters=("path",))

    def execute(self, params):
        self.handler_calls += 1
        return ToolHandlerOutcome(self.model_spec.name, False, "boom", error_code="TEST_FAIL")


# LLM: 假后端按脚本回放「空正文+思考 / 只思考截断 / 工具调用 / 终答」，并记录每次真实收到的
#   推理档位；支持降档配置（effort 控制 + 声明支持 request_options）。
# 类用途: 模拟可按剧本复现的供应商，供端到端循环断言结构化事实。
class _ScriptedBackend:
    __test__ = False

    name = "empty-budget-scripted-backend"
    supports_provider_request_options = True
    reasoning_control = "effort"

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen_efforts: list[str] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        self.calls += 1
        options = kwargs.get("request_options")
        self.seen_efforts.append(str(getattr(options, "reasoning_effort", "") or ""))
        return self.script[min(self.calls, len(self.script)) - 1]


class _RecordingPrompts:
    __test__ = False

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, user_prompt, memories, **kwargs):
        tools = kwargs.get("tools")
        tool_context = list(getattr(tools, "tool_context", ()) or ()) if tools else []
        text = "\n".join(
            [str(user_prompt)]
            + [str(item) for item in (kwargs.get("inject") or [])]
            + [str(item) for item in tool_context]
        )
        self.built.append(text)
        return text


# LLM: 端到端循环用的最小宿主外壳：backend / config（工具、档位、降档开关、协议与输出上限）/
#   prompts / 子代理 run 标记；任何产品字段缺失都应显式暴露。
# 类用途: 让真工具循环跑在脚本化假后端与隔离工具执行器上。
class _LoopAgent(SimpleNamespace):
    __test__ = False

    def __init__(self, backend: object, prompts: _RecordingPrompts, *, root, effort: str = "max") -> None:
        super().__init__(
            backend=backend,
            config=SimpleNamespace(
                request_timeout=5.0,
                enable_tools=True,
                max_tool_rounds=0,
                model_reasoning_effort=effort,
                model_truncation_thinking_downgrade=True,
                model_backend="openai_compatible",
                max_tokens=65536,
            ),
            prompts=prompts,
            root=str(root),
            _current_subagent_run_id="",
        )


# LLM: 每次调用一个新的 params 实例（live_archive_state 是同一 run 的结构化事实容器，
#   不能被测试之间串用）；native 协议 + 真实工具运行快照是工具轮的真实形态。
# 函数用途: 构造一次工具模型轮所需的运行参数。
def _params(run_id: str, *, context_scope: str = "default") -> ToolLoopExecuteParams:
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
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=run_id, source_protocol="native"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((_READ_SPEC,), run_id=run_id),
        tool_ir_history=[],
        save=False,
        delivery_contract={},
        context_scope=context_scope,
    )


# LLM: 一次驱动所需的全部场景参数收成一个对象（size 门禁：模块级函数参数 ≤4）。
# 类用途: 承载剧本、工具、run 身份与 scope/档位选项。
@dataclass
class _Scenario:
    __test__ = False

    script: list
    tool: object
    run_id: str
    context_scope: str = "default"
    effort: str = "max"


# LLM: 真 execute_tool_loop 保留；只把工具 handler 换成隔离执行器里的测试工具、把 prompt 装配
#   换成产品渲染函数——工具账（executed/archive）走真实 _record_tool_call 记账，不是桩件自证。
# 函数用途: 在脚本化假后端上跑一次真实工具循环，返回结果、参数、后端与工具。
def _drive(monkeypatch, tmp_path, scenario: _Scenario):
    params = _params(scenario.run_id, context_scope=scenario.context_scope)
    backend = _ScriptedBackend(scenario.script)
    agent = _LoopAgent(backend, _RecordingPrompts(), root=tmp_path, effort=scenario.effort)

    # LLM: 只替换具体工具 handler，canonical ToolExecutor 仍实际执行；显式核对宿主防止串用。
    # 函数用途: 核对当前宿主后交给隔离测试执行器运行只读测试工具。
    def exec_one(loop_agent, request):
        assert loop_agent is agent
        return execute_canonical_test_call(
            tmp_path,
            tools={scenario.tool.model_spec.name: scenario.tool},
            tool_name=request.call.tool_name,
            arguments=dict(request.call.arguments),
            call_id=request.call.call_id,
            run_id=scenario.run_id,
        )

    monkeypatch.setattr(loop, "execute_one_tool_call", exec_one)
    monkeypatch.setattr(
        loop,
        "build_tool_loop_prompt",
        lambda _agent, loop_params: loop._render_tool_loop_prompt(agent, loop_params),
    )
    result = loop.execute_tool_loop(agent, params)
    return result, params, backend


# LLM: 剧本响应构造：空正文+思考（走空正文 nudge）、只思考截断（走截断续跑降档）、
#   工具调用（native tool_use）、终答；全部只读结构化字段。
# 函数用途: 构造一次模型调用的响应。
def _empty_thinking() -> ModelResponse:
    return ModelResponse(
        text="",
        backend="empty-budget-scripted-backend",
        assistant_content_blocks=[{"type": "thinking", "thinking": "opaque reasoning"}],
    )


def _thinking_truncated() -> ModelResponse:
    return ModelResponse(
        text="",
        backend="empty-budget-scripted-backend",
        truncated=True,
        runtime_status="unfinished",
        runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )


def _tool_call(*, call_id: str = "call-1") -> ModelResponse:
    return ModelResponse(
        text="",
        backend="empty-budget-scripted-backend",
        tool_use_blocks=[{"id": call_id, "name": "read_file", "input": {"path": "notes.md"}}],
    )


def _final(text: str) -> ModelResponse:
    return ModelResponse(text=text, backend="empty-budget-scripted-backend")


# LLM: nudge 计数只数结构化指令本身，不看模型正文；tool_context 里原样追加过几次就是几次。
# 函数用途: 统计本次循环里空正文 nudge 被追加的次数。
def _nudge_count(params: ToolLoopExecuteParams) -> int:
    return sum(1 for item in params.tool_context if item == _EMPTY_TEXT_NUDGE)


# ---------------------------------------------------------------- 1. 有进展 → 预算恢复

# LLM: 旧语义下第 3 次空正文就结束（预算整轮累计 2 次）；新语义下每次空正文之前只要有一次
#   工具调用真正执行完，预算就恢复——长回合不再"后期一空就死"。修复前本用例必红（calls==5）。
# 函数用途: 验证空 2 次（中间都有工具进展）后第 3 次空正文照常续跑，最终正常交付。
def test_tool_progress_resets_empty_text_budget(monkeypatch, tmp_path) -> None:
    tool = _OkTool()
    result, params, backend = _drive(
        monkeypatch,
        tmp_path,
        _Scenario(
            [
                _empty_thinking(),  # 1 空 → nudge（空 1）
                _tool_call(),  # 2 工具成功 → 真实进展
                _empty_thinking(),  # 3 空 → 预算恢复 + nudge
                _tool_call(call_id="call-2"),  # 4 工具成功 → 真实进展
                _empty_thinking(),  # 5 空 → 预算恢复 + nudge（旧语义在这里结束）
                _final("完成"),  # 6 终答
            ],
            tool,
            "empty-budget-progress",
        ),
    )

    assert backend.calls == 6, "有工具进展时空正文预算恢复，不提前结束"
    assert result.final_response.text == "完成"
    assert params.executed_tools == ["read_file", "read_file"]
    assert _nudge_count(params) == 3


# ---------------------------------------------------------------- 2. 无进展 → 保留硬上限

# LLM: 连续空正文、中间没有任何进展时，仍然最多 2 次 nudge，第 3 次按原方式结束（不变成死循环）。
# 函数用途: 验证无进展硬上限原样保留（第 3 次空正文直接收口、不再有第 4 次模型调用）。
def test_three_consecutive_empty_without_progress_still_ends(monkeypatch, tmp_path) -> None:
    tool = _OkTool()
    result, params, backend = _drive(
        monkeypatch,
        tmp_path,
        _Scenario(
            [
                _empty_thinking(),  # 1 空 → nudge（空 1）
                _empty_thinking(),  # 2 空 → nudge（空 2）
                _empty_thinking(),  # 3 空 → 按原方式结束
                _final("不该被调用"),  # 4 若被调用说明没结束
            ],
            tool,
            "empty-budget-no-progress",
        ),
    )

    assert backend.calls == 3, "连续空正文无进展仍守 2 次硬上限"
    assert result.final_response.text == "", "第 3 次空正文按原方式原样收口"
    assert _nudge_count(params) == 2


# ---------------------------------------------------------------- 3. 与截断预算独立

# LLM: 空正文预算与「只思考截断」续跑预算各算各的：截断轮不消耗空正文额度、空正文预算的
#   恢复也不吞掉截断额度。修复前本用例必红（第 5 枪空正文时空预算已耗尽、直接结束）。
# 函数用途: 验证两条预算互相独立，且截断降档序列不受空正文重置影响。
def test_truncation_budget_and_empty_text_budget_stay_independent(monkeypatch, tmp_path) -> None:
    tool = _OkTool()
    result, params, backend = _drive(
        monkeypatch,
        tmp_path,
        _Scenario(
            [
                _empty_thinking(),  # 1 空 → nudge（空 1）
                _thinking_truncated(),  # 2 只思考截断 → 截断续跑 + 降档（不碰空预算）
                _empty_thinking(),  # 3 空 → nudge（空 2；截断轮没偷吃空预算）
                _tool_call(),  # 4 工具成功 → 真实进展
                _empty_thinking(),  # 5 空 → 预算恢复 + nudge（旧语义在这里结束）
                _thinking_truncated(),  # 6 第二次截断 → 仍按截断预算续跑（already_downgraded）
                _final("完成"),  # 7 终答
            ],
            tool,
            "empty-budget-independent",
        ),
    )

    assert backend.calls == 7, "空正文预算与截断预算各自独立、互不吞掉"
    assert result.final_response.text == "完成"
    assert _nudge_count(params) == 3
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    assert [row["outcome"] for row in trace] == ["downgraded", "already_downgraded"]
    assert backend.seen_efforts == ["max", "max", "high", "max", "max", "max", "max"], (
        "降档只影响紧邻下一次调用，空正文预算重置不改动档位序列"
    )


# ---------------------------------------------------------------- 4. 子代理同样适用

# LLM: 子代理回合（task_local scope）走同一条裁决路径：同样的空正文预算按进展恢复。
# 函数用途: 验证 task_local 形态下第 3 次空正文（前面有工具进展）照常续跑、最终交付。
def test_subagent_scope_gets_same_progress_reset(monkeypatch, tmp_path) -> None:
    tool = _OkTool()
    result, params, backend = _drive(
        monkeypatch,
        tmp_path,
        _Scenario(
            [
                _empty_thinking(),
                _tool_call(),
                _empty_thinking(),
                _tool_call(call_id="call-2"),
                _empty_thinking(),
                _final("完成"),
            ],
            tool,
            "empty-budget-subagent",
            context_scope="task_local",
        ),
    )

    assert backend.calls == 6, "子代理回合同样按工具进展恢复空正文预算"
    assert result.final_response.text == "完成"
    assert _nudge_count(params) == 3


# ---------------------------------------------------------------- 5. 发起但无结果 ≠ 进展

# LLM: "工具调用发起了但没有成功结果"（executed_tools 不增长、archive_tool_calls 增长）不算
#   真实进展：空正文预算不被恢复，连续空仍守硬上限。变异"把发起了但没有结果也算进展"由本用例抓取。
# 函数用途: 验证失败/无结果的工具调用不重置空正文预算。
def test_started_but_unfinished_tool_call_is_not_progress(monkeypatch, tmp_path) -> None:
    tool = _FailTool()
    result, params, backend = _drive(
        monkeypatch,
        tmp_path,
        _Scenario(
            [
                _empty_thinking(),  # 1 空 → nudge（空 1）
                _tool_call(),  # 2 工具调用发起但执行失败 → 只有 archive 增长
                _empty_thinking(),  # 3 空 → 不恢复，nudge（空 2）
                _empty_thinking(),  # 4 空 → 按原方式结束
                _final("不该被调用"),  # 5 若被调用说明预算被错误恢复
            ],
            tool,
            "empty-budget-unfinished",
        ),
    )

    assert backend.calls == 4, "发起了但没有结果的调用不算进展，硬上限照旧"
    assert result.final_response.text == ""
    assert params.executed_tools == [], "没有成功执行过的工具"
    assert len(params.archive_tool_calls) == 1, "调用本身有归档记录"
    assert _nudge_count(params) == 2
