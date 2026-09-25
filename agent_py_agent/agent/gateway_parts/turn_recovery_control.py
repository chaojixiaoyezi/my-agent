# LLM: /recover 只处理当前会话工作任务（thread.workspace_task_id）的根主代理执行链；目标来自运行库的
# 结构化恢复投影，处置值来自已解析命令。唯一写入口是 RuntimeRepository.recover_attempt_unknown，
# 不改会话历史、不重放旧操作、不按提示词或最近任务猜目标。改动时同步 test_turn_recovery_control.py。
# 模块用途: 实现会话控制 /recover，查看阻塞本会话的未知执行轮，并在用户显式确认处置后解除阻塞。
from __future__ import annotations

import time

from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from ..runtime_db.operations import ATTEMPT_EFFECT_DISPOSITIONS, ATTEMPT_STATUS_UNKNOWN

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
}


# LLM: owner_agent/thread 由控制面按已认证 scope 解析后传入；本函数不创建线程，只有 apply 写运行库。
# 函数用途: 查看或恢复当前会话被 unknown 阻塞的执行；没有阻塞时返回无需恢复。
def execute_turn_recovery_control(
    owner_agent: object,
    thread: object | None,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    repo = getattr(getattr(owner_agent, "subagents", None), "runtime_db", None)
    block = repo.main_agent_recovery_block_for_task(task_id) if task_id and repo is not None else None
    if block is None:
        return ConversationControlResult("recover", True, _NO_BLOCK)
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
        operations = repo.unsettled_attempt_operations(attempt_id)
        return ConversationControlResult("recover", True, _render_block(operations))
    result = repo.recover_attempt_unknown(
        attempt_id,
        operator=_OPERATOR,
        effect_disposition=command.value,
        reason="用户在会话中用 /recover 显式确认处置",
    )
    if not result.get("recovered"):
        reason = str(result.get("reason") or "")
        return ConversationControlResult(
            "recover",
            False,
            "没有恢复：" + _REFUSAL_LABELS.get(reason, f"{reason or '未知原因'}，请查看运行诊断。"),
            error_code="RUN_RECOVERY_REJECTED",
        )
    return ConversationControlResult(
        "recover",
        True,
        f"已按「{_DISPOSITION_LABELS.get(command.value, command.value)}」解除阻塞。"
        "下一条消息会接着原任务继续；系统不会自动重做那些未确认的操作。",
    )


# LLM: 只渲染结构化字段（工具名、状态、开始时间），不读工具参数或结果正文，避免把命令原文和路径投到 IM。
# 函数用途: 生成 /recover 查看结果：列出未确认的操作和三种处置命令。
def _render_block(operations: list[dict[str, object]]) -> str:
    lines = ["上一轮执行中断，结果未确认，本会话已暂停自动执行。"]
    if operations:
        lines.append("未确认的操作：")
        for item in operations:
            started = float(item.get("handler_started_at") or 0)
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(started)) if started > 0 else "时间未知"
            status = _OPERATION_STATUS_LABELS.get(str(item.get("status") or ""), str(item.get("status") or ""))
            lines.append(f"- {item.get('operation_type') or '未知工具'}｜{status}｜开始于 {when}")
    else:
        lines.append("没有记录到进行中的工具操作；执行者中途退出，结果无法自动确认。")
    lines.append("请先核对这些操作在外部是否已经生效，然后选择一种处置：")
    lines.extend(f"/recover {value}：{_DISPOSITION_LABELS.get(value, value)}" for value in ATTEMPT_EFFECT_DISPOSITIONS)
    lines.append("三种处置都会解除阻塞，下一条消息接着原任务继续；系统不会自动重做这些操作。")
    return "\n".join(lines)


__all__ = ["execute_turn_recovery_control"]
