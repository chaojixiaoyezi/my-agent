"""CLI 自动续跑循环（2026-08-14 根因3 设计 v2，审查通过后实施）。

cmd_run 首轮收口后，若收口是可续跑族（共享 gate should_continue_task），
进程内循环续跑——同一 task/run/thread 链路（CliContinuationContext 贯穿），
直到：任务完成 / 预算耗尽 / 不可续跑族（blocked/协议违规/UNKNOWN）/ 用户
停止。续跑轮不走 cli_one_shot failed 兜底（切片3），任务级非终态由本循环
决定。

防失控：max_rounds（进程内护栏，默认 8，读 cli_resume_max_rounds 可调大）。
2026-08-15 3×3 对齐对照组：ordinary_task_resume_limit 预算已删除（会话运行时/
终端应用 无续跑预算概念），自动续跑不再因预算耗尽停等用户——防失控由
repeated_failure_halt / max_tool_rounds 等其它防线承担。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from ..agent.agent_core.cli_run_conversation import bind_cli_run_conversation
from ..agent.agent_core.runtime.loop_models import RunParams
from ..agent.backends import ProviderRecoverableError
from .resume_contract import (
    CliContinuationContext,
    record_budget_exhausted,
    resume_prompt_for,
)

# 用户指示(2026-08-16): 进程不能退出——连续不可续跑收口后进入退避节奏
# (30s→60s→120s→180s 封顶, sleep≤180s 铁律), 退避后继续尝试, 进程存活。
_UNCONTINUABLE_BACKOFF_START = 5


def _continuation_backoff_sleep(consecutive: int) -> None:
    """连续失败退避(sleep≤180s 铁律): <5 直接重试, ≥5 30s→60s→120s→180s 封顶。

    用户指示(2026-08-16): 进程不能退出——不可续跑收口与 provider 可恢复错误
    共用同一退避节奏, 退避后继续尝试, 进程存活。
    """
    if consecutive < _UNCONTINUABLE_BACKOFF_START:
        return
    time.sleep(
        min(
            180,
            30 * (2 ** min(consecutive - _UNCONTINUABLE_BACKOFF_START, 3)),
        )
    )


def record_uncontinuable_continuation(
    agent: object,
    *,
    runner: object,
    seq: int,
    reason: str,
    consecutive: int,
) -> None:
    """记录一次不可续跑收口(进程内继续下一轮的决策痕迹)。

    用户指示(2026-08-16)后不再退出进程——每次收口都留记录
    (原因/轮次/连续次数), 审计可追溯; 连续次数 ≥ 阈值时进入退避。
    记录失败不阻断续跑。
    """
    try:
        run_id = getattr(runner, "ctx", None)
        run_id = getattr(run_id, "root_run_id", "") if run_id is not None else ""
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "warning"):
            logger.warning(
                "cli_uncontinuable_continuation run_id=%s seq=%s reason=%s "
                "consecutive=%s action=continue_in_process",
                run_id, seq, reason, consecutive,
            )
    except Exception:  # noqa: BLE001 - 记录失败不阻断续跑
        pass


@dataclass(frozen=True)
class CliResumeOutcome:
    """续跑循环结果（结构化，供 cmd_run 收口与审计）。"""

    status: str  # completed / budget_exhausted / stopped / unresumable
    rounds: int  # 首轮 + 续跑轮数
    final_result: object = None
    reason: str = ""


def should_resume(result: object) -> tuple[bool, str]:
    """收口后是否续跑（共享 gate，gateway 与 CLI 同一判断）。"""
    from ..agent.conversation.runtime import should_continue_task

    return should_continue_task(result)


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



def _write_handoff(
    agent: object,
    runner: "ResumeRunOnce",
    *,
    seq: int,
    reason: str,
    user_prompt: str = "",
) -> None:
    """显式移交(2026-08-14 长任务首要约束 owner seq1856/steward seq1857):
    预算耗尽/不可续跑收口时写 runtime.db 待接管移交单——runtime.db 是
    owner 级共享权威(CLI 与 gateway 同库), gateway 调度器扫描接管续跑,
    不依赖进程 cwd 或入口私有 conversation store(CLI/gateway store 隔离
    真机坐实导致任务截断)。fail-silent: 移交失败不阻断执行路径。"""
    try:
        subagents = getattr(agent, "subagents", None)
        repo = getattr(subagents, "runtime_db", None)
        if repo is None or not callable(getattr(repo, "create_continuation_handoff", None)):
            return
        if runner.ctx is None:
            return
        repo.create_continuation_handoff(
            agent_run_id="",
            attempt_id=runner.ctx.parent_attempt_id,
            task_run_id=runner._task_run_id(),
            root_run_id=runner.ctx.root_run_id,
            root_request_id=runner.ctx.root_request_id,
            root_thread_id=runner.ctx.root_thread_id,
            root_task_id=runner.ctx.root_task_id,
            user_prompt=str(user_prompt or ""),
            continuation_seq=int(seq or 0),
            reason=str(reason or ""),
        )
    except Exception:  # noqa: BLE001 移交失败保守跳过
        pass



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

    2026-08-15 3×3 真机(cell2 bs4): max_rounds 硬编码 8 对复刻类长任务
    太紧——续跑链 8 轮×resume_limit 3 次≈24 轮就预算耗尽死停等用户「继续」,
    无人值守 CLI 场景任务必死。改为读配置 cli_resume_max_rounds(默认 8
    保持现状, 普通交互任务防失控语义不变), 长任务部署可调大。
    """
    if max_rounds is None:
        max_rounds = int(
            getattr(getattr(agent, "config", None), "cli_resume_max_rounds", 0) or 8
        )
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    consecutive_uncontinuable = 0  # 连续失败计数(不可续跑收口/provider 错误共用)
    # 首轮：进程内重试直到成功(用户指示 2026-08-16: 进程不能退出)。provider
    # 可恢复错误(MODEL_INCOMPLETE_RESPONSE 输出截断等)按退避节奏进程内重试,
    # 不让异常冒泡到 cmd_run 的 except → return 退出——真机 cell2 2026-08-15:
    # stop_reason=max_tokens → ProviderRecoverableError → 进程退出实锤(违反铁律)。
    while True:
        try:
            result, _bound = runner(initial_prompt, 0, "")
            break
        except ProviderRecoverableError as exc:
            consecutive_uncontinuable += 1
            record_uncontinuable_continuation(
                agent,
                runner=runner,
                seq=0,
                reason=f"provider_recoverable:{type(exc).__name__}",
                consecutive=consecutive_uncontinuable,
            )
            _continuation_backoff_sleep(consecutive_uncontinuable)
        except Exception as exc:  # noqa: BLE001 进程不退出铁律(2026-08-16 用户):
            # 非 Provider 异常(sqlite 并发竞争/工具执行链未知异常等)同样进程内
            # 退避续跑——真机 cell 进程消失根因: cmd_run 只捕获 Provider 两类
            # 异常, 其余 traceback 冒泡=进程退出→守护脚本反复拉起(投机取巧)。
            consecutive_uncontinuable += 1
            record_uncontinuable_continuation(
                agent,
                runner=runner,
                seq=0,
                reason=f"cli_loop:{type(exc).__name__}:{str(exc)[:120]}",
                consecutive=consecutive_uncontinuable,
            )
            _continuation_backoff_sleep(consecutive_uncontinuable)
    rounds = 1
    should, reason = should_resume(result)
    # 用户指示(2026-08-16): 进程不能退出——只有任务终态(ok)或无契约
    # (首轮连 task/run 都没建, 无法续跑)才返回; 其余收口(blocked/
    # 协议违规/UNKNOWN/unfinished)一律进入续跑循环进程内继续。
    if runner.ctx is None or not runner.ctx.root_task_id:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result,
            reason="no_continuation_contract",
        )
    if str(getattr(result, "runtime_status", "") or "").strip().lower() == "ok":
        return CliResumeOutcome(
            status="completed", rounds=rounds, final_result=result, reason=reason
        )
    if not should:
        # 首轮不可续跑族: 记录后进入续跑循环(进程内继续, 不退出)
        consecutive_uncontinuable = 1
        record_uncontinuable_continuation(
            agent,
            runner=runner,
            seq=0,
            reason=reason or "not_continuable",
            consecutive=1,
        )

    # 缺口A(双席复核 seq1834): 进入续跑循环前 claim 一次(整个循环持有,
    # lease 300s 覆盖进程内多轮)——CLI 与 gateway 同读同写同一 policy claim,
    # 已被其他 consumer 持有则整个续跑放弃(等 gateway/下次), 不双跑。
    # 缺口A真机补充(2026-08-14 testbox): try_claim 依赖
    # kind=ordinary_task_resume 的 policy, 但该 policy 由收口 finalization
    # (_schedule_typed_unfinished_continuation)创建——那是 gateway 调度器
    # 路径, CLI one-shot run 从不经过 → 找不到 policy 永远 False, 永不续跑
    # (测试里 fake agent 显式建 policy 掩盖了此洞)。修: claim 前先
    # ensure_ordinary_task_resume 建/递增同一 policy(预算单一权威),
    # 预算耗尽(返回 False) → budget_exhausted, 与 gateway 互斥语义不变。
    from ..agent.conversation.runtime import ensure_ordinary_task_resume
    from .resume_contract import try_claim_cli_resume

    store = getattr(agent, "conversation_store", None)
    if store is not None:
        ensured = ensure_ordinary_task_resume(
            agent,
            task_id=runner.ctx.root_task_id,
            thread_id=runner.ctx.root_thread_id,
            store=store,
            due_now=True,
        )
        if not ensured:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=runner.ctx.parent_attempt_id,
                task_run_id=runner._task_run_id(),
                budget_before=0,
                budget_after=0,
                reason="resume_limit_reached",
            )
            _write_handoff(
                agent, runner, seq=rounds, reason="resume_limit_reached",
                user_prompt=initial_prompt,
            )
            return CliResumeOutcome(
                status="budget_exhausted", rounds=rounds, final_result=result,
                reason="resume_limit_reached",
            )
        if not try_claim_cli_resume(store, runner.ctx.root_task_id):
            return CliResumeOutcome(
                status="unresumable", rounds=rounds, final_result=result,
                reason="claim_held_by_other_consumer",
            )
    # 用户指示(2026-08-16): 进程不能退出(按一年任务长度设计)——收口后
    # 进程内继续下一轮, 不返回 unresumable。连续不可续跑计数 + 退避
    # (维护记录 点2/6: violation 会话内修复, 连续失败转退避不
    # busy-loop 不退出; 任务终态/显式 stop 才允许退出)。
    for seq in range(1, max_rounds + 1):
        prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=initial_prompt,
        )
        # 缺口F: 上轮收口原因随 params 传续跑轮(消息结构化 metadata)
        # 用户指示(2026-08-16): 进程不能退出——provider 可恢复错误(输出截断/
        # 超时/限流)进程内退避重试同一续跑轮(不消耗 max_rounds), 异常不冒泡。
        while True:
            try:
                result, _bound = runner(
                    prompt, seq, attempt_id="", continuation_reason=reason
                )
                break
            except ProviderRecoverableError as exc:
                consecutive_uncontinuable += 1
                record_uncontinuable_continuation(
                    agent,
                    runner=runner,
                    seq=seq,
                    reason=f"provider_recoverable:{type(exc).__name__}",
                    consecutive=consecutive_uncontinuable,
                )
                _continuation_backoff_sleep(consecutive_uncontinuable)
            except Exception as exc:  # noqa: BLE001 进程不退出铁律(同首轮):
                # 任何异常不冒泡退出——进程内退避后重试同一续跑轮。
                consecutive_uncontinuable += 1
                record_uncontinuable_continuation(
                    agent,
                    runner=runner,
                    seq=seq,
                    reason=f"cli_loop:{type(exc).__name__}:{str(exc)[:120]}",
                    consecutive=consecutive_uncontinuable,
                )
                _continuation_backoff_sleep(consecutive_uncontinuable)
        rounds += 1
        # parent_attempt 推进在 ResumeRunOnce 内部完成（DB 真实 attempt）
        should, reason = should_resume(result)
        if not should:
            # 只有 runtime_status=ok 的任务完成才算 completed 退出
            # (缺口C seq1835 语义保留); 其余收口(blocked/协议违规/UNKNOWN)
            # 不再退出进程——记录原因后进程内继续下一轮(resume 续跑),
            # 连续不可续跑按退避节奏重试, 永不因收口原因退出。
            runtime_status = str(
                getattr(result, "runtime_status", "") or ""
            ).strip().lower()
            if runtime_status == "ok":
                return CliResumeOutcome(
                    status="completed", rounds=rounds, final_result=result, reason=reason
                )
            consecutive_uncontinuable += 1
            record_uncontinuable_continuation(
                agent,
                runner=runner,
                seq=seq,
                reason=reason or "not_continuable",
                consecutive=consecutive_uncontinuable,
            )
            # 退避: <5 直接重试, ≥5 30s→60s→120s→180s 封顶(sleep ≤180s
            # 铁律), 退避后重置计数继续尝试——进程存活等待链路/模型恢复。
            _continuation_backoff_sleep(consecutive_uncontinuable)
            if consecutive_uncontinuable >= _UNCONTINUABLE_BACKOFF_START:
                consecutive_uncontinuable = 0
            continue
        if rounds >= max_rounds + 1:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=runner.ctx.parent_attempt_id,
                task_run_id=runner._task_run_id(),
                budget_before=rounds - 1,
                budget_after=rounds,
                reason="max_rounds_reached",
            )
            _write_handoff(
                agent, runner, seq=rounds, reason="max_rounds_reached",
                user_prompt=initial_prompt,
            )
            return CliResumeOutcome(
                status="budget_exhausted", rounds=rounds, final_result=result,
                reason="max_rounds_reached",
            )
    record_budget_exhausted(
        agent=agent,
        run_id=runner.ctx.root_run_id,
        attempt_id=runner.ctx.parent_attempt_id,
        task_run_id=runner._task_run_id(),
        budget_before=rounds,
        budget_after=rounds,
        reason="resume_limit_reached",
    )
    _write_handoff(
        agent, runner, seq=rounds, reason="resume_limit_reached",
        user_prompt=initial_prompt,
    )
    return CliResumeOutcome(
        status="budget_exhausted", rounds=rounds, final_result=result,
        reason="resume_limit_reached",
    )
