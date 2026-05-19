# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: runner result recording and debrief persistence.

给人看的解释：
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""

import time
from pathlib import Path

from .manager_runner_result_payload import (
    BuildAndPersistContext,
    RecordRunnerResultParams,
    _ApplyStatusParams,
    _ExtractedOutput,
    apply_status_and_build_payload,
)
from .models import SubAgentParsedOutput, SubAgentRunnerResult, SubAgentTask
from .result_processors import (
    RunnerResultContext,
    _append_runner_debrief_content,
    _build_runner_result,
    _process_structured_output,
    _write_runner_result_files,
    merge_actual_tools_for_unparsed,
)
from .runner_rendering import render_runner_result_markdown
from .services.subagent_session_compact import (
    SubagentSessionCompactRequest,
    write_subagent_session_compact,
)
from .utils import _apply_missing_paths

# ---------------------------------------------------------------------------
# Internal helpers — promoted from SubAgentRunnerResultMixin to module scope
# ---------------------------------------------------------------------------

# LLM: _runner_append_debrief 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 推进执行器append复盘的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _runner_append_debrief(task, parsed):
    """Append runner debrief content."""
    _append_runner_debrief_content(task, parsed)


# LLM: _PostResultSideEffectParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存post结果sideeffect参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
class _PostResultSideEffectParams:

    # LLM: __init__ 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def __init__(self, output_payload: dict, dry_run: bool, parsed: SubAgentParsedOutput, lessons: list):
        self.output_payload = output_payload
        self.dry_run = dry_run
        self.parsed = parsed
        self.lessons = lessons


# LLM: _RunnerResultBuildParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器结果build参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
class _RunnerResultBuildParams:

    # LLM: __init__ 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def __init__(self, params: RecordRunnerResultParams, extracted: _ExtractedOutput, now: float):
        self.params = params
        self.extracted = extracted
        self.now = now


# LLM: _SubAgentRunnerResultFacade 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent执行器结果门面流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class _SubAgentRunnerResultFacade:
    # LLM: _build_and_persist_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建persist结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _build_and_persist_result(
        self,
        ctx: BuildAndPersistContext,
    ) -> SubAgentRunnerResult:
        """Build runner result and persist files."""
        result = _build_runner_result(
            RunnerResultContext(
                task=ctx.task,
                dry_run=ctx.params.dry_run,
                ok=ctx.final_ok,
                message=ctx.final_message,
                backend=ctx.params.backend,
                tool_rounds=ctx.params.tool_rounds,
                prompt=ctx.params.prompt,
                response=ctx.params.response,
                parsed=ctx.parsed,
                structured_repair_attempted=ctx.params.structured_repair_attempted,
                structured_repair_ok=ctx.params.structured_repair_ok,
                structured_repair_error=ctx.params.structured_repair_error,
                structured_evidence_count=ctx.structured_evidence_count,
                structured_request_count=ctx.structured_request_count,
                artifact_count=len(ctx.artifacts),
                test_count=len(ctx.tests),
                patch_count=len(ctx.patches),
                lesson_count=len(ctx.lessons),
                now=ctx.now,
            )
        )
        _write_runner_result_files(ctx.task, result, ctx.output_payload, prompt=ctx.params.prompt, response=ctx.params.response)
        Path(ctx.task.runner_result_file).write_text(render_runner_result_markdown(result), encoding="utf-8")
        return result

    # LLM: _extract_parsed_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理extractparsedoutput相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _extract_parsed_output(
        self,
        task: SubAgentTask,
        structured_output: SubAgentParsedOutput | None,
        now: float,
        actual_tools: list[str] | None,
    ) -> _ExtractedOutput:
        """Extract parsed output or return defaults."""
        parsed = structured_output or SubAgentParsedOutput()
        if parsed.found and parsed.ok:
            proc = _process_structured_output(task, parsed, now, actual_tools)
            return _ExtractedOutput(
                parsed=parsed,
                ignored_tools=proc["ignored_tools"],
                ignored_skills=proc["ignored_skills"],
                structured_evidence_count=proc["structured_evidence_count"],
                structured_request_count=proc["structured_request_count"],
                created_request_ids=proc["created_request_ids"],
                artifacts=proc["artifacts"],
                evidence_packets=proc["evidence_packets"],
                findings=proc["findings"],
                tests=proc["tests"],
                patches=proc["patches"],
                lessons=proc["lessons"],
                next_actions=proc["next_actions"],
            )
        merge_actual_tools_for_unparsed(task, actual_tools, now)
        return _ExtractedOutput(parsed=parsed)

    # LLM: _apply_status_and_build_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新状态build载荷对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _apply_status_and_build_payload(
        self,
        params: RecordRunnerResultParams,
        extracted: _ApplyStatusParams,
        now: float,
    ) -> tuple[dict, BuildAndPersistContext]:
        """Apply status to task and build output payload."""
        # LLM: 载荷组装外移，让该管理器保持兼容门面职责。
        return apply_status_and_build_payload(params, extracted, now)

    # LLM: _post_result_side_effects 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 发送结果sideeffects请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _post_result_side_effects(
        self,
        task: SubAgentTask,
        result: SubAgentRunnerResult,
        params: _PostResultSideEffectParams,
    ) -> int:
        """Handle save, debrief, learning side effects. Returns learning candidate count."""
        output_payload = params.output_payload
        parsed = params.parsed
        if parsed.found and parsed.ok:
            # LLM: 状态报告字段来自结构化执行器输出，便于父级可见。
            task.latest_summary = parsed.summary or task.latest_summary
            task.current_step = parsed.status or task.status
        for blocker in output_payload.get("blockers", []) or []:
            text = str(blocker or "").strip()
            if text and text not in task.blockers:
                task.blockers.append(text)
        self.save(task)
        if parsed.found and parsed.ok:
            _runner_append_debrief(task, parsed)
        learning_candidates = []
        if not params.dry_run and parsed.found and parsed.ok and params.lessons:
            learning_candidates = self.record_learning_candidates(task, params.lessons)
        self._append_task_work_log(
            task,
            f"subagent_runner: dry_run={params.dry_run} ok={result.ok} status={task.status} "
            f"message={result.message} learning_candidates={len(learning_candidates)}",
        )
        self._index_runner_result(result, output_payload)
        return len(learning_candidates)

    # LLM: record_runner_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入执行器结果的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_runner_result(
        self,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult:
        task = self.load(params.run_id)
        stale_result = self._check_stale_runner_result(task, params.attempt_id, params.dry_run)
        if stale_result:
            return stale_result

        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        now = time.time()

        extracted = self._extract_parsed_output(task, params.structured_output, now, params.actual_tools)
        output_payload, build_ctx = self._runner_result_build_context(
            _RunnerResultBuildParams(params, extracted, now),
            task,
        )
        result = self._build_and_persist_result(build_ctx)
        self._post_result_side_effects(
            task,
            result,
            _PostResultSideEffectParams(output_payload, params.dry_run, extracted.parsed, extracted.lessons),
        )
        _abandon_superseded_artifact_repair_children(self, task, result, now)
        # LLM: save=False runner compact signals are persisted only as task-local package refs.
        session_refs = write_subagent_session_compact(
            SubagentSessionCompactRequest(task, params.session_compact or {}, output_payload)
        )
        if session_refs:
            self.save(task)
        # LLM: optional debug tracing is refs-only and gated by subagent_debug_trace_level.
        from .debug_trace import SubAgentRunnerTraceRequest, trace_runner_result

        trace_runner_result(SubAgentRunnerTraceRequest(self, task, result, params))
        return result

    # LLM: _runner_result_build_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 推进执行器结果build上下文的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def _runner_result_build_context(
        self,
        build_params: _RunnerResultBuildParams,
        task: SubAgentTask,
    ):
        params = build_params.params
        extracted = build_params.extracted
        output_payload, build_ctx = self._apply_status_and_build_payload(
            params,
            _ApplyStatusParams(
                task=task,
                parsed=extracted.parsed,
                structured_evidence_count=extracted.structured_evidence_count,
                structured_request_count=extracted.structured_request_count,
                created_request_ids=extracted.created_request_ids,
                structured_repair_attempted=params.structured_repair_attempted,
                structured_repair_ok=params.structured_repair_ok,
                structured_repair_error=params.structured_repair_error,
                actual_tools=params.actual_tools,
                ignored_tools=extracted.ignored_tools,
                ignored_skills=extracted.ignored_skills,
                artifacts=extracted.artifacts,
                evidence_packets=extracted.evidence_packets,
                findings=extracted.findings,
                tests=extracted.tests,
                patches=extracted.patches,
                lessons=extracted.lessons,
                next_actions=extracted.next_actions,
            ),
            build_params.now,
        )
        # Override params in context with actual params object for full field access
        build_ctx.params = params
        return output_payload, build_ctx

    # LLM: _check_stale_runner_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验stale执行器结果需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def _check_stale_runner_result(self, task, attempt_id, dry_run):
        if not dry_run and (_task_text_attr(task, "status").upper() == "TAKEN_OVER" or _task_text_attr(task, "takeover_by")):
            return self._make_quick_result(task, dry_run, False, "ignored runner result for already taken-over run")
        normalized_attempt_id = str(attempt_id or "").strip()
        if normalized_attempt_id:
            if normalized_attempt_id in _task_list_attr(task, "runner_abandoned_attempt_ids"):
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for abandoned attempt {normalized_attempt_id}")
            active_attempt_id = _task_text_attr(task, "runner_active_attempt_id")
            if active_attempt_id and active_attempt_id != normalized_attempt_id:
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for non-active attempt {normalized_attempt_id}")
        return None

    # LLM: _make_quick_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建quick结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _make_quick_result(self, task, dry_run, ok, message):
        return SubAgentRunnerResult(
            run_id=task.id, dry_run=dry_run, ok=ok, status=task.status,
            verification_status=task.verification_status, message=message,
            runner_attempts=task.runner_attempts, runner_last_error=task.runner_last_error,
            execution_context_json=task.execution_context_json, execution_context_file=task.execution_context_file,
            prompt_file=task.runner_prompt_file, response_file=task.runner_response_file,
            result_file=task.runner_result_file, result_json=task.runner_result_json,
            output_json=task.output_json, created_at=time.time(),
        )


# LLM: _task_text_attr prevents MagicMock default attributes from becoming real task state.
# 函数用途: 从真实 task 或测试替身读取字符串字段；缺失/非字符串值按空值处理。
def _task_text_attr(task, name: str) -> str:
    value = getattr(task, name, "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _task_list_attr reads list-like task fields while ignoring mock placeholders.
# 函数用途: 兼容旧测试替身缺字段的情况，避免 stale runner 判断误读 MagicMock。
def _task_list_attr(task, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]


# LLM: A later successful source attempt supersedes stale artifact-repair children created from an older blocker.
# 函数用途: 同一个 run 后续已产出可验收结果时，把旧的产物修复子任务收口为 ABANDONED，避免过期后代卡住父级验收。
def _abandon_superseded_artifact_repair_children(
    manager: object,
    task: SubAgentTask,
    result: SubAgentRunnerResult,
    now: float,
) -> None:
    if not _source_result_supersedes_artifact_repairs(task, result):
        return
    for child in _artifact_repair_children_for_source(manager, task.id):
        if str(getattr(child, "status", "") or "").upper() in {"DONE", "VERIFIED", "TAKEN_OVER", "ABANDONED"}:
            continue
        child.status = "ABANDONED"
        child.verification_status = "VERIFIED"
        child.failure_type = ""
        child.result = "已由源 run 后续成功结果覆盖，修复任务不再需要执行。"
        child.abandoned_at = now
        child.ended_at = now
        child.updated_at = now
        attrs = getattr(child, "attributes", None)
        if not isinstance(attrs, dict):
            attrs = {}
            child.attributes = attrs
        attrs["superseded_by_run_id"] = task.id
        attrs["superseded_reason"] = "source_run_later_awaiting_acceptance"
        manager.save(child)
        _append_superseded_child_log(manager, child, task.id)


# LLM: _source_result_supersedes_artifact_repairs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _source_result_supersedes_artifact_repairs(task: SubAgentTask, result: SubAgentRunnerResult) -> bool:
    if not result.ok:
        return False
    if str(getattr(task, "failure_type", "") or "").strip():
        return False
    if any("artifact_integrity_failed:" in item for item in _task_list_attr(task, "blockers")):
        return False
    return str(getattr(task, "status", "") or "").upper() in {"AWAITING_ACCEPTANCE", "DONE"}


# LLM: _artifact_repair_children_for_source is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _artifact_repair_children_for_source(manager: object, source_run_id: str) -> list[SubAgentTask]:
    children: list[SubAgentTask] = []
    for candidate in manager.list_runs():
        attrs = getattr(candidate, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            continue
        if attrs.get("repair_kind") != "artifact_integrity":
            continue
        if attrs.get("repair_source_run_id") != source_run_id:
            continue
        if str(getattr(candidate, "parent_id", "") or "") != source_run_id:
            continue
        children.append(candidate)
    return children


# LLM: _append_superseded_child_log is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _append_superseded_child_log(manager: object, child: SubAgentTask, source_run_id: str) -> None:
    message = (
        "runner_result: 已标记为 ABANDONED，"
        f"因为源 run {source_run_id} 后续成功结果已覆盖这次 artifact repair。"
    )
    try:
        manager._append_task_work_log(child, message)
    except AttributeError:
        with Path(child.work_log_file).open("a", encoding="utf-8") as handle:
            handle.write(f"- {message}\n")


# LLM: SubAgentRunnerResultMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent执行器结果混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentRunnerResultMixin(_SubAgentRunnerResultFacade):
    """Public compatibility mixin; runner result behavior stays in the facade class."""
