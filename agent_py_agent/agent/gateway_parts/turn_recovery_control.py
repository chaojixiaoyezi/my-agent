# LLM: /recover 处理当前会话与管理员 owner 历史的未知执行轮，目标只来自运行库结构化投影，处置值来自已解析命令：
#   1. 当前会话工作任务（thread.workspace_task_id）的根主代理执行链，经 main_agent_recovery_block_for_task 定位；优先级最高，
#      行为与 O1 之前完全相同，写入口是 RuntimeRepository.recover_attempt_unknown。
#   2. 主链不阻塞时，本线程（thread.thread_id）未关 TaskRun 里被中断的子代理执行轮（runtime_db/child_recovery 只读投影）；
#      不带编号时保持“恰好一条才处置”，带编号时由写事务精确复核范围与 unknown 状态，不使用查看快照写库；
#      之后子代理记录显示已被接替（TAKEN_OVER）才用 settle_taken_over_run 收成 cancelled，再按任务尝试 TaskRun 收口。
#   3. /recover owner 仅完整可信 local/main 管理员可用；只读列出无 thread 历史 unknown，处置须先预览并用绑定完整目标集合的确认码。
#   不改会话历史、不重放旧操作、不按提示词或最近任务猜目标、不做任何自动处置。改动时同步三份 recover 测试。
#   分层边界（scripts/check_import_boundaries.py）不允许本层导入 agent_core 与 subagents：接替判定只经
#   owner_agent.subagents.taken_over_successor，TaskRun 收口调用会话层 conversation/task_run_closeout.settle_terminal_task_run
#   （与执行收口边同一个函数）。
# 模块用途: 实现会话控制 /recover，查看阻塞本会话的未知执行轮（含本会话子代理留下的），并在用户显式确认处置后解除阻塞。
from __future__ import annotations

import time

from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from ..conversation.task_run_closeout import settle_terminal_task_run
from ..runtime_db.child_recovery import (
    ChildRecoveryTarget,
    ThreadRecoveryRequest,
    recover_child_attempt_unknown,
    recover_thread_attempt_unknown,
    unknown_child_attempts_for_thread,
)
from ..runtime_db.operations import ATTEMPT_EFFECT_DISPOSITIONS, ATTEMPT_STATUS_UNKNOWN
from ..runtime_db.owner_recovery import (
    OwnerRecoveryRequest,
    OwnerRecoveryTarget,
    owner_recovery_confirmation,
    owner_unknown_attempts,
    recover_owner_unknown_attempts,
)
from ..runtime_db.run_takeover import settle_taken_over_run_best_effort
from ..user_space.owner_access import is_complete_local_admin_owner

_OPERATOR = "conversation-control:/recover"
_NO_BLOCK = "当前会话没有等待核对的执行，无需恢复。"
_DISPOSITION_LABELS = {
    "recorded": "已核实生效并记下",
    "confirmed_noop": "已核实没有生效",
    "abandoned": "不再核对，接受未知后果",
}
_OPERATION_STATUS_LABELS = {
    "EXECUTING": "执行中断，结果未回传",
    "UNKNOWN": "结果未知",
    "CLAIMED": "已启动，结果未回传",
}
_REFUSAL_LABELS = {
    "not_unknown": "这条执行的状态刚刚变化，请重新输入 /recover 查看。",
    "not_current_attempt": "执行记录已经换代，请重新输入 /recover 查看。",
    "no_such_attempt": "找不到对应的执行记录，请查看运行诊断。",
    "target_changed": "这条子代理执行的状态刚刚变化，请重新输入 /recover 查看。",
    "target_not_found": "找不到这个编号，请重新输入 /recover 查看。",
    "target_not_unknown": "这个编号的执行轮已不再是结果未知，请重新输入 /recover 查看。",
    "target_out_of_scope": "这个编号不在本线程未关闭的 TaskRun 范围内。",
    "target_set_changed": "owner 历史待恢复目标集合已经变化，请重新预览并使用新的确认码。",
}
_NO_OPERATIONS = "没有记录到进行中的工具操作；执行者中途退出，结果无法自动确认。"
_OWNER_ADMIN_REQUIRED = "只有完整可信的本机 local/main 管理员身份能查看和处置 owner 历史未知执行轮。"
_OWNER_NO_TARGETS = "本 owner 没有不挂会话线程、等待人工核对的历史未知执行轮。"


# LLM: owner_agent/thread 由控制面按已认证 scope 解析后传入；本函数不创建线程，只有 apply/owner_apply 写运行库。
#   owner 命令先按完整可信身份分流；线程命令带编号时直接进写事务复核，不消费锁外候选快照。
#   不带编号仍保持主链阻塞优先、主链不阻塞才轮到子代理的旧语义。
# 函数用途: 查看或恢复当前会话被 unknown 阻塞的执行；没有阻塞时返回无需恢复。
def execute_turn_recovery_control(
    owner_agent: object,
    thread: object | None,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    repo = getattr(getattr(owner_agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return ConversationControlResult("recover", True, _NO_BLOCK)
    if command.operation.startswith("owner_"):
        return _execute_owner_recovery(owner_agent, repo, command)
    thread_id = str(getattr(thread, "thread_id", "") or "")
    if command.target_id:
        return _execute_targeted_recovery(owner_agent, repo, thread_id, command)
    task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    block = repo.main_agent_recovery_block_for_task(task_id) if task_id else None
    children = unknown_child_attempts_for_thread(repo, thread_id)
    if block is not None:
        return _execute_main_recovery(repo, block, command, len(children))
    if children:
        return _execute_child_recovery(owner_agent, repo, children, command)
    return ConversationControlResult("recover", True, _NO_BLOCK)


# LLM: 目标编号只来自已通过 opaque 校验的解析字段；写入口在锁内复核 thread/open TaskRun/current unknown，
#   所以这里不先按查看清单挑目标。成功后才做原有子代理接替与 TaskRun 尽力收口。
# 函数用途: 执行 `/recover <处置> <编号>` 的线程内精确恢复。
def _execute_targeted_recovery(
    owner_agent: object,
    repo: object,
    thread_id: str,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    request = ThreadRecoveryRequest(thread_id, command.target_id, command.value, _OPERATOR)
    result = recover_thread_attempt_unknown(repo, request)
    if not result.get("recovered"):
        return _refused(result)
    if result.get("recovery_target") != "child_agent_run":
        label = _DISPOSITION_LABELS.get(command.value, command.value)
        return ConversationControlResult("recover", True, f"已按「{label}」解除执行 {command.target_id} 的阻塞。")
    target = ChildRecoveryTarget(
        agent_run_id=str(result.get("agent_run_id") or ""), run_id=str(result.get("run_id") or ""),
        role=str(result.get("role") or ""), attempt_id=str(result.get("attempt_id") or ""),
        task_run_id=str(result.get("task_run_id") or ""), task_id=str(result.get("task_id") or ""),
        thread_id=thread_id,
    )
    return _complete_child_recovery(owner_agent, repo, target, command.value)


# LLM: owner 历史入口先用与 /settings 共用的完整 local/main 身份规则授权；查看和预览只读，只有带确认码的 owner_apply 写库。
# 函数用途: 分派管理员 owner 历史 unknown 的查看、预览与确认处置。
def _execute_owner_recovery(
    owner_agent: object,
    repo: object,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    home = getattr(owner_agent, "home_paths", None)
    if not is_complete_local_admin_owner(home):
        return ConversationControlResult(
            "recover", False, _OWNER_ADMIN_REQUIRED, error_code="RUN_RECOVERY_REJECTED",
            details={"scope": "owner", "reason": "admin_required"},
        )
    owner_id = str(getattr(home, "owner_id", "") or "")
    if command.operation == "owner_apply":
        return _apply_owner_recovery(repo, owner_id, command)
    targets = owner_unknown_attempts(repo, owner_id)
    if command.operation == "owner_preview":
        return _preview_owner_recovery(owner_id, targets, command.value)
    return ConversationControlResult(
        "recover", True, _render_owner_targets(targets),
        details={"scope": "owner", "target_count": len(targets)},
    )


# LLM: 预览确认码只由宿主对 owner、处置和完整目标集合生成；零目标时不发可提交的确认码。
# 函数用途: 返回 owner 历史恢复的只读预览和下一步确认命令。
def _preview_owner_recovery(
    owner_id: str,
    targets: list[OwnerRecoveryTarget],
    disposition: str,
) -> ConversationControlResult:
    if not targets:
        return ConversationControlResult(
            "recover", True, _OWNER_NO_TARGETS,
            details={"scope": "owner", "target_count": 0},
        )
    code = owner_recovery_confirmation(owner_id, disposition, targets)
    message = _render_owner_targets(targets)
    message += f"\n确认目标无误后输入：/recover owner {disposition} --confirm {code}"
    return ConversationControlResult(
        "recover", True, message,
        details={
            "scope": "owner", "target_count": len(targets), "confirmation_code": code,
            "target_ids": [target.target_id for target in targets],
        },
    )


# LLM: 写入口自己在 BEGIN IMMEDIATE 中重读目标并校验确认码；服务层只投影固定统计字段，不能把内部异常或正文带到 IM。
# 函数用途: 执行已确认的 owner 历史恢复并返回结构化计数回执。
def _apply_owner_recovery(
    repo: object,
    owner_id: str,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    outcome = recover_owner_unknown_attempts(
        repo, OwnerRecoveryRequest(owner_id, command.value, command.confirmation_code),
    )
    details = _owner_outcome_details(outcome, command.confirmation_code)
    if not outcome.get("recovered"):
        return ConversationControlResult(
            "recover", False, "没有恢复：" + _REFUSAL_LABELS.get(
                str(outcome.get("reason") or ""), "恢复条件不再成立，请重新预览。",
            ), error_code="RUN_RECOVERY_REJECTED", details=details,
        )
    prefix = "该确认已处理过" if outcome.get("idempotent") else "owner 历史未知执行轮已处理"
    return ConversationControlResult(
        "recover", True,
        f"{prefix}：目标 {details['target_count']}，成功 {details['success_count']}，跳过 {details['skipped_count']}。",
        details=details,
    )


# LLM: 成功回执字段固定，测试和 TUI/IM 不应依赖 helper 的内部 recovered 标志；拒绝时只额外公开稳定 reason 码。
# 函数用途: 把 owner 写入口结果收窄为跨进程结构化回执。
def _owner_outcome_details(outcome: dict[str, object], confirmation_code: str) -> dict[str, object]:
    details: dict[str, object] = {
        "scope": "owner", "target_count": int(outcome.get("target_count") or 0),
        "success_count": int(outcome.get("success_count") or 0),
        "skipped_count": int(outcome.get("skipped_count") or 0),
        "reason_counts": dict(outcome.get("reason_counts") or {}),
        "confirmation_code": str(outcome.get("confirmation_code") or confirmation_code),
        "idempotent": bool(outcome.get("idempotent")),
    }
    if outcome.get("reason"):
        details["reason"] = str(outcome["reason"])
    return details


# LLM: owner 列表只显示投影中的编号、根/子角色、开始时间和未确认操作数，不读取任务标题、目标或会话正文。
# 函数用途: 渲染 owner 历史 unknown 的只读目标清单。
def _render_owner_targets(targets: list[OwnerRecoveryTarget]) -> str:
    if not targets:
        return _OWNER_NO_TARGETS
    lines = [f"本 owner 有 {len(targets)} 个不挂会话线程的历史未知执行轮："]
    lines.extend(_owner_target_line(target) for target in targets)
    return "\n".join(lines)


# LLM: 时间仅由 started_at 数值格式化；agent_kind 只接受投影的 root/child，不从 role 文案猜类型。
# 函数用途: 把一条 owner 历史目标排成单行。
def _owner_target_line(target: OwnerRecoveryTarget) -> str:
    kind = "根代理" if target.agent_kind == "root" else "子代理"
    started = time.strftime("%m-%d %H:%M:%S", time.localtime(target.started_at)) if target.started_at > 0 else "时间未知"
    return (
        f"- 编号 {target.target_id}｜{kind} {target.role or '未知角色'}｜开始于 {started}｜"
        f"未确认操作 {target.unsettled_operation_count}"
    )


# LLM: 主链分支保持 O1 之前的语义与文案；pending_children 只在查看时追加一句提示，apply 仍只作用于主链 current attempt。
# 函数用途: 查看或恢复根主代理的未知执行轮，并提示还有多少子代理在排队等待核对。
def _execute_main_recovery(
    repo: object,
    block: dict[str, str],
    command: ConversationControlCommand,
    pending_children: int,
) -> ConversationControlResult:
    if block.get("attempt_status") != ATTEMPT_STATUS_UNKNOWN:
        return ConversationControlResult(
            "recover",
            False,
            f"这条执行链的状态为 {block.get('run_status')}/{block.get('attempt_status')}，"
            "不是执行中断造成的未知执行轮，/recover 无法处理；请查看运行诊断。",
            error_code="RUN_RECOVERY_REJECTED",
        )
    attempt_id = str(block.get("attempt_id") or "")
    if command.operation != "apply":
        message = _render_block(repo.unsettled_attempt_operations(attempt_id))
        if pending_children:
            message += f"\n另有 {pending_children} 个子代理执行中断待核对；主链解除阻塞后再输入 /recover 查看。"
        return ConversationControlResult("recover", True, message)
    result = repo.recover_attempt_unknown(
        attempt_id,
        operator=_OPERATOR,
        effect_disposition=command.value,
        reason="用户在会话中用 /recover 显式确认处置",
    )
    if not result.get("recovered"):
        return _refused(result)
    return ConversationControlResult(
        "recover",
        True,
        f"已按「{_DISPOSITION_LABELS.get(command.value, command.value)}」解除阻塞。"
        "下一条消息会接着原任务继续；系统不会自动重做那些未确认的操作。",
    )


# LLM: 子代理分支：查看只读；apply 只在恰好一条时执行，多条时拒绝且不写库（没有目标参数，不能替用户挑）。
# 函数用途: 查看或处置本线程子代理留下的未知执行轮。
def _execute_child_recovery(
    owner_agent: object,
    repo: object,
    children: list[ChildRecoveryTarget],
    command: ConversationControlCommand,
) -> ConversationControlResult:
    if command.operation != "apply":
        return ConversationControlResult("recover", True, _render_children(owner_agent, repo, children))
    if len(children) != 1:
        return ConversationControlResult(
            "recover", False, _render_ambiguous(children), error_code="RUN_RECOVERY_REJECTED",
        )
    return _recover_single_child(owner_agent, repo, children[0], command.value)


# LLM: 顺序固定：先 CAS 恢复（事务内复核线程范围），成功后才经 owner_agent.subagents.taken_over_successor 决定是否做接替收口，
#   最后用会话层 settle_terminal_task_run 按任务尝试 TaskRun 收口（按根主代理判定）。接替收口与 TaskRun 收口都是尽力而为，
#   失败不影响已落账的恢复。副作用：写 attempt_recovered，可能写 agent_run.completed 与 task_run.closed。
# 函数用途: 恢复唯一一条子代理未知执行轮，并把已被接替的来源和整棵任务执行总账顺带收口。
def _recover_single_child(
    owner_agent: object,
    repo: object,
    target: ChildRecoveryTarget,
    disposition: str,
) -> ConversationControlResult:
    result = recover_child_attempt_unknown(repo, target, disposition, _OPERATOR)
    if not result.get("recovered"):
        return _refused(result)
    return _complete_child_recovery(owner_agent, repo, target, disposition)


# LLM: 精确编号和旧“唯一子代理”路径完成 CAS 后共用同一收口；本函数不再写 attempt 状态，避免编号路径重复恢复。
#   接替收口与 TaskRun 收口仍是尽力而为，失败不回滚已经落账的 unknown→recovered。
# 函数用途: 完成已恢复子代理的接替收口、TaskRun 收口和用户回执。
def _complete_child_recovery(
    owner_agent: object,
    repo: object,
    target: ChildRecoveryTarget,
    disposition: str,
) -> ConversationControlResult:
    successor = owner_agent.subagents.taken_over_successor(target.run_id) or ""
    settled: dict[str, object] = {}
    if successor:
        settled = settle_taken_over_run_best_effort(repo, target.run_id, takeover_by=successor)
    settle_terminal_task_run(repo, getattr(owner_agent, "conversation_store", None), task_id=target.task_id)
    lines = [f"已按「{_DISPOSITION_LABELS.get(disposition, disposition)}」解除子代理 {target.run_id} 的阻塞。"]
    if successor:
        tail = "，运行记录已收口为取消。" if settled.get("settled") else "。"
        lines.append(f"它已由 {successor} 接替{tail}")
    else:
        lines.append("它没有被接替，是否让它接着做由主代理决定。")
    lines.append("系统不会自动重做那些未确认的操作。")
    return ConversationControlResult("recover", True, "".join(lines))


# LLM: 拒绝原因只来自写入口返回的结构化 reason；未登记的 reason 原样带出并指向运行诊断。
# 函数用途: 把恢复写入口的拒绝结果转成统一的 /recover 拒绝回复。
def _refused(result: dict[str, object]) -> ConversationControlResult:
    reason = str(result.get("reason") or "")
    return ConversationControlResult(
        "recover",
        False,
        "没有恢复：" + _REFUSAL_LABELS.get(reason, f"{reason or '未知原因'}，请查看运行诊断。"),
        error_code="RUN_RECOVERY_REJECTED",
        details={"reason": reason or "unknown_reason"},
    )


# LLM: 只渲染结构化字段（工具名、状态、开始时间），不读工具参数或结果正文，避免把命令原文和路径投到 IM。
# 函数用途: 生成主链 /recover 查看结果：列出未确认的操作和三种处置命令。
def _render_block(operations: list[dict[str, object]]) -> str:
    lines = ["上一轮执行中断，结果未确认，本会话已暂停自动执行。"]
    if operations:
        lines.append("未确认的操作：")
        lines.extend(_operation_line(item) for item in operations)
    else:
        lines.append(_NO_OPERATIONS)
    lines.append("请先核对这些操作在外部是否已经生效，然后选择一种处置：")
    lines.extend(_disposition_lines())
    lines.append("三种处置都会解除阻塞，下一条消息接着原任务继续；系统不会自动重做这些操作。")
    return "\n".join(lines)


# LLM: 同样只渲染结构化字段；子代理编号用运行库的 run_id，接替情况来自子代理记录，读不到就明说未知。
# 函数用途: 生成子代理 /recover 查看结果；一条保留简写，多条给出带编号的逐条处置语法。
def _render_children(owner_agent: object, repo: object, children: list[ChildRecoveryTarget]) -> str:
    lines = ["本会话有子代理执行中断，结果未确认："]
    for target in children:
        lines.extend(_child_lines(owner_agent, repo, target))
    if len(children) == 1:
        lines.append("请先核对这些操作在外部是否已经生效，然后选择一种处置：")
        lines.extend(_disposition_lines())
        lines.append("处置只解除这个子代理的阻塞，系统不会自动重做这些操作；没被接替的子代理是否接着做由主代理决定。")
    else:
        lines.append(f"同时有 {len(children)} 个子代理待核对；请先核对后用下面格式逐条处置：")
        lines.extend(_disposition_lines("<编号>"))
    return "\n".join(lines)


# LLM: 多条时不带编号仍拒绝，文案只列编号和角色并指向新精确语法，不写库。
# 函数用途: 生成“多条子代理待核对、必须指定编号”的拒绝回复。
def _render_ambiguous(children: list[ChildRecoveryTarget]) -> str:
    lines = [f"没有恢复：同时有 {len(children)} 个子代理待核对，不带编号无法确定目标："]
    lines.extend(f"- 子代理 {target.run_id}（{target.role or '未知角色'}）" for target in children)
    lines.append("请用 /recover <处置> <编号> 逐条处置，例如 /recover recorded <编号>。")
    return "\n".join(lines)


# LLM: 每个子代理一行身份加接替情况，下面缩进列未确认操作；只读运行库与子代理记录（经 owner_agent.subagents）。
#   taken_over_successor 返回 None 表示记录读不到，空串表示没被接替。
# 函数用途: 生成单个子代理在查看结果里的几行说明。
def _child_lines(owner_agent: object, repo: object, target: ChildRecoveryTarget) -> list[str]:
    successor = owner_agent.subagents.taken_over_successor(target.run_id)
    if successor:
        takeover = f"已由 {successor} 接替"
    else:
        takeover = "接替情况未知（子代理记录读不到）" if successor is None else "没有被接替"
    lines = [f"- 子代理 {target.run_id}（{target.role or '未知角色'}）｜{takeover}"]
    operations = repo.unsettled_attempt_operations(target.attempt_id)
    lines.extend("  " + _operation_line(item) for item in operations)
    if not operations:
        lines.append("  " + _NO_OPERATIONS)
    return lines


# LLM: 单条操作的展示口径，主链和子代理共用；开始时间为 0 时显示“时间未知”。
# 函数用途: 把一条未确认操作渲染成“工具｜状态｜开始时间”。
def _operation_line(item: dict[str, object]) -> str:
    started = float(item.get("handler_started_at") or 0)
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(started)) if started > 0 else "时间未知"
    status = _OPERATION_STATUS_LABELS.get(str(item.get("status") or ""), str(item.get("status") or ""))
    return f"- {item.get('operation_type') or '未知工具'}｜{status}｜开始于 {when}"


# LLM: 处置值与说明只来自 ATTEMPT_EFFECT_DISPOSITIONS 与本模块标签表；可选编号只用于展示命令格式。
# 函数用途: 生成三种处置命令的说明行，必要时附上编号占位符。
def _disposition_lines(target_id: str = "") -> list[str]:
    suffix = f" {target_id}" if target_id else ""
    return [f"/recover {value}{suffix}：{_DISPOSITION_LABELS.get(value, value)}"
            for value in ATTEMPT_EFFECT_DISPOSITIONS]


__all__ = ["execute_turn_recovery_control"]
