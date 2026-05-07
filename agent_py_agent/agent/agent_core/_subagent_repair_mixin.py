# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass

from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..subagent import parse_subagent_runner_output
from .runner_prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_repair_prompt,
)


# LLM: SubagentRepairParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存子代理repair参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubagentRepairParams:
    context: object
    result: object
    structured: object
    prompt_for_log: str
    response_for_log: str
    backend_name: str
    message: str


# LLM: RecoverySnapshotParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存恢复snapshot参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecoverySnapshotParams:
    run_id: str
    user_prompt: str
    response_text: str
    backend: str
    status: str
    error_code: str
    tool_calls: list[dict[str, object]]


# LLM: _SubagentRepairMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分子代理repair混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _SubagentRepairMixin:

    # LLM: _handle_subagent_repair 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进子代理repair的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _handle_subagent_repair(self, params: SubagentRepairParams):
        structured_repair_ok = False
        structured_repair_error = ""

        repair_prompt = _build_subagent_runner_repair_prompt(
            params.context,
            original_prompt=params.result.prompt,
            original_response=params.result.response,
            parse_error=params.structured.parse_error,
        )
        try:
            repair_response = self.backend.generate(repair_prompt)
        except Exception as exc:
            return _repair_failure_tuple(params, exc)

        repaired = parse_subagent_runner_output(repair_response.text)
        prompt_for_log = _append_runner_repair_prompt(params.result.prompt, repair_prompt)
        response_for_log = _append_runner_repair_response(params.result.response, repair_response.text)
        backend_name = repair_response.backend or params.result.backend
        structured = params.structured
        message = params.message

        if repaired.found and repaired.ok:
            structured = repaired
            structured_repair_ok = True
            message = "runner 已完成模型调用，并已修复结构化结果，等待独立验收。"
        elif not structured.found and repaired.found:
            structured = repaired
            structured_repair_error = repaired.parse_error
        else:
            structured_repair_error = (
                repaired.parse_error or "repair response still missing structured output"
            )

        return (
            structured,
            structured_repair_ok,
            structured_repair_error,
            backend_name,
            prompt_for_log,
            response_for_log,
            message,
        )

    # LLM: _write_subagent_recovery_snapshot 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入子代理恢复snapshot的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _write_subagent_recovery_snapshot(
        self,
        run_id: str | None = None,
        *,
        params: RecoverySnapshotParams | None = None,
        user_prompt: str = "",
        response_text: str = "",
        backend: str = "",
        status: str = "",
        error_code: str = "",
        tool_calls: list[dict] | None = None,
    ) -> None:

        if not bool(getattr(self.config, "memory_hook_enabled", True)):
            return
        snapshot = _recovery_snapshot_params(
            params,
            run_id=run_id,
            user_prompt=user_prompt,
            response_text=response_text,
            backend=backend,
            status=status,
            error_code=error_code,
            tool_calls=tool_calls,
        )
        try:
            task = self.subagents.load(snapshot.run_id)
        except (FileNotFoundError, TypeError):
            task = None
        write_recovery_snapshot(
            self.root,
            params=_recovery_snapshot_input(self, snapshot, _recovery_content_paths(task)),
        )


# LLM: _repair_failure_tuple 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理repair失败tuple相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _repair_failure_tuple(params: SubagentRepairParams, exc: Exception) -> tuple:
    # LLM: repair failures preserve the original structured result and only append audit text.
    return (
        params.structured,
        False,
        str(exc),
        params.backend_name,
        params.prompt_for_log,
        _append_runner_repair_failure(params.result.response, exc),
        params.message,
    )


# LLM: _recovery_snapshot_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理恢复snapshot参数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _recovery_snapshot_params(
    params: RecoverySnapshotParams | None,
    *,
    run_id: str | None,
    user_prompt: str,
    response_text: str,
    backend: str,
    status: str,
    error_code: str,
    tool_calls: list[dict] | None,
) -> RecoverySnapshotParams:
    if params is not None:
        if not isinstance(params, RecoverySnapshotParams):
            raise TypeError("subagent recovery snapshot requires params: RecoverySnapshotParams")
        return params
    # LLM: 旧恢复字段先归一到一个参数包，再进入快照持久化。
    return RecoverySnapshotParams(
        run_id=str(run_id or ""),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        status=str(status),
        error_code=str(error_code),
        tool_calls=list(tool_calls or []),
    )


# LLM: _recovery_content_paths 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理恢复内容路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _recovery_content_paths(task) -> list[str]:
    if task is None:
        return []
    return [
        task.status_file,
        task.work_log_file,
        task.runner_result_file,
        task.runner_result_json,
        task.output_json,
        task.handoff_file,
    ]


# LLM: _recovery_snapshot_input 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理恢复snapshotinput相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _recovery_snapshot_input(agent, snapshot: RecoverySnapshotParams, content_paths: list[str]):
    next_actions = [
        "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
    ]
    return RecoverySnapshotInput(
        session_id=getattr(agent, "session_id", agent.config.agent_name),
        user_prompt=snapshot.user_prompt,
        response_text=snapshot.response_text,
        backend=snapshot.backend,
        source="subagent_run",
        request_id=f"subagent-run:{snapshot.run_id}",
        run_id=snapshot.run_id,
        task_id=snapshot.run_id,
        status=str(snapshot.status).lower() or "unknown",
        error_code=snapshot.error_code,
        tool_calls=snapshot.tool_calls,
        task_refs=[snapshot.run_id],
        content_paths=content_paths,
        next_actions=next_actions,
        archive_level=int(getattr(agent.config, "memory_hook_archive_level", 3)),
    )
