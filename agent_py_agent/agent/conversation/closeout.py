"""模块用途: 收口状态机（owner 四改之 2，docs/design/closeout_state_machine.md）。

工具循环停下后,"下一步是什么"由 decide_closeout 这一个纯函数决定——CLI
resume_loop 与 gateway 调度器共用, 停止原因采集方(_final_response_after_*)
只报因不再各自决定命运。白名单 gate(should_continue_task)保持原处, 其判定
结果以 continuable 事实输入; goal 授权(EXEC-39)以 active_goal 事实输入;
本模块不读 store、不读 policy、不发事件、不改状态——纯组合器, 便于穷举
单测与两侧复用。改动契约: 新增事实/终态必须同步更新 truth table 测试与
DESIGN_LEDGER 摘要; 删除本模块前先确认 resume 能力(goal/cron 模式)仍有
权威决策点。
"""
from __future__ import annotations

from dataclasses import dataclass

# 终态集合(与设计文档 §3 一致, 一字不多)。
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATE_WAIT_HUMAN = "wait_human"
STATE_WAIT_HANDOFF = "wait_handoff"
STATE_RESUME_ROUND = "resume_round"
STATE_SLEEP_WAIT = "sleep_wait"

# EXEC-30: 同一收口 reason 连续续跑上限(对照 会话运行时/轻量运行时 写完即测即收的
# 天然收敛机制的机制等价物)。同因 3 次仍不收敛=模型在同一模式无限迭代,
# 强制收口移交。
SAME_REASON_RESUME_LIMIT = 3


# LLM: 机器输入事实——全部结构化, 由调用方从权威来源(policy/load_goal/
# should_continue_task/收口 result)读好后传入; 本模块不做任何读取。
# 函数用途: 给 decide_closeout 打包一轮收口后的事实快照。
@dataclass(frozen=True)
class CloseoutFacts:
    runtime_status: str
    runtime_reason: str
    runtime_source: str
    continuable: bool
    active_goal: bool
    resume_budget_left: int
    same_reason_streak: int
    rounds: int
    max_rounds: int
    # 模型本轮最后动作是 clock.sleep 且成功落字条(工具循环结构化判定,
    # 不与 reason 字符串重复推导): 收口走 sleep_wait 而不是 done/wait_human。
    sleeping: bool = False


# LLM: 机器输出——state 是唯一命运判定, guidance_key 是唯一承诺文案选择键
# (见 _final_response_after_* 改造步骤 4)。2026-08-17 owner 拍板删除 gateway
# 移交线后不再有 handoff 标志: 预算/同因/轮数耗尽只是"停, 等用户继续"。
# 函数用途: decide_closeout 的返回值, 调用方按 state 执行、不另做分支判断。
@dataclass(frozen=True)
class CloseoutOutcome:
    state: str
    reason: str
    guidance_key: str


# LLM: 收口判定单一权威。规则顺序即优先级: 终态 > 白名单 > goal 授权 >
# 收敛护栏(同因/预算/轮数)。语义必须与 run_with_resume/run_manual_resume
# 现有行为一一对应(行为等价迁移, 不是新语义); 2026-08-17 owner 拍板删除
# gateway 移交线后机器只服务 CLI 两条线; 任何规则变更要同步
# docs/design/closeout_state_machine.md。
# 函数用途: 输入一轮收口后的事实, 输出下一步终态; 纯函数, 无副作用。
def decide_closeout(facts: CloseoutFacts) -> CloseoutOutcome:
    status = str(facts.runtime_status or "").strip().lower()
    reason = str(facts.runtime_reason or "").strip()
    if status == "ok":
        return CloseoutOutcome(STATE_DONE, "", "done")
    if status == "cancelled":
        return CloseoutOutcome(STATE_CANCELLED, reason or "user_stop", "cancelled")
    # 模型主动 sleep 收口(调度改造 2b): 任务保持非终态等闹钟, 不标 done,
    # 也不进续跑族(否则 CLI 立刻再开一轮, 睡觉作废); wake_queue 到期由
    # 调度器唤醒。用户中断(cancelled)优先级更高, 先杀闹钟。
    if facts.sleeping:
        return CloseoutOutcome(STATE_SLEEP_WAIT, "clock_sleep", "sleep_wait")
    # 不可续跑族(blocked/协议违规/UNKNOWN 副作用等): 等用户显式继续, 不自动跑。
    if not facts.continuable:
        return CloseoutOutcome(
            STATE_WAIT_HUMAN, reason or "not_continuable", "wait_human"
        )
    # 可续跑族, 但正常不自动续跑(EXEC-39): 无 active goal → 停即停
    # , 用户 run --resume 手动继续。2026-08-17 owner 拍板
    # 删除 gateway 移交线——不再有"写单供调度器接管"这一步, 任务停下
    # 后唯一续跑入口是用户显式 run --resume(或后续 goal/cron 模式按
    # 持久事实新建的驱动)。
    if not facts.active_goal:
        return CloseoutOutcome(
            STATE_WAIT_HANDOFF, "no_active_goal", "wait_handoff"
        )
    # EXEC-30 同因收敛: 连续同因达上限强制收口移交, 不再续跑。
    if int(facts.same_reason_streak or 0) >= SAME_REASON_RESUME_LIMIT:
        return CloseoutOutcome(
            STATE_WAIT_HANDOFF,
            "same_reason_resume_limit_reached",
            "wait_handoff",
        )
    # 预算闸(resume_limit, policy 单一权威): 无剩余自动续跑次数 → 移交。
    # 语义: >0=剩余次数; ==0=耗尽; <0=无预算概念(不限, 手动续跑/无 policy)。
    if int(facts.resume_budget_left or 0) == 0:
        return CloseoutOutcome(
            STATE_WAIT_HANDOFF, "resume_limit_reached", "wait_handoff"
        )
    # 进程内护栏(max_rounds, 总代数上限含首轮): 已达上限 → 移交。
    if int(facts.rounds or 0) >= int(facts.max_rounds or 0):
        return CloseoutOutcome(
            STATE_WAIT_HANDOFF, "max_rounds_reached", "wait_handoff"
        )
    # 全部护栏通过且 goal 授权 → 自动续下一轮(goal-round-driver 同款)。
    return CloseoutOutcome(STATE_RESUME_ROUND, reason or "continuable", "resume_round")
