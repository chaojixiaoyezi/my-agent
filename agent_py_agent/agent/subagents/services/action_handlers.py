# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""concrete action handlers used by SubAgentActionService.

给人看的解释：
每个 handler 只处理一种动作写回，公共审计字段放在 ActionHandlerContext 里。
"""

from pathlib import Path

from .action_context import ActionHandlerContext
from .action_params import RecordAfterTaskActionParams


# LLM: apply_probe_or_repair_channel 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新proberepair通道对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def apply_probe_or_repair_channel(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    result = service.manager.probe_channel(action.run_id)
    task = service.manager.load(action.run_id)
    return ActionApplyRecord(
        id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=False, applied=True, ok=True,
        message=f"已执行 channel probe，结果为 {result.channel_status}。",
        before_status=ctx.before_status, after_status=task.status,
        before_channel_status=ctx.before_channel_status, after_channel_status=task.channel_status,
        evidence_paths=[task.channel_probe_file, str(Path(task.logs_dir) / "last_channel_probe.json")],
        created_at=ctx.now,
    )


# LLM: apply_repair_work_order 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新repairworkorder对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def apply_repair_work_order(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    service.manager.save(task)
    validation = service.manager.validate_work_order(action.run_id)
    task = service.manager.load(action.run_id)
    ok = validation.ok
    message = "已补齐标准工单现场。" if ok else f"工单仍缺少 {len(validation.missing)} 个路径。"
    service._append_task_work_log(task, f"action_apply repair_work_order: {message}")
    return ActionApplyRecord(
        id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=False, applied=ok, ok=ok, message=message,
        before_status=ctx.before_status, after_status=task.status,
        before_channel_status=ctx.before_channel_status, after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.work_log_file], created_at=ctx.now,
    )


# LLM: apply_reopen_for_evidence 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新reopen证据对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def apply_reopen_for_evidence(service, action, task, ctx: ActionHandlerContext):
    task.status = "BLOCKED"
    task.failure_type = "missing_evidence"
    task.verification_status = "UNVERIFIED"
    task.updated_at = ctx.now
    task.result = task.result or "缺少验收证据，等待补充 evidence 后再完成。"
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action, task, ctx.before_status, ctx.before_channel_status, "已把缺证据的 DONE 任务改为 BLOCKED。"
        )
    )


# LLM: apply_run_acceptance 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新验收对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def apply_run_acceptance(service, action, task, ctx: ActionHandlerContext):
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action, task, ctx.before_status, ctx.before_channel_status, "已标记为需要验收，未自动执行未知命令。"
        )
    )


# LLM: apply_stop_no_progress_and_escalate records a terminal retry fuse without changing ownership or spawning work.
# 函数用途: 连续恢复无进展时写入明确 blocker/worklog，让父级停止自动重试并根据 refs 人工决策。
def apply_stop_no_progress_and_escalate(service, action, task, ctx: ActionHandlerContext):
    task.failure_type = "no_progress_fuse"
    blocker = "no_progress_fuse: 连续恢复没有进展，已停止自动重试和扩容。"
    if blocker not in task.blockers:
        task.blockers.append(blocker)
    task.current_step = "no-progress fuse tripped; waiting for parent/user decision"
    task.latest_summary = "连续恢复无进展；请父级/用户根据 checkpoint、summary 和 task-local refs 决定下一步。"
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(
        task,
        "action_apply stop_no_progress_and_escalate: 已触发 no-progress fuse，停止自动重试和扩容。",
    )
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            "no-progress fuse 已触发：已停止自动重试和扩容，等待父级/用户决策。",
            evidence_paths=[task.work_log_file, task.task_dir],
        )
    )


# LLM: apply_create_artifact_repair_child creates a scoped worker from machine-readable integrity refs.
# 函数用途: 产物结构检查失败时自动派修复小傻妞，继承目标产物写入根，不让父级直接改业务文件。
def apply_create_artifact_repair_child(service, action, task, ctx: ActionHandlerContext):
    from agent_py_agent.agent.agent_core.orchestration_artifact_integrity_signals import (
        artifact_integrity_signals_from_tasks,
    )

    if _is_artifact_integrity_repair_task(task):
        return _nested_artifact_repair_refused_record(service, action, task, ctx)
    signals = artifact_integrity_signals_from_tasks([task])
    if not signals:
        return _artifact_repair_missing_signal_record(service, action, task, ctx)
    signal = signals[0]
    if existing := _existing_artifact_repair_child(service.manager, task.id, signal):
        message = f"已复用已有 artifact repair run {existing.id}，未重复创建修复任务。"
        service._append_task_work_log(task, f"action_apply create_repair_child_from_artifact_integrity_refs: {message}")
        return service._record_after_task_action(
            RecordAfterTaskActionParams(
                action,
                task,
                ctx.before_status,
                ctx.before_channel_status,
                message,
                evidence_paths=[existing.task_dir, existing.output_json, task.work_log_file],
                created_run_ids=[existing.id],
            )
        )
    repair = service.manager.create_run(
        goal=_artifact_repair_goal(signal),
        thought="根据 artifact_integrity failure refs 修复已生成产物。",
        plan=["读取 failure refs", "修复列出的产物文件", "读回并验证结构"],
        agent_name="小傻妞-产物修复",
        role="worker",
        parent_id=task.id,
        root_id=task.root_id or task.id,
        depth=int(getattr(task, "depth", 0) or 0) + 1,
        allowed_tools=["list_files", "read_file", "search_text", "replace_in_file", "write_file", "append_file"],
        acceptance_checks=[
            "只修复 artifact_integrity failure_refs 列出的产物文件",
            "修复后读取被修改文件，确认 HTML 结构闭合且没有占位链接",
            "不要改写无关产物或健康分支",
        ],
        extra_write_roots=[str(root) for root in signal.get("allowed_write_roots", []) or []],
        attributes={
            "repair_source_run_id": task.id,
            "repair_kind": "artifact_integrity",
            "failure_refs": signals,
            "target_artifact_refs": list(signal.get("artifact_refs", []) or []),
        },
    )
    task.updated_at = ctx.now
    service.manager.save(task)
    message = f"已创建 artifact repair run {repair.id} 修复产物结构失败。"
    service._append_task_work_log(task, f"action_apply create_repair_child_from_artifact_integrity_refs: {message}")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            message,
            evidence_paths=[repair.task_dir, repair.output_json, task.work_log_file],
            created_run_ids=[repair.id],
        )
    )


# LLM: apply_create_parent_acceptance_repair_child turns failed parent tests into scoped repair work.
# 函数用途: 父级真实验收失败时自动派修复小傻妞，读取验收 refs 后只修被点名产物。
def apply_create_parent_acceptance_repair_child(service, action, task, ctx: ActionHandlerContext):
    from agent_py_agent.agent.agent_core.orchestration_parent_acceptance_repair import (
        parent_acceptance_repair_signals_from_tasks,
    )

    if _is_parent_acceptance_repair_task(task):
        return _nested_parent_acceptance_repair_refused_record(service, action, task, ctx)
    signals = parent_acceptance_repair_signals_from_tasks([task])
    if not signals:
        return _parent_acceptance_repair_missing_signal_record(service, action, task, ctx)
    signal = signals[0]
    target_refs = list(signal.get("artifact_refs", []) or []) or list(getattr(task, "artifact_refs", []) or [])
    if existing := _existing_parent_acceptance_repair_child(service.manager, task.id, signal, target_refs):
        message = f"已复用已有 parent acceptance repair run {existing.id}，未重复创建修复任务。"
        service._append_task_work_log(task, f"action_apply create_repair_child_from_parent_acceptance_refs: {message}")
        return service._record_after_task_action(
            RecordAfterTaskActionParams(
                action,
                task,
                ctx.before_status,
                ctx.before_channel_status,
                message,
                evidence_paths=[existing.task_dir, existing.output_json, task.work_log_file],
                created_run_ids=[existing.id],
            )
        )
    repair = service.manager.create_run(
        goal=_parent_acceptance_repair_goal(signal, target_refs),
        thought="根据 parent_acceptance failure refs 修复已生成产物。",
        plan=["读取 acceptance/test/followup refs", "修复被父级验收点名的问题", "读回并等待父级重新验收"],
        agent_name="小傻妞-验收修复",
        role="worker",
        parent_id=task.id,
        root_id=task.root_id or task.id,
        depth=int(getattr(task, "depth", 0) or 0) + 1,
        allowed_tools=["subagent_board", "list_files", "read_file", "search_text", "replace_in_file", "write_file", "append_file"],
        acceptance_checks=[
            "只修复 parent_acceptance failure_refs 点名的问题和产物文件",
            "修复后读取被修改文件，确认父级失败测试点已消失",
            "不要改写无关产物或健康分支",
        ],
        extra_write_roots=[str(root) for root in signal.get("allowed_write_roots", []) or []],
        attributes={
            "repair_source_run_id": task.id,
            "repair_kind": "parent_acceptance",
            "failure_refs": signals,
            "target_artifact_refs": target_refs,
        },
    )
    task.updated_at = ctx.now
    service.manager.save(task)
    message = f"已创建 parent acceptance repair run {repair.id} 修复父级验收失败。"
    service._append_task_work_log(task, f"action_apply create_repair_child_from_parent_acceptance_refs: {message}")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            message,
            evidence_paths=[repair.task_dir, repair.output_json, task.work_log_file],
            created_run_ids=[repair.id],
        )
    )


# LLM: verified repair children close the source blocker instead of leaving a stale parent task hot forever.
# 函数用途: artifact repair 子任务已通过验收后，把父任务从 BLOCKED 收口到 DONE/VERIFIED，并保留修复子任务证据引用。
def apply_close_parent_from_verified_repair_child(service, action, task, ctx: ActionHandlerContext):
    from ..model_capabilities import VerificationEvidence
    from ..reports import ActionApplyRecord

    repair = _verified_repair_child(service.manager, task)
    if not repair:
        return ActionApplyRecord(
            id=service.manager._new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=False,
            ok=False,
            message="未找到已 VERIFIED 的 repair 子任务，未收口父任务。",
            before_status=ctx.before_status,
            after_status=task.status,
            before_channel_status=ctx.before_channel_status,
            after_channel_status=task.channel_status,
            evidence_paths=[task.task_dir, task.output_json],
            created_at=ctx.now,
        )

    task.status = "DONE"
    task.verification_status = "VERIFIED"
    task.failure_type = ""
    task.blockers = []
    task.result = f"问题已由修复子任务 {repair.id} 修复并通过验收。"
    task.current_step = "completed by verified repair child"
    task.latest_summary = task.result
    task.progress = 1.0
    task.updated_at = ctx.now
    task.ended_at = task.ended_at or ctx.now
    task.artifact_refs = _merge_refs(task.artifact_refs, repair.artifact_refs)
    task.evidence.append(
        VerificationEvidence(
            kind="artifact_integrity_repair_child",
            summary=f"修复子任务 {repair.id} 已 DONE/VERIFIED。",
            path=repair.output_json,
            ok=True,
            created_at=ctx.now,
        )
    )
    task.attributes["resolved_by_repair_run_id"] = repair.id
    service.manager.save(task)
    message = f"已用 VERIFIED repair run {repair.id} 收口父任务。"
    service._append_task_work_log(task, f"action_apply close_parent_from_verified_repair_child: {message}")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            message,
            evidence_paths=[repair.task_dir, repair.output_json, task.work_log_file],
        )
    )


# LLM: artifact repair is intentionally single-hop; failed repair should be recovered by takeover.
# 函数用途: 判断 task 是否已是 artifact_integrity repair，作为 action apply 的最后防线。
def _is_artifact_integrity_repair_task(task) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and attrs.get("repair_kind") == "artifact_integrity"


# LLM: parent acceptance repair is also single-hop; failed repair should hand off to takeover.
# 函数用途: 防止验收修复任务再次因验收失败而递归创建同类修复任务。
def _is_parent_acceptance_repair_task(task) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and attrs.get("repair_kind") == "parent_acceptance"


# LLM: stale action plans must not create nested artifact repair workers.
# 函数用途: 旧计划误触发 repair action 时返回可审计失败记录，不改任务树。
def _nested_artifact_repair_refused_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message="artifact repair run 已失败；拒绝创建嵌套 repair child，请改走 takeover_or_reassign。",
        before_status=ctx.before_status,
        after_status=task.status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.output_json],
        created_at=ctx.now,
    )


# LLM: _artifact_repair_missing_signal_record preserves auditability when a stale plan lacks refs.
# 函数用途: action plan 指向产物修复但 task/output 中没有机器 failure refs 时，失败留痕不乱派工。
def _artifact_repair_missing_signal_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message="未找到 artifact_integrity failure refs，未创建修复任务。",
        before_status=ctx.before_status,
        after_status=task.status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.output_json],
        created_at=ctx.now,
    )


# LLM: stale parent-acceptance action plans must not create nested repair workers.
# 函数用途: 验收修复 run 自己失败时返回可审计失败记录，由 takeover/recovery 接手。
def _nested_parent_acceptance_repair_refused_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message="parent acceptance repair run 已失败；拒绝创建嵌套 repair child，请改走 takeover_or_reassign。",
        before_status=ctx.before_status,
        after_status=task.status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.output_json],
        created_at=ctx.now,
    )


# LLM: _parent_acceptance_repair_missing_signal_record preserves auditability when reports are missing.
# 函数用途: action plan 指向父级验收修复但缺少 acceptance/test refs 时，失败留痕不乱派工。
def _parent_acceptance_repair_missing_signal_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message="未找到 parent_acceptance failure refs，未创建修复任务。",
        before_status=ctx.before_status,
        after_status=task.status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.output_json],
        created_at=ctx.now,
    )


# LLM: _existing_artifact_repair_child keeps recovery idempotent for one source failure.
# 函数用途: 同一个 artifact_integrity failure 已有修复 run 时复用，避免重复创建一批相同小傻妞。
def _existing_artifact_repair_child(manager, source_run_id: str, signal: dict[str, object]):
    target_refs = _normalized_ref_set(signal.get("artifact_refs", []) or [])
    for candidate in manager.list_runs():
        attrs = getattr(candidate, "attributes", {}) or {}
        if attrs.get("repair_kind") != "artifact_integrity":
            continue
        if attrs.get("repair_source_run_id") != source_run_id:
            continue
        if not _repair_child_is_reusable(candidate):
            continue
        candidate_refs = _normalized_ref_set(attrs.get("target_artifact_refs", []) or [])
        if not target_refs or not candidate_refs or target_refs == candidate_refs:
            return candidate
    return None


# LLM: _verified_repair_child is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _verified_repair_child(manager, task):
    target_refs = _normalized_ref_set(getattr(task, "artifact_refs", []) or [])
    for candidate in manager.list_runs():
        attrs = getattr(candidate, "attributes", {}) or {}
        if attrs.get("repair_kind") not in {"artifact_integrity", "parent_acceptance"}:
            continue
        if attrs.get("repair_source_run_id") != task.id:
            continue
        if candidate.status != "DONE" or candidate.verification_status != "VERIFIED":
            continue
        candidate_refs = _normalized_ref_set(attrs.get("target_artifact_refs", []) or [])
        if target_refs and candidate_refs and target_refs != candidate_refs:
            continue
        return candidate
    return None


# LLM: _existing_parent_acceptance_repair_child keeps failed-test recovery idempotent.
# 函数用途: 同一个父级验收失败已派修复 run 时复用，避免每轮 watch 重复创建。
def _existing_parent_acceptance_repair_child(
    manager,
    source_run_id: str,
    signal: dict[str, object],
    target_refs: list[str],
):
    refs = target_refs or list(signal.get("artifact_refs", []) or [])
    target_ref_set = _normalized_ref_set(refs)
    for candidate in manager.list_runs():
        attrs = getattr(candidate, "attributes", {}) or {}
        if attrs.get("repair_kind") != "parent_acceptance":
            continue
        if attrs.get("repair_source_run_id") != source_run_id:
            continue
        if not _repair_child_is_reusable(candidate):
            continue
        candidate_refs = _normalized_ref_set(attrs.get("target_artifact_refs", []) or [])
        if not target_ref_set or not candidate_refs or target_ref_set == candidate_refs:
            return candidate
    return None


# LLM: idempotency follows an existing repair chain instead of spawning sibling repair work.
# 函数用途: repair child 被 BLOCKED/TAKEN_OVER/TIMEOUT 时应由自己的恢复链继续推进，父源任务不能再开同类 sibling。
def _repair_child_is_reusable(task) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    if status == "DONE" and verification == "VERIFIED":
        return True
    return status in {
        "PLANNING",
        "RUNNING",
        "AWAITING_ACCEPTANCE",
        "BLOCKED",
        "TAKEN_OVER",
        "TIMEOUT",
        "CHANNEL_ERROR",
    }


# LLM: _normalized_ref_set compares artifact refs as path facts without reading artifact bodies.
# 函数用途: 让幂等判断不受字符串空值或路径表现形式影响。
def _normalized_ref_set(values: object) -> set[str]:
    refs: set[str] = set()
    for item in values if isinstance(values, list) else []:
        text = str(item or "").strip()
        if text:
            refs.add(str(Path(text).resolve(strict=False)))
    return refs


# LLM: _merge_refs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _merge_refs(left: list[str], right: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*(left or []), *(right or [])]:
        text = str(item or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


# LLM: _artifact_repair_goal keeps repair worker scope refs-first and concrete.
# 函数用途: 用 run/task/output/artifact refs 生成修复任务目标，不复制产物正文进 prompt。
def _artifact_repair_goal(signal: dict[str, object]) -> str:
    artifacts = ", ".join(str(item) for item in signal.get("artifact_refs", []) or []) or "查看 output_ref"
    blockers = "; ".join(str(item) for item in signal.get("blockers", []) or []) or "查看 output_ref blockers"
    return (
        f"修复 run {signal.get('run_id', '')} 的产物结构失败。"
        f"先读取 task_ref={signal.get('task_ref', '')}、output_ref={signal.get('output_ref', '')}、"
        f"run_ref={signal.get('run_ref', '')}。"
        f"目标产物：{artifacts}。失败码：{blockers}。"
        "只修复列出的文件，例如去掉 href=\"#\" 占位链接、补齐缺失结构或截断内容；"
        "修复后读回文件并确认 HTML 结构闭合、没有 disabled/空链接/外部依赖。"
    )


# LLM: _parent_acceptance_repair_goal names exact failed refs and keeps the original goal in scope.
# 函数用途: 用验收/测试/followup 引用生成修复目标，不把产物正文塞进 prompt。
def _parent_acceptance_repair_goal(signal: dict[str, object], target_refs: list[str]) -> str:
    refs = "; ".join(
        str(signal.get(key) or "")
        for key in ("acceptance_ref", "test_ref", "followup_ref", "task_ref")
        if signal.get(key)
    )
    failures = "; ".join(str(item) for item in signal.get("failed_tests", []) or [])
    details = "; ".join(str(item) for item in signal.get("test_failure_details", []) or [])
    artifacts = "; ".join(str(item) for item in target_refs) or "查看 test_ref / output_json artifacts"
    original = str(signal.get("original_goal") or "")
    return (
        f"修复 run {signal.get('run_id', '')} 的父级验收失败。"
        f"先读取 refs：{refs or '查看任务 reports 目录'}。"
        f"失败测试/线索：{failures or details or signal.get('test_failure_summary', '')}。"
        f"目标产物：{artifacts}。"
        f"原始任务目标：{original}。"
        "只修复父级验收报告点名的问题，修复后读回产物并等待父级重新执行验收 tests。"
    )

# LLM: apply_record_only_action 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新only动作对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def apply_record_only_action(service, action, task, ctx: ActionHandlerContext):
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            f"已记录 {action.action} 待人工处理。",
        )
    )
