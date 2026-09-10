# LLM: CLI 续跑只由既有结构化 Goal 和显式 resume 控制；名称整理不能改变授权、收口或恢复链。
# 模块用途: 执行已授权的 Goal 接续和用户恢复，本轮仅更新说明，不新增后台轮询或模型调用。
"""CLI Goal 续跑与用户显式 resume 执行器。

普通 ``run`` 只执行一轮；只有 exact active Goal 才能在进程内进入下一轮。
用户 ``run --resume`` 则是显式控制命令，沿同一 task/run/thread 恢复。

Goal 续跑由同因收敛和 max_rounds 护栏限制；不再创建普通任务 progress
policy，也没有 CLI/Gateway 为普通任务抢同一 policy 的旁路。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..agent.agent_core.cli_run_conversation import bind_cli_run_conversation
from ..agent.agent_core.runtime.loop_models import RunParams
from ..agent.conversation.closeout import (
    STATE_DONE,
    STATE_RESUME_ROUND,
    STATE_WAIT_HANDOFF,
    CloseoutFacts,
    decide_closeout,
)
from .resume_contract import (
    CliContinuationContext,
    record_budget_exhausted,
    resume_prompt_for,
)


@dataclass(frozen=True)
class CliResumeOutcome:
    """续跑循环结果（结构化，供 cmd_run 收口与审计）。"""

    status: str  # completed / budget_exhausted / stopped / unresumable
    rounds: int  # 首轮 + 续跑轮数
    final_result: object = None
    reason: str = ""


def should_resume(result: object) -> tuple[bool, str]:
    """收口是否允许由显式 Goal 或用户 resume 继续。"""
    from ..agent.conversation.runtime import should_continue_task

    return should_continue_task(result)


# LLM: CLI 续跑线与 wake_queue 闹钟字条的生命周期桥——用户显式 resume
# 与收口终态都代表"该任务的闹钟作废/已消费": resume 入口 = 事件提前醒
# (对齐 gateway 侧 _cancel_sleep_wake_on_event 与 会话运行时 sleep 可被新输入
# 打断语义); 终态收口 = 任务不再自动醒(EXEC-39), 清 pending 字条防僵尸
# 到期重醒。只清字条不清历史行, fail-silent(清不掉由 5min 对账兜底)。
# 函数用途: 取消某任务全部 pending 唤醒字条; 续跑入口与收口执行器共用。
def _cancel_task_wake_notes(agent: object, task_id: str) -> None:
    if not task_id:
        return
    try:
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        if repo is not None and callable(getattr(repo, "cancel_wakes_for_task", None)):
            repo.cancel_wakes_for_task(task_id)
    except Exception:  # noqa: BLE001 清不掉不拦续跑/收口, 对账兜底
        pass


class ResumeRunOnce:
    """首轮/续跑轮的 run 封装：契约 ID 贯穿同一 task/run/thread 链路。

    - 首轮：bind_cli_run_conversation 显式绑定（thread 建在
      ConversationStore），从绑定 params 构建 CliContinuationContext。
    - 续跑轮：ctx.apply_to 覆写 ID（request_id=root#cont-N, run/task=
      root, thread 复用），resume_context=True。
    """

    def __init__(
        self,
        agent: object,
        *,
        base_params: RunParams,
        on_chunk: object = None,
        save: bool = False,
        delivery_contract: object = None,
    ):
        self.agent = agent
        self.base_params = base_params
        self.on_chunk = on_chunk
        self.save = save
        self.delivery_contract = delivery_contract
        self.ctx: CliContinuationContext | None = None

    def __call__(
        self,
        prompt: str,
        continuation_seq: int = 0,
        attempt_id: str = "",
        continuation_reason: str = "",
    ) -> tuple:
        """返回 (result, bound_params)。首轮后 self.ctx 建立。"""
        if continuation_seq == 0:
            bound = bind_cli_run_conversation(self.agent, self.base_params, prompt)
            result = self.agent.run(
                prompt,
                params=bound,
                save=self.save,
                source="cli_run",
                delivery_contract=self.delivery_contract,
                on_chunk=self.on_chunk,
            )
            # EXEC-29c: 首轮 run 完成后, attach_run_task_workspace_context 已把
            # 权威 task root 写入 agent._current_run_task_workspace。bind 返回的
            # bound 只有 conversation_thread_id、没有 run_workspace(workspace 是
            # agent.run 内部 attach 才生成的)——EXEC-29b 从 bound 读 workspace
            # 恒为空、条件不成立、回写从未发生, 真机(ma-a r2b)续跑仍新建
            # "系统续跑-1"目录。从这里构造 run_workspace 回写 base_params,
            # 续跑轮 attach 走 _existing_workspace_paths 复用首轮目录
            # 。output/work 子目录取
            # _existing_workspace_paths 同款默认(root/output、root/work)。
            root_text = str(
                getattr(self.agent, "_current_run_task_workspace", "") or ""
            ).strip()
            if root_text:
                merged = dict(getattr(self.base_params, "task_attributes", None) or {})
                merged["run_workspace"] = {
                    "task_root": root_text,
                    "output_dir": str(Path(root_text) / "output"),
                    "work_dir": str(Path(root_text) / "work"),
                }
                self.base_params = replace(self.base_params, task_attributes=merged)
            thread_id = str(
                (bound.task_attributes or {}).get("conversation_thread_id") or ""
            )
            # parent_attempt 记首轮真实 DB attempt(双席复核 seq1845 硬门2:
            # bound.attempt_id 是 agent.run 前的合成值, agent.run 内部
            # create_attempt 才发放 DB 权威 attempt——首轮收口后从 DB 查
            # 真实 attempt 作树形链起点, 不依赖 pre-run 值)。
            real_first = self._latest_db_attempt()
            self.ctx = CliContinuationContext(
                root_task_id=str(bound.task_id or bound.run_id or ""),
                root_run_id=str(bound.run_id or bound.request_id or ""),
                root_thread_id=thread_id,
                root_request_id=str(bound.request_id or ""),
                parent_attempt_id=real_first or str(bound.attempt_id or ""),
            )
            return result, bound
        if self.ctx is None:
            raise RuntimeError("resume round without first-round continuation context")
        # 缺口B(双席复核 seq1834): attempt_id 不传合成 ID——传空让
        # run_params_with_request_id 生成 + agent.run 内部 create_attempt
        # 发放 DB current attempt(同 run_id 续挂, 见 runtime_mixin:321-368),
        # ctx 只记录 parent 关系, attempt 链是真实 DB 链。
        bound = self.ctx.apply_to(
            self.base_params, seq=continuation_seq, attempt_id=""
        )
        # 缺口F(双席复核 seq1835): 上轮收口原因写进 params——
        # bind_cli_run_conversation 的续跑分支把它放进消息结构化 metadata
        # (typed continuation event), 不依赖提示文本解析。
        if continuation_reason:
            bound = replace(
                bound, continuation_reason=str(continuation_reason)
            )
        result = self.agent.run(
            prompt,
            params=bound,
            save=self.save,
            source="cli_run",
            resume_context=True,
            delivery_contract=self.delivery_contract,
            on_chunk=self.on_chunk,
        )
        # 缺口B: 续跑轮 attempt 由 agent.run 内部 create_attempt 发放
        # (DB current attempt, 同 run 续挂), 这里从 repo 查最新 attempt
        # 记录为真实链——不合成 attempt-{seq}。
        real_attempt = self._latest_db_attempt()
        if real_attempt:
            self.ctx = self.ctx.next(attempt_id=real_attempt)
        return result, bound

    def _task_run_id(self) -> str:
        """从 runtime.db 查当前 agent_run 的 task_run_id（账本完整链）。

        双席复核(seq1847 维护记录): continuation_budget_exhausted 事件此前
        task_run_id 恒空——attempt/agent_run 有值不等于 task/run/attempt/
        event ledger 完整闭合。预算事件补齐 task_run_id 后整链可反查。
        """
        try:
            subagents = getattr(self.agent, "subagents", None)
            repo = getattr(subagents, "runtime_db", None)
            if repo is None or self.ctx is None:
                return ""
            row = repo.agent_run_for_run_id(self.ctx.root_run_id)
            if row is None:
                return ""
            return str(row["task_run_id"] or "")
        except Exception:  # noqa: BLE001 查不到保守返回空(fail-silent)
            return ""

    def _latest_db_attempt(self) -> str:
        """从 runtime.db 查当前 agent_run 的最新 attempt（DB 权威链）。"""
        try:
            subagents = getattr(self.agent, "subagents", None)
            repo = getattr(subagents, "runtime_db", None)
            if repo is None or self.ctx is None:
                return ""
            row = repo.agent_run_for_run_id(self.ctx.root_run_id)
            if row is None:
                return ""
            latest = repo._runtime_connect().execute(
                "SELECT attempt_id FROM agent_attempts "
                "WHERE agent_run_id = ? ORDER BY started_at DESC LIMIT 1",
                (str(row["agent_run_id"]),),
            ).fetchone()
            return str(latest["attempt_id"] or "") if latest else ""
        except Exception:  # noqa: BLE001 查不到保守返回空(沿用上轮 parent)
            return ""



# EXEC-30: 同一收口 reason 连续续跑上限(对照 会话运行时/轻量运行时 写完即测即收的
# 天然收敛的机制等价物)。同因 3 次仍不收敛=模型在同一模式无限迭代, 强制收口。
# 常量权威在 conversation/closeout.py 的收口状态机, 这里 import 复用。


# LLM: 只读取既有 conversation goal 的结构化 active 状态；品牌注释调整不得改变普通回合不自动续跑的边界。
# 函数用途: 判断当前任务是否显式授权 Goal 续跑；不写账、不启动模型，读取失败仍保守拒绝。
def _auto_resume_authorized(agent: object, runner: object) -> bool:
    """EXEC-39(owner 拍板): 正常不自动续跑——只有任务有 active goal 时才
    授权 CLI 自动续跑(用户/模型显式设了持续
    目标+预算, idle 驱动才启动)。无 goal 时收口即停, 用户 run --resume
    手动继续。"""
    store = getattr(agent, "conversation_store", None)
    if store is None or runner.ctx is None:
        return False
    load_goal = getattr(store, "load_goal", None)
    if not callable(load_goal):
        return False
    try:
        goal = load_goal(
            runner.ctx.root_thread_id,
            task_id=str(runner.ctx.root_task_id or ""),
        )
    except Exception:  # noqa: BLE001 goal 读不到=fail-closed 不自动续跑
        return False
    return goal is not None and str(
        getattr(goal, "status", "") or ""
    ).strip().lower() == "active"


def run_with_resume(
    agent: object,
    *,
    initial_prompt: str,
    base_params: RunParams,
    max_rounds: int | None = None,
    on_chunk: object = None,
    save: bool = False,
    delivery_contract: object = None,
) -> CliResumeOutcome:
    """首轮 + 续跑轮循环（进程内，同一 task/run/thread 链路）。

    ``max_rounds`` 只限制显式 Goal 的进程内连续轮数；普通 CLI 请求没有
    active Goal，因此首轮结束即返回。

    EXEC-41(四改之 2 步骤 2): 收口判定换用 decide_closeout 单一权威——
    停止原因采集(should_resume 白名单 gate)与 goal 授权(_auto_resume_
    authorized)以事实输入机器, 机器输出终态; 本函数只执行终态对应的
    落账/移交/返回, 不再各自分支推理预算与收敛。行为与旧实现等价
    (max_rounds=0 的退化配置除外: 旧实现回落 resume_limit_reached,
    新实现判 max_rounds_reached)。
    """
    if max_rounds is None:
        max_rounds = int(
            getattr(getattr(agent, "config", None), "cli_resume_max_rounds", 0) or 8
        )
    # EXEC-35: 上次运行异常中断(429/kill)后, 旧 attempt 可能仍是 running
    # (僵尸执行者)。目录级执行锁已移除；仍须用进程事实纠正 attempt 状态，
    # 避免停止、恢复和持久运行展示继续引用已退出的执行者。
    # 复用 RUN-01 recover_stale_attempts(进程死亡证明)先调和僵尸 attempt,
    # 再以接管者身份续跑。
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    recover = getattr(repo, "recover_stale_attempts", None)
    if callable(recover):
        try:
            recover()
        except Exception:  # noqa: BLE001 调和失败不拦续跑(旧行为), 保守继续
            pass
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    result, _bound = runner(initial_prompt, 0, "")
    rounds = 1
    same_reason_streak = 0
    should, reason = should_resume(result)
    if runner.ctx is None or not runner.ctx.root_task_id:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result,
            reason="no_continuation_contract",
        )
    authorized = _auto_resume_authorized(agent, runner)

    # GoalManager/ConversationStore 的 exact active goal 是唯一自动续跑授权；
    # -1 表示没有独立普通任务 policy 预算，仍受同因和 max_rounds 护栏约束。
    budget_left = -1
    while True:
        outcome = decide_closeout(
            CloseoutFacts(
                runtime_status=str(getattr(result, "runtime_status", "") or ""),
                runtime_reason=str(getattr(result, "runtime_reason", "") or ""),
                runtime_source=str(getattr(result, "runtime_source", "") or ""),
                continuable=should,
                active_goal=authorized,
                resume_budget_left=budget_left,
                same_reason_streak=same_reason_streak,
                rounds=rounds,
                # 机器语义: max_rounds 是总代数上限(含首轮), 而本函数参数
                # max_rounds 是续跑轮上限——加回首轮。
                max_rounds=max_rounds + 1,
            )
        )
        if outcome.state != STATE_RESUME_ROUND:
            return _closeout_after_decision(
                agent, runner, outcome, result, rounds, initial_prompt
            )
        seq = rounds  # 续跑轮 seq 与已跑代数一致(1..max_rounds)
        prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=initial_prompt,
        )
        # 缺口F: 上轮收口原因随 params 传续跑轮(消息结构化 metadata)
        result, _bound = runner(
            prompt, seq, attempt_id="", continuation_reason=reason
        )
        rounds += 1
        # parent_attempt 推进在 ResumeRunOnce 内部完成（DB 真实 attempt）
        should, new_reason = should_resume(result)
        # EXEC-30 续跑收敛 gate(阶段三 gorm 真机): 同一 runtime_reason 连续
        # 续跑≥3 次说明模型在同一收口模式里无限迭代(13h/268 写操作不停,
        # 对照 会话运行时/轻量运行时 写完即测即收)。同因循环到限强制收敛, 不再续跑——
        # 与 EXEC-04'同类失败循环'防护同族, 防无人值守无限续跑。reason 变了
        # (新问题)则重新计数, 不误杀有进展的续跑链。
        if should and new_reason == reason:
            same_reason_streak += 1
        elif should:
            same_reason_streak = 1
        reason = new_reason


def _closeout_after_decision(
    agent: object,
    runner: ResumeRunOnce,
    outcome: object,
    result: object,
    rounds: int,
    initial_prompt: str,
    *,
    write_events: bool = True,
) -> CliResumeOutcome:
    """EXEC-41: 执行收口机器的终态——落账/返回都只按 outcome.state, 不再
    散落分支推理。done → completed; wait_handoff → no_active_goal 是
    "正常不自动续跑"(EXEC-39) 标 unresumable, 预算/同因/轮数耗尽标
    budget_exhausted + 落账事件(write_events=False 时手动续跑只返回状态);
    其余(wait_human/cancelled) → unresumable(缺口C: 只有 runtime_status=ok
    才算 completed, 不误标)。所有终态都清任务闹钟字条(EXEC-39: 停即停,
    不自动醒, 防僵尸到期重醒)。2026-08-17 owner
    拍板删除 gateway 移交线——不再写移交单, 任务停下后唯一续跑入口是
    用户显式 run --resume。"""
    if outcome.state == STATE_DONE:
        _cancel_task_wake_notes(agent, runner.ctx.root_task_id)
        return CliResumeOutcome(
            status="completed", rounds=rounds, final_result=result,
            reason=outcome.reason or "task_completed",
        )
    if outcome.state == STATE_WAIT_HANDOFF:
        _cancel_task_wake_notes(agent, runner.ctx.root_task_id)
        if outcome.reason == "no_active_goal":
            return CliResumeOutcome(
                status="unresumable", rounds=rounds, final_result=result,
                reason=outcome.reason,
            )
        if write_events:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=runner.ctx.parent_attempt_id,
                task_run_id=runner._task_run_id(),
                budget_before=max(rounds - 1, 0),
                budget_after=rounds,
                reason=outcome.reason,
            )
        return CliResumeOutcome(
            status="budget_exhausted", rounds=rounds, final_result=result,
            reason=outcome.reason,
        )
    # wait_human / cancelled: 任务等用户显式继续, 闹钟作废(EXEC-39)。
    _cancel_task_wake_notes(agent, runner.ctx.root_task_id)
    return CliResumeOutcome(
        status="unresumable", rounds=rounds, final_result=result,
        reason=outcome.reason or "not_continuable",
    )


# LLM: 手动续跑入口(EXEC-33, 对照 会话运行时 resume/轻量运行时 --continue/终端交互
# --continue): 非完成收口(unresumable)后任务不再死——用户可 run --resume
# <task_id|目录> 从持久事实源(task.yaml/run_workspace.json)恢复同一
# task/run/thread 链继续。续跑轮走 ResumeRunOnce 的 continuation 路径
# (bind 用 root_request_id 复用同一 ConversationStore thread), 与自动
# resume_loop 共用全部契约与 EXEC-30 收敛 gate。
# 函数用途: 恢复一个已存在 CLI 任务的执行; 找不到/不可恢复时明确报错。
def run_manual_resume(
    agent: object,
    *,
    task_ref: str,
    max_rounds: int | None = None,
    on_chunk: object = None,
    save: bool = False,
    delivery_contract: object = None,
) -> CliResumeOutcome:
    from pathlib import Path as _Path

    from ..agent.agent_core.runtime.loop_models import RunParams
    from ..agent.agent_core.runtime.run_params import run_params_with_request_id

    facts = _load_task_facts(agent, task_ref)
    if facts is None:
        return CliResumeOutcome(
            status="unresumable", rounds=0, final_result=None,
            reason="task_facts_not_found",
        )
    base_params = run_params_with_request_id(
        RunParams(
            source="cli_run",
            task_attributes={
                "run_workspace": facts["run_workspace"],
            },
            run_id=facts["run_id"],
            task_id=facts["task_id"],
            request_id=facts["request_id"],
            continuation_root_request_id=facts["request_id"],
            continuation_root_run_id=facts["run_id"],
            continuation_root_task_id=facts["task_id"],
            continuation_root_thread_id=facts.get("thread_id", ""),
            recovery_next_actions=[],
        )
    )
    # EXEC-35b: 上次运行以终态(completed/failed/cancelled 等)收口的任务,
    # promote_current_conversation_task 不会再续接终态 link(真机 2026-08-16:
    # ma-b 假完成标 completed → resume 工具全被 BINDING_FAILED 拦)。用户
    # 显式 run --resume 就是结构化"重新激活"命令——把终态 link 迁移回
    # active, 再以接管者身份续跑。
    store = getattr(agent, "conversation_store", None)
    load_link = getattr(store, "load_task_link", None)
    update_status = getattr(store, "update_task_status", None)
    if callable(load_link) and callable(update_status):
        try:
            link = load_link(facts["task_id"])
        except Exception:  # noqa: BLE001 link 读不到不拦(绑定时自然报错)
            link = None
        if link is not None and str(
            getattr(link, "status", "") or ""
        ).strip().lower() in {"completed", "done", "failed", "abandoned", "cancelled"}:
            try:
                update_status({"task_id": facts["task_id"], "status": "active"})
            except Exception:  # noqa: BLE001 迁移失败保守继续(与旧行为一致)
                pass
    # EXEC-35d: 旧执行者的进度 policy 残留会让 conversation_task_execution_
    # state 判 running(真机 2026-08-16: 429 收口后 3 个 policy 未停用 →
    # resume 工具仍被 BINDING_FAILED 拦)。用户显式 resume = 接管, 停用
    # 该任务的全部进度 policy, 再由本轮执行重新建立。
    list_policies = getattr(store, "list_progress_policies", None)
    disable_policy = getattr(store, "disable_progress_policy", None)
    if callable(list_policies) and callable(disable_policy):
        try:
            for policy in list_policies(enabled_only=False):
                if str(getattr(policy, "task_id", "") or "") == facts["task_id"]:
                    try:
                        disable_policy(str(getattr(policy, "policy_id", "") or ""))
                    except Exception:  # noqa: BLE001 单个停用失败不拦续跑
                        pass
        except Exception:  # noqa: BLE001 policy 列表读不到保守继续
            pass
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    # 手动续跑没有首轮 result——直接用持久事实重建 ctx(同一 task/run/thread)。
    runner.ctx = CliContinuationContext(
        root_task_id=facts["task_id"],
        root_run_id=facts["run_id"],
        root_thread_id=facts.get("thread_id", ""),
        root_request_id=facts["request_id"],
        parent_attempt_id=facts.get("latest_attempt_id", ""),
    )
    if max_rounds is None:
        max_rounds = int(
            getattr(getattr(agent, "config", None), "cli_resume_max_rounds", 0) or 8
        )
    prompt = (
        f"继续执行之前的任务（{facts.get('title') or facts['task_id']}）。"
        "请基于现有工作目录里的进度继续推进直到完成交付。"
    )
    # EXEC-35f: 反复 resume 时 channel_bindings 可能漂移(失败重试新建了
    # thread, channel resolve 找到非任务 thread → dedupe 冲突/BINDING)。
    # 任务 thread 的单一权威是 tasks/<id>.json 的 thread_id——resume 前
    # 把 channel 绑定修正回任务 thread(thread_for_task 优先于 channel
    # resolve)。
    store_fix = getattr(agent, "conversation_store", None)
    thread_for_task = getattr(store_fix, "thread_for_task", None)
    bind_channel = getattr(store_fix, "bind_channel", None)
    if callable(thread_for_task) and callable(bind_channel):
        try:
            task_thread = thread_for_task(facts["task_id"])
            if task_thread is not None:
                from ..agent.agent_core.cli_run_conversation import (
                    _CLI_RUN_CHANNEL,
                    _CLI_RUN_USER_ID,
                )

                bind_channel(
                    {
                        "channel": _CLI_RUN_CHANNEL,
                        "channel_conversation_id": str(facts.get("request_id") or ""),
                        "channel_user_id": _CLI_RUN_USER_ID,
                        "canonical_user_id": _CLI_RUN_USER_ID,
                        "thread_id": str(getattr(task_thread, "thread_id", "") or ""),
                    }
                )
                facts["thread_id"] = str(getattr(task_thread, "thread_id", "") or "")
        except Exception:  # noqa: BLE001 修正失败保守继续(旧行为)
            pass
    # EXEC-35e: 续跑 seq 固定 1 会让每次 resume 用同一 dedupe_key
    # (cli_run:{root}#cont-1:system) 但 content 不同(resume_prompt_for 按
    # 上轮 reason 生成)→ append_message_once 抛 dedupe key reused。seq 从
    # 历史最大 continuation_seq+1 开始, 每次 resume 是新的执行代数。
    start_seq = _next_manual_resume_seq(agent, facts) or 1
    # wake_queue 生命周期: 用户显式 run --resume = 事件提前醒(对齐 会话运行时
    # sleep 可被新输入打断), 该任务 pending 字条(sleep/goal_tick)作废,
    # 防到期后重复唤醒已人工接管的轮。
    _cancel_task_wake_notes(agent, facts["task_id"])
    result, _bound = runner(
        prompt, start_seq, "", continuation_reason="manual_resume"
    )
    rounds = 1
    reason = "manual_resume"
    same_reason_streak = 0
    # EXEC-35g: 续跑轮 seq 从 start_seq+1 开始——首轮已用 start_seq, 循环
    # 若从 2 开始会在 start_seq>2 时与首轮重叠(跨进程 resume 时同 seq 不同
    # content → dedupe 冲突, 真机 ma-b-resume5 实锤)。
    # EXEC-41(四改之 2 步骤 3): 收口判定换用 decide_closeout——用户显式
    # run --resume 本身就是授权(active_goal=True 事实), 无 policy 预算
    # (resume_budget_left=-1=不限), 同因/轮数护栏与自动续跑同款。
    while True:
        should, new_reason = should_resume(result)
        if should and new_reason == reason:
            same_reason_streak += 1
        elif should:
            same_reason_streak = 1
        reason = new_reason
        outcome = decide_closeout(
            CloseoutFacts(
                runtime_status=str(getattr(result, "runtime_status", "") or ""),
                runtime_reason=str(getattr(result, "runtime_reason", "") or ""),
                runtime_source=str(getattr(result, "runtime_source", "") or ""),
                continuable=should,
                active_goal=True,
                resume_budget_left=-1,
                same_reason_streak=same_reason_streak,
                rounds=rounds,
                max_rounds=max_rounds + 1,
            )
        )
        if outcome.state != STATE_RESUME_ROUND:
            return _closeout_after_decision(
                agent, runner, outcome, result, rounds, prompt,
                write_events=False,
            )
        seq = start_seq + rounds
        next_prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=prompt,
        )
        result, _bound = runner(
            next_prompt, seq, attempt_id="", continuation_reason=reason
        )
        rounds += 1


def _next_manual_resume_seq(agent: object, facts: dict) -> int | None:
    """从会话历史里数已有续跑轮数, 返回下一个可用 continuation_seq(EXEC-35e)。"""
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None
    thread_id = str(facts.get("thread_id") or "").strip()
    if not thread_id:
        resolver = getattr(store, "resolve_thread", None)
        if not callable(resolver):
            return None
        try:
            from ..agent.agent_core.cli_run_conversation import (
                _CLI_RUN_CHANNEL,
                _CLI_RUN_USER_ID,
            )

            thread = resolver(
                channel=_CLI_RUN_CHANNEL,
                channel_conversation_id=str(facts.get("request_id") or ""),
                channel_user_id=_CLI_RUN_USER_ID,
            )
            thread_id = str(getattr(thread, "thread_id", "") or "").strip()
        except Exception:  # noqa: BLE001 查不到不拦, 回落 seq=1
            return None
    if not thread_id:
        return None
    recent = getattr(store, "recent_messages", None)
    if not callable(recent):
        return None
    try:
        entries = recent(thread_id, limit=100)
    except Exception:  # noqa: BLE001 消息读不到不拦
        return None
    max_seq = 0
    for entry in entries:
        meta = getattr(entry, "metadata", None) or {}
        try:
            seq = int((meta or {}).get("continuation_seq") or 0)
        except (TypeError, ValueError):
            continue
        max_seq = max(max_seq, seq)
    return max_seq + 1


def _load_task_facts(agent: object, task_ref: str) -> dict | None:
    """从 task.yaml/run_workspace.json 读取持久事实源(EXEC-33)。"""
    import json as _json
    from pathlib import Path as _Path

    home = getattr(agent, "home_paths", None)
    tasks_root = None
    if home is not None:
        owner_home = str(getattr(home, "owner_home_dir", "") or "").strip()
        if owner_home:
            tasks_root = _Path(owner_home) / "tasks"

    def _read_workspace_dir(candidate: _Path):
        task_yaml = candidate / "task.yaml"
        ws_json = candidate / "run_workspace.json"
        if not task_yaml.exists() or not ws_json.exists():
            return None
        try:
            # task.yaml 由 workspace 写入器生成, 是简单的 "key: JSON 值" 行格式
            # (项目不依赖第三方 yaml 库, 这里做最小解析)。
            meta: dict = {}
            for line in task_yaml.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if value:
                    try:
                        meta[key.strip()] = _json.loads(value)
                    except Exception:
                        meta[key.strip()] = value
        except Exception:
            return None
        try:
            ws = _json.loads(ws_json.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        return meta, ws, candidate

    ref = str(task_ref or "").strip()
    candidates: list[_Path] = []
    p = _Path(ref).expanduser()
    if p.is_dir():
        candidates.append(p / "work")
        candidates.append(p)
    if tasks_root is not None and tasks_root.exists():
        # 目录名以 task 标题命名;task_id 存于 task.yaml——扫描所有任务目录的
        # work/task.yaml 按 task_id 精确匹配(目录名含 ref 也收)。
        for day_dir in tasks_root.iterdir():
            if not day_dir.is_dir():
                continue
            for tdir in day_dir.iterdir():
                if not tdir.is_dir():
                    continue
                if ref in tdir.name or ref in str(tdir):
                    candidates.append(tdir / "work")
                    candidates.append(tdir)
                else:
                    candidates.append(tdir / "work")
    for cand in candidates:
        hit = _read_workspace_dir(cand)
        if not hit:
            continue
        meta, ws, work_dir = hit
        task_id = str(meta.get("task_id") or meta.get("request_id") or "")
        if task_id and task_id != ref and ref not in str(cand):
            # 扫描候选按 task_id 精确匹配;目录路径含 ref 的候选放行(模糊输入)
            continue
        task_root = str(work_dir.parent)
        return {
            "task_id": task_id,
            "run_id": str(meta.get("run_id") or meta.get("request_id") or ""),
            "request_id": str(meta.get("request_id") or ""),
            "thread_id": str(meta.get("thread_id") or ""),
            "title": str(meta.get("task_title") or ""),
            "latest_attempt_id": str(meta.get("latest_attempt_id") or ""),
            "run_workspace": {
                "task_root": task_root,
                "output_dir": str(ws.get("output_dir") or _Path(task_root) / "output"),
                "work_dir": str(ws.get("work_dir") or work_dir),
            },
        }
    return None
