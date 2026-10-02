# LLM: 被打断回合续跑时给用户看的提示（I4）。按 Gateway 恢复标记里的结构化 cause 选文案：TUI 在续跑边界（turn_resumed 事件）
#   显示、IM 在最终回复正文前（channel_delivery.host_notices）显示，两边共用这一张表，不各写一份。cause 只来自
#   gateway_parts.recovery 写下的 active_turn_recovery.cause，不读正文。改文案同步 test_tui_runtime 与 test_shutdown_turn_resume。
#   续跑次数用完（TURN_RESUME_LIMIT_EXCEEDED）时的三句也在这里，按收口时插话结算的结构化计数选（turn_resume_limit_notice），
#   recovery 把它写进失败结果的 user_error/error，TUI 和 IM 都从失败结果显示；改它同步 test_turn_resume_limit 与
#   test_turn_resume_limit_steer。
# 模块用途: 统一“这一轮被重启或超时打断、已自动续跑”和“打断太多次、已停止自动续跑”的提示文案。
from __future__ import annotations

# 已知 cause 的提示；宿主停机准入拒绝的回合重启后按 gateway_restart / gateway_safe_restart 续跑，和进程消失的回合同一句话。
TURN_RESUMED_NOTICES = {
    "gateway_safe_restart": "Gateway 安全重启打断了这一轮，已自动续跑。",
    "gateway_restart": "Gateway 重启打断了这一轮，已自动续跑。",
    "processing_lease_expired": "这一轮执行超时中断，Gateway 已自动续跑。",
}
# 不认识的 cause 用的通用提示。
TURN_RESUMED_FALLBACK_NOTICE = "这一轮执行被打断，已自动续跑。"
# 同一回合因非计划重启自动续跑的次数用完、又被打断时的提示（3a 10-02 定的原话）；“继续”是新回合，不受上限影响。
TURN_RESUME_LIMIT_NOTICE = "这一轮被打断太多次，已停止自动续跑；发‘继续’可以接着做。"
# 同上，但收口时用户补充的话被拒收、会由入口回执排成新的一轮马上跑（3a 10-02 定的原话）。
TURN_RESUME_LIMIT_BACKUP_NOTICE = "这一轮被打断太多次，已停止自动续跑；你补充的话会作为新的一轮马上处理。"
# 同上，但用户补充的话已经写进会话历史、不会再单独跑一轮，发“继续”时和原任务一起处理。
TURN_RESUME_LIMIT_RECORDED_NOTICE = "这一轮被打断太多次，已停止自动续跑；你补充的话已记在会话里，发‘继续’会一起处理。"


# LLM: 只按 cause 查表，不认识的给通用句；TUI 续跑边界和 IM 宿主提示都调它，别在调用方各写一份文案。
# 函数用途: 按续跑原因取提示文案，不认识的原因给通用的一句。
def turn_resumed_notice_text(cause: object) -> str:
    return TURN_RESUMED_NOTICES.get(str(cause or "").strip(), TURN_RESUMED_FALLBACK_NOTICE)


# LLM: 只看收口时插话结算的两项计数（reject_pending 的 backup_turns、recorded_in_transcript），不读正文。有备用下一轮时优先说
#   “会作为新的一轮马上处理”：那一轮带着同一会话历史跑，已记在历史里的补充也会被看到；都没有时用原句。
# 函数用途: 按插话怎么收口选续跑上限提示的那句话。
def turn_resume_limit_notice(*, backup_turns: int, recorded_in_transcript: int) -> str:
    if backup_turns > 0:
        return TURN_RESUME_LIMIT_BACKUP_NOTICE
    if recorded_in_transcript > 0:
        return TURN_RESUME_LIMIT_RECORDED_NOTICE
    return TURN_RESUME_LIMIT_NOTICE


__all__ = [
    "TURN_RESUMED_FALLBACK_NOTICE",
    "TURN_RESUMED_NOTICES",
    "TURN_RESUME_LIMIT_BACKUP_NOTICE",
    "TURN_RESUME_LIMIT_NOTICE",
    "TURN_RESUME_LIMIT_RECORDED_NOTICE",
    "turn_resume_limit_notice",
    "turn_resumed_notice_text",
]
