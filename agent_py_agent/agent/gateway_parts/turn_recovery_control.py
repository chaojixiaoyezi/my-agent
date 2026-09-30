# LLM: /recover 处理当前会话的两类未知执行轮，目标只来自运行库结构化投影，处置值来自已解析命令：
#   1. 当前会话工作任务（thread.workspace_task_id）的根主代理执行链，经 main_agent_recovery_block_for_task 定位；优先级最高，
#      行为与 O1 之前完全相同，写入口是 RuntimeRepository.recover_attempt_unknown。
#   2. 主链不阻塞时，本线程（thread.thread_id）未关 TaskRun 里被中断的子代理执行轮（runtime_db/child_recovery 只读投影）；
#      恰好一条才处置，多条拒绝并列清单（RUN_RECOVERY_REJECTED）。写入口是 child_recovery.recover_child_attempt_unknown（同一 CAS）；
#      之后子代理记录显示已被接替（TAKEN_OVER）才用 settle_taken_over_run 收成 cancelled，再按任务尝试 TaskRun 收口。
#   不改会话历史、不重放旧操作、不按提示词或最近任务猜目标、不做任何自动处置。改动时同步 test_turn_recovery_control.py
#   与 test_turn_recovery_child_unknown.py。
# 模块用途: 实现会话控制 /recover，查看阻塞本会话的未知执行轮（含本会话子代理留下的），并在用户显式确认处置后解除阻塞。
from __future__ import annotations

import time

from ..agent_core.runtime_mixin import settle_terminal_task_run_for_task
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from ..runtime_db.child_recovery import (
    ChildRecoveryTarget,
    recover_child_attempt_unknown,
    unknown_child_attempts_for_thread,
)
from ..runtime_db.operations import ATTEMPT_EFFECT_DISPOSITIONS, ATTEMPT_STATUS_UNKNOWN
from ..runtime_db.run_takeover import settle_taken_over_run_best_effort
from ..subagents.models import (
    TAKEOVER_DISPOSITION_TAKEN_OVER,
    TaskStatus,
    task_replacement_successor,
)

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
}
_NO_OPERATIONS = "没有记录到进行中的工具操作；执行者中途退出，结果无法自动确认。"


# LLM: owner_agent/thread 由控制面按已认证 scope 解析后传入；本函数不创建线程，只有 apply 写运行库。
#   主链阻塞优先；主链不阻塞时才轮到本线程子代理。thread 缺 thread_id 时子代理投影为空，行为与主链-only 时相同。
# 函数用途: 查看或恢复当前会话被 unknown 阻塞的执行；没有阻塞时返回无需恢复。
def execute_turn_recovery_control(
    owner_agent: object,
    thread: object | None,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    repo = getattr(getattr(owner_agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return ConversationControlResult("recover", True, _NO_BLOCK)
    task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    block = repo.main_agent_recovery_block_for_task(task_id) if task_id else None
    children = unknown_child_attempts_for_thread(repo, str(getattr(thread, "thread_id", "") or ""))
    if block is not None:
        return _execute_main_recovery(repo, block, command, len(children))
    if children:
        return _execute_child_recovery(owner_agent, repo, children, command)
    return ConversationControlResult("recover", True, _NO_BLOCK)


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


# LLM: 顺序固定：先 CAS 恢复（事务内复核线程范围），成功后才读子代理记录决定是否做接替收口，最后按任务尝试 TaskRun 收口。
#   接替收口与 TaskRun 收口都是尽力而为，失败不影响已落账的恢复。副作用：写 attempt_recovered，可能写 agent_run.completed
#   与 task_run.closed。
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
    successor = _taken_over_successor(_load_subagent(owner_agent, target.run_id))
    settled: dict[str, object] = {}
    if successor:
        settled = settle_taken_over_run_best_effort(repo, target.run_id, takeover_by=successor)
    settle_terminal_task_run_for_task(owner_agent, target.task_id)
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
    )


# LLM: 子代理记录是“是否已被接替”的唯一来源（status 与 takeover_by）；记录缺失或读坏返回 None，调用方按“接替情况未知”处理，
#   不能据此猜测接替关系。只读，不改记录。
# 函数用途: 读取某个子代理的持久记录；读不到时返回 None。
def _load_subagent(owner_agent: object, run_id: str) -> object | None:
    load = getattr(getattr(owner_agent, "subagents", None), "load", None)
    if not callable(load) or not run_id:
        return None
    try:
        return load(run_id)
    except Exception:  # noqa: BLE001 记录缺失或读坏只影响接替展示与收口，恢复本身以运行库为准
        return None


# LLM: 只有 status=TAKEN_OVER 且 takeover_by 有值才算“已被接替”（与 services/base.record_takeover 的收口条件同口径）；
#   已关闭来源只记 superseded_by 的情况不在这里收口。纯函数。
# 函数用途: 返回接替这个子代理的 run_id；没有被接替或记录读不到时返回空串。
def _taken_over_successor(task: object | None) -> str:
    if task is None or str(getattr(task, "status", "") or "").upper() != TaskStatus.TAKEN_OVER.value:
        return ""
    successor, disposition = task_replacement_successor(task)
    return successor if disposition == TAKEOVER_DISPOSITION_TAKEN_OVER else ""


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
# 函数用途: 生成子代理 /recover 查看结果；恰好一条时给出处置命令，多条时说明暂不支持逐条处置。
def _render_children(owner_agent: object, repo: object, children: list[ChildRecoveryTarget]) -> str:
    lines = ["本会话有子代理执行中断，结果未确认："]
    for target in children:
        lines.extend(_child_lines(owner_agent, repo, target))
    if len(children) == 1:
        lines.append("请先核对这些操作在外部是否已经生效，然后选择一种处置：")
        lines.extend(_disposition_lines())
        lines.append("处置只解除这个子代理的阻塞，系统不会自动重做这些操作；没被接替的子代理是否接着做由主代理决定。")
    else:
        lines.append(f"同时有 {len(children)} 个子代理待核对；/recover 目前一次只能处置唯一的一条，暂不支持指定目标，请查看运行诊断。")
    return "\n".join(lines)


# LLM: 多条时的拒绝文案只列编号和角色，不写库。
# 函数用途: 生成“多条子代理待核对、无法确定目标”的拒绝回复。
def _render_ambiguous(children: list[ChildRecoveryTarget]) -> str:
    lines = [f"没有恢复：同时有 {len(children)} 个子代理待核对，/recover 目前一次只能处置唯一的一条，暂不支持指定目标："]
    lines.extend(f"- 子代理 {target.run_id}（{target.role or '未知角色'}）" for target in children)
    lines.append("请查看运行诊断。")
    return "\n".join(lines)


# LLM: 每个子代理一行身份加接替情况，下面缩进列未确认操作；只读运行库与子代理记录。
# 函数用途: 生成单个子代理在查看结果里的几行说明。
def _child_lines(owner_agent: object, repo: object, target: ChildRecoveryTarget) -> list[str]:
    task = _load_subagent(owner_agent, target.run_id)
    successor = _taken_over_successor(task)
    if successor:
        takeover = f"已由 {successor} 接替"
    else:
        takeover = "接替情况未知（子代理记录读不到）" if task is None else "没有被接替"
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


# LLM: 处置值与说明只来自 ATTEMPT_EFFECT_DISPOSITIONS 与本模块标签表。
# 函数用途: 生成三种处置命令的说明行。
def _disposition_lines() -> list[str]:
    return [f"/recover {value}：{_DISPOSITION_LABELS.get(value, value)}" for value in ATTEMPT_EFFECT_DISPOSITIONS]


__all__ = ["execute_turn_recovery_control"]
