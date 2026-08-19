"""CLI 自动续跑契约层（2026-08-14 根因3 设计 v2）。

首轮 run 创建后，续跑轮显式贯穿同一 task/run/thread 链路——绝不靠
resume_context=True 或同一 conversation thread 自然形成（每轮 agent.run
默认会生成新 request_id/attempt_id/run_id/task_id，见 run_params.py 的
run_params_with_request_id）。本模块定义：
- CliContinuationContext：首轮创建的稳定身份，贯穿所有续跑轮
- 每轮 request_id 派生规则（{root}#cont-{seq}，ConversationStore append
  幂等键唯一且可追溯根）
- 续跑提示构造（只引用结构化 continuation_reason，不含验收语义）
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..agent.agent_core.runtime.loop_models import RunParams


@dataclass(frozen=True)
class CliContinuationContext:
    """CLI 自动续跑轮间贯穿的稳定身份（首轮创建，续跑轮复用）。"""

    root_task_id: str
    root_run_id: str
    root_thread_id: str
    # 首轮 request_id: conversation thread 的 channel_conversation_id 身份
    # (bind_cli_run_conversation 用 request_id 建 thread; 续跑轮复用同一
    # channel id 才能进同一 thread——root_run_id 可能是另一值, 不能当
    # channel 身份用)。
    root_request_id: str = ""
    continuation_seq: int = 0
    parent_attempt_id: str = ""

    def next(self, *, attempt_id: str) -> CliContinuationContext:
        """推进到下一轮：seq+1，parent_attempt 指向刚结束的轮。"""
        return replace(
            self,
            continuation_seq=self.continuation_seq + 1,
            parent_attempt_id=attempt_id,
        )

    def request_id_for(self, seq: int) -> str:
        """每轮 request_id 派生：{root}#cont-{seq}（幂等键唯一且可溯根）。"""
        return f"{self.root_run_id}#cont-{seq}"

    def apply_to(self, params: RunParams, *, seq: int, attempt_id: str) -> RunParams:
        """把契约身份写入 RunParams（续跑轮显式 ID 覆写，不新建根）。"""
        return replace(
            params,
            request_id=self.request_id_for(seq),
            run_id=self.root_run_id,
            task_id=self.root_task_id,
            attempt_id=attempt_id,
            continuation_seq=seq,
            continuation_root_task_id=self.root_task_id,
            continuation_root_run_id=self.root_run_id,
            continuation_root_thread_id=self.root_thread_id,
            continuation_root_request_id=self.root_request_id,
            continuation_parent_attempt_id=self.parent_attempt_id if seq > 0 else "",
        )


def continuation_from_params(params: RunParams) -> CliContinuationContext | None:
    """从 RunParams 还原契约（首轮 run 收口后读取，供 loop 续跑）。"""
    if not str(getattr(params, "continuation_root_task_id", "") or "").strip():
        return None
    return CliContinuationContext(
        root_task_id=str(params.continuation_root_task_id),
        root_run_id=str(params.continuation_root_run_id or params.run_id or ""),
        root_thread_id=str(params.continuation_root_thread_id or ""),
        root_request_id=str(
            getattr(params, "continuation_root_request_id", "") or params.request_id or ""
        ),
        continuation_seq=int(getattr(params, "continuation_seq", 0) or 0),
        parent_attempt_id=str(params.continuation_parent_attempt_id or ""),
    )


# CLI 消费续跑 policy 的互斥 lease(2026-08-14 设计 v2 审查意见2):
# gateway 调度器与 CLI resume_loop 可能同时扫描同一 policy——CLI 消费前
# 先写 metadata.cli_claim(带时间戳), 已 claim 且 lease 未过期则跳过;
# gateway 不读此标记(它仍是权威调度器), 但 CLI 侧保证自己不与 gateway
# 双跑同一 task(CLI 只在首轮收口后立即续跑, 竞争窗口极窄)。
# CLI claim 的互斥 lease 秒数(双席复核硬门2: gateway 调度器与 CLI 同读
# 同一 claim)。唯一权威在 conversation/runtime.py(gate 层, cli→conversation
# import 方向合法, 反之违反导入边界 RUNTIME_IMPORTS_CLI)。
from ..agent.conversation.runtime import CLI_RESUME_LEASE_SECONDS  # noqa: E402

_CLI_RESUME_LEASE_SECONDS = CLI_RESUME_LEASE_SECONDS


def try_claim_cli_resume(store: object, task_id: str, *, now: float | None = None) -> bool:
    """尝试独占消费某 task 的续跑 policy(CLI 侧, 严格 CAS)。

    返回 True=可续跑(未 claim 或 lease 已过期); False=已被其他 consumer
    持有(CLI 跳过本轮, 等 gateway/下次)。

    2026-08-14 双席复核硬门1: 旧实现先 list_progress_policies 读、再
    mark_progress_reported 写——两个消费者(CLI vs gateway)并发时读-改-写
    窗口内互相覆盖, 无 expected-version 比对。现在改用 store 的
    update_progress_policy_atomic(fcntl.flock 锁内读-改-写, 同文件系统
    任意消费者互斥): updater 内比对 cli_claim_at lease, 条件不满足返回
    None=不写盘——严格 compare-and-set。
    """
    import time as _time

    current = now if now is not None else _time.time()
    try:
        policies = store.list_progress_policies(enabled_only=True)
    except Exception:  # noqa: BLE001 读不到 policy 不阻断(无 policy=无可续跑)
        return False
    matching = [
        p for p in policies if p.task_id == task_id
        and str((p.metadata or {}).get("kind") or "") == "ordinary_task_resume"
    ]
    if not matching:
        return False
    policy = matching[0]

    def _cas_updater(current_policy: object):
        metadata = dict(current_policy.metadata or {})
        claimed_at = metadata.get("cli_claim_at")
        try:
            claimed_at = float(claimed_at) if claimed_at else 0.0
        except (TypeError, ValueError):
            claimed_at = 0.0
        if claimed_at > 0 and current - claimed_at < _CLI_RESUME_LEASE_SECONDS:
            return None  # lease 未过期: 条件不满足, 锁内不写盘(=CAS 失败)
        # 双向互斥(双席复核硬门1 seq1845): gateway 执行前 CAS 领取写
        # consumer=gateway_scheduler + gateway_claim_at——CLI 侧同读,
        # gateway lease 内 CLI 也放弃, 不双跑。
        gateway_claimed = metadata.get("gateway_claim_at")
        try:
            gateway_claimed = float(gateway_claimed) if gateway_claimed else 0.0
        except (TypeError, ValueError):
            gateway_claimed = 0.0
        if gateway_claimed > 0 and current - gateway_claimed < _CLI_RESUME_LEASE_SECONDS:
            return None  # gateway 正在消费(执行前领取未过期)
        metadata["cli_claim_at"] = current
        metadata["cli_claim_owner"] = "cli_resume_loop"
        # 预算语义(双席复核硬门2): 一次 claim 授予整条进程内续跑链——
        # resume_used 只在建/续 policy 时递增(ensure_ordinary_task_resume),
        # 链内 max_rounds 轮次不重复记账; gateway 同读 cli_claim_at(见
        # _progress_policy_suppression_reason)在 lease 内跳过该 task,
        # 保证 claim 链内无并发消费者, resume_limit 与 max_rounds 不漂移。
        from dataclasses import replace as _replace

        return _replace(current_policy, metadata=metadata)

    try:
        _updated, changed, aborted = store.update_progress_policy_atomic(
            policy.policy_id, _cas_updater
        )
    except Exception:  # noqa: BLE001 CAS 失败保守跳过
        return False
    if aborted:
        return False  # updater 明确放弃(lease 内被持有)
    return True  # 锁内确认后(含幂等重写) claim 成功


def record_budget_exhausted(
    *,
    agent: object,
    run_id: str,
    attempt_id: str,
    task_run_id: str = "",
    budget_before: int,
    budget_after: int,
    reason: str,
) -> None:
    """预算耗尽写 runtime_events continuation_budget_exhausted（fail-silent）。

    2026-08-14 设计 v2 审查意见5: max_rounds/resume_limit 只是预算, 不是
    成功条件——耗尽时必须写明确事件(含 budget before/after + 原因), 保留
    首轮与每个 attempt, 绝不输出 DONE。
    """
    try:
        subagents = getattr(agent, "subagents", None)
        repo = getattr(subagents, "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_event", None)):
            return
        if not run_id or not attempt_id:
            return
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            return
        repo.append_event(
            event_type="continuation_budget_exhausted",
            attempt_id=attempt_id,
            agent_run_id=str(row["agent_run_id"]),
            task_run_id=task_run_id,
            payload={
                "budget_before": int(budget_before or 0),
                "budget_after": int(budget_after or 0),
                "reason": str(reason or ""),
                "needs_user_continue": True,
                "stage": "cli_resume_loop",
            },
        )
    except Exception:  # noqa: BLE001 审计落账失败绝不反噬执行路径
        pass


def resume_prompt_for(
    *, continuation_reason: str, continuation_seq: int, user_task: str
) -> str:
    """续跑提示（唯一权威在 conversation/runtime.py, 此处转发兼容）。"""
    from ..agent.conversation.runtime import resume_prompt_for as _impl

    return _impl(
        continuation_reason=continuation_reason,
        continuation_seq=continuation_seq,
        user_task=user_task,
    )
