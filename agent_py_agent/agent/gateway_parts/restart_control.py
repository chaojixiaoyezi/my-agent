# LLM: /restart 只在已认证 scope 解析出的 owner 上执行，且 owner 必须是本机管理员（local/main，含已绑定管理员的 IM 身份）；
# 普通用户/群一律拒绝。命令只写一份安全重启请求，排空与换进程由 Gateway 服务主循环执行。不经过模型、不读正文。
# 改动须同步 test_gateway_safe_restart.py 与 CLI_REFERENCE.md 的 /restart 说明。
# 模块用途: 让管理员在 TUI 或 IM 里直接安排一次 Gateway 安全重启，不需要回到终端。
from __future__ import annotations

import os

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..user_space.approval_mode import is_permission_admin
from .paths import GatewayPaths
from .restart_service import submit_restart_request

_REFUSALS = {
    "cooldown": ("GATEWAY_RESTART_COOLDOWN", "刚完成过一次安全重启，请 {retry_after_seconds} 秒后再试。"),
    "loop_guard": ("GATEWAY_RESTART_LOOP_GUARD", "这个会话短时间内已多次安排重启，已暂停接受新的重启请求。"),
}


# LLM: owner_agent 与 thread 由 control_service 按 scope 解析；paths 是执行本命令的 Gateway 的权威队列根，
# 目标进程就是这个 Gateway 进程。不能按 owner home 重新推导 Gateway 路径。
# 函数用途: 校验管理员身份后提交安全重启请求，并把排队/冷却/防循环结果告诉用户；会写重启请求文件。
def execute_restart_control(
    owner_agent: object,
    thread: object | None,
    command: ConversationControlCommand,
    *,
    paths: GatewayPaths,
) -> ConversationControlResult:
    home = getattr(owner_agent, "home_paths", None)
    if not is_permission_admin(home):
        return ConversationControlResult(
            "restart", False, "只有管理员可以重启 Gateway。", error_code="GATEWAY_RESTART_ADMIN_ONLY",
        )
    config = getattr(owner_agent, "config", None)
    reason = str(command.value or "").strip() or "管理员使用 /restart"
    result = submit_restart_request(
        paths,
        target_pid=os.getpid(),
        requester={
            "kind": "control_command",
            "owner_provider": str(getattr(home, "owner_provider", "") or ""),
            "owner_kind": str(getattr(home, "owner_kind", "") or ""),
            "owner_id": str(getattr(home, "owner_id", "") or ""),
            "thread_id": str(getattr(thread, "thread_id", "") or ""),
        },
        reason=reason,
        cooldown_seconds=float(getattr(config, "gateway_restart_cooldown_seconds", 30) or 0),
    )
    status = str(result.get("status") or "")
    if status in _REFUSALS:
        code, template = _REFUSALS[status]
        return ConversationControlResult("restart", False, template.format(**result), error_code=code)
    wait = int(getattr(config, "gateway_restart_turn_wait_seconds", 300) or 0)
    prefix = "已合并到正在排队的安全重启" if status == "coalesced" else "已安排安全重启"
    return ConversationControlResult(
        "restart",
        True,
        f"{prefix}：Gateway 会先停领新请求、等在跑的回合结束（最多约 {wait} 秒），再等执行中的工具跑完后换新进程。"
        "期间发出的消息会排队，重启后自动处理；TUI 和 IM 会自动重连。",
    )


__all__ = ["execute_restart_control"]
