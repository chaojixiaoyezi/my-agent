# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import ParentPlannerRecord
from ..subagents.services.dispatch_params import ParentPlannerRecordParams
from .planner import _build_parent_planner_prompt, _build_parent_planner_state
from .planner_templates import PARENT_PLANNER_SYSTEM_PROMPT


# LLM: RunParentPlannerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存run父级规划器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunParentPlannerParams:

    router: CapabilityRouter
    capability_config: CapabilityConfig | None
    apply: bool
    execute_runners: bool
    max_runners: int
    limit: int
    reviewer: str
    note: str
    runner_instruction: str


# LLM: PlannerLLMParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存规划器llmparams字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class PlannerLLMParams:
    state: dict
    apply: bool
    execute_runners: bool
    max_runners: int
    runner_instruction: str


# LLM: PlannerRecordBuildParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存规划器记录build参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class PlannerRecordBuildParams:
    result: object
    state: dict
    gate_summary: dict
    apply: bool
    runner_instruction: str


# LLM: _ParentPlannerMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分父级规划器混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _ParentPlannerMixin:

    # LLM: run_parent_planner 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进父级规划器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def run_parent_planner(self, params: RunParentPlannerParams) -> ParentPlannerRecord:
        cfg = params.capability_config or CapabilityConfig()
        state = _build_parent_planner_state(
            self,
            cfg,
            max_runners=params.max_runners,
            limit=params.limit,
            reviewer=params.reviewer,
            note=params.note,
        )
        gate_summary = {
            key: int(value) for key, value in state["gate"].items() if isinstance(value, int)
        }

        if not state["gate"].get("needs_planner", 0):
            return self._make_heartbeat_ok_record(not params.apply, gate_summary)

        result = self._execute_planner_llm(
            PlannerLLMParams(
                state=state,
                apply=params.apply,
                execute_runners=params.execute_runners,
                max_runners=params.max_runners,
                runner_instruction=params.runner_instruction,
            )
        )
        if result is None:
            return self._make_planner_error_record(
                gate_summary, params.runner_instruction, params.apply
            )
        return self._build_planner_record(
            PlannerRecordBuildParams(
                result=result,
                state=state,
                gate_summary=gate_summary,
                apply=params.apply,
                runner_instruction=params.runner_instruction,
            )
        )

    # LLM: _make_heartbeat_ok_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建heartbeatok记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _make_heartbeat_ok_record(self, dry_run, gate_summary):
        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=dry_run,
                triggered=False,
                ok=True,
                decision="HEARTBEAT_OK",
                message="planner gate 确认无 active/pending/stalled/needs-intervention 事项，允许 HEARTBEAT_OK。",
                gate_summary=gate_summary,
                summary="no work",
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=dry_run)
        self.subagents.write_parent_planner_report(report, append_log=False)
        return record

    # LLM: _execute_planner_llm 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进规划器llm的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _execute_planner_llm(self, params: PlannerLLMParams):
        prompt = _build_parent_planner_prompt(
            params.state,
            apply=params.apply,
            execute_runners=params.execute_runners,
            max_runners=params.max_runners,
            runner_instruction=params.runner_instruction,
        )
        self.subagents.write_parent_planner_exchange(prompt)
        try:
            return self.run(
                prompt,
                save=False,
                # LLM: parent planner is a control-plane structured call; the state snapshot is authoritative.
                allowed_tools=[],
                resume_context=False,
                system_prompt_override=PARENT_PLANNER_SYSTEM_PROMPT,
                context_scope="control_plane",
            )
        except Exception:
            return None

    # LLM: _make_planner_error_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建规划器error记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _make_planner_error_record(self, gate_summary, runner_instruction, apply):
        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not apply,
                triggered=True,
                ok=False,
                decision="PLANNER_ERROR",
                message="父代理 planner 调用失败",
                gate_summary=gate_summary,
                runner_instruction=runner_instruction,
                evidence_paths=[],
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
        self.subagents.write_parent_planner_report(report, append_log=apply)
        return record

    # LLM: _build_planner_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建规划器记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _build_planner_record(self, params: PlannerRecordBuildParams):
        from ..subagents.parsing import parse_parent_planner_output

        prompt_path, response_path = self.subagents.write_parent_planner_exchange(
            params.result.prompt,
            params.result.response,
        )
        parsed = parse_parent_planner_output(params.result.response)
        ok, decision, message, parse_error = _planner_record_status(parsed, params.state)

        record = self.subagents.make_parent_planner_record(
            params=ParentPlannerRecordParams(
                dry_run=not params.apply,
                triggered=True,
                ok=ok,
                decision=decision,
                message=message,
                gate_summary=params.gate_summary,
                backend=params.result.backend,
                tool_rounds=params.result.tool_rounds,
                parse_error=parse_error,
                summary=parsed.summary,
                actions=parsed.actions,
                blockers=parsed.blockers,
                risks=parsed.risks,
                notes=parsed.notes,
                runner_instruction=parsed.runner_instruction,
                suggested_max_runners=parsed.suggested_max_runners,
                prompt_path=prompt_path,
                response_path=response_path,
                evidence_paths=[prompt_path, response_path],
            ),
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not params.apply)
        self.subagents.write_parent_planner_report(report, append_log=params.apply)
        return record


# LLM: _planner_record_status 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理规划器记录状态相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _planner_record_status(parsed, state: dict) -> tuple[bool, str, str, str]:
    # LLM: 解析兜底规则与父级规划记录持久化分离，避免失败处理写错位置。
    ok = parsed.found and parsed.ok
    decision = parsed.decision or "PARSE_ERROR"
    message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
    parse_error = parsed.parse_error
    if not parsed.found:
        return False, "PARSE_ERROR", "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。", "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。"
    if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
        return False, decision, "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。", parse_error or "planner gate blocked HEARTBEAT_OK"
    return ok, decision, message, parse_error
