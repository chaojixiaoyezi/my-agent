# LLM: 被打断回合续跑时给用户看的提示（I4）。按 Gateway 恢复标记里的结构化 cause 选文案：TUI 在续跑边界（turn_resumed 事件）
#   显示、IM 在最终回复正文前（channel_delivery.host_notices）显示，两边共用这一张表，不各写一份。cause 只来自
#   gateway_parts.recovery 写下的 active_turn_recovery.cause，不读正文。改文案同步 test_tui_runtime 与 test_shutdown_turn_resume。
# 模块用途: 统一“这一轮被重启或超时打断、已自动续跑”的提示文案。
from __future__ import annotations

# 已知 cause 的提示；宿主停机准入拒绝的回合重启后按 gateway_restart / gateway_safe_restart 续跑，和进程消失的回合同一句话。
TURN_RESUMED_NOTICES = {
    "gateway_safe_restart": "Gateway 安全重启打断了这一轮，已自动续跑。",
    "gateway_restart": "Gateway 重启打断了这一轮，已自动续跑。",
    "processing_lease_expired": "这一轮执行超时中断，Gateway 已自动续跑。",
}
# 不认识的 cause 用的通用提示。
TURN_RESUMED_FALLBACK_NOTICE = "这一轮执行被打断，已自动续跑。"


# 函数用途: 按续跑原因取提示文案，不认识的原因给通用的一句。
def turn_resumed_notice_text(cause: object) -> str:
    return TURN_RESUMED_NOTICES.get(str(cause or "").strip(), TURN_RESUMED_FALLBACK_NOTICE)


__all__ = ["TURN_RESUMED_FALLBACK_NOTICE", "TURN_RESUMED_NOTICES", "turn_resumed_notice_text"]
