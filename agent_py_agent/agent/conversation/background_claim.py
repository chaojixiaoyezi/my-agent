# LLM: 后台执行只沿原 claims 领取、续租和结算；最终准入与退出顺序须联测取消、Compact、恢复和来源消费。
# 不持有 Agent/完整 Store，不消费普通来源或创建执行器；共享心跳继续使用 run_claim.py。
# 模块用途: 领取一个后台执行车道，在模型执行前重查状态，并按本次真实结果释放精确租约。
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..backends.errors import is_provider_transient_error
from ..concurrency.interrupt import is_interrupted, register_interruptible
from ..settings.thread_model_selection import is_model_configuration_unavailable
from .background_execution import BackgroundCompactSliceYield
from .control_commands import conversation_request_interrupt_name
from .models import BackgroundMainAgentReport
from .run_claim import ConversationRunClaimHeartbeat
from .store_io import now

if TYPE_CHECKING:
    from .store_claims import ClaimStore


# LLM: 状态、任务归属和来源查询在原分支调用；claims 为唯一持久域，配置来自原 scheduler 归一值。
# 类用途: 列明一片后台执行需要的能力，不缓存准入结论、不暴露模型权限或其它存储领域。
@dataclass(frozen=True)
class BackgroundClaimDependencies:
    claims: ClaimStore
    claim_scope_id: Callable[[str, str], str]
    child_owns_task: Callable[[object], bool]
    terminal_task: Callable[[dict], bool]
    recovery_block: Callable[[str], dict[str, str] | None]
    retire_source: Callable[[dict], None]
    run_once: Callable[[dict], BackgroundMainAgentReport | None]
    runtime_facts: Callable[[], dict]
    record_policy_failure: Callable[[dict], None]
    lease_seconds: int
    heartbeat_interval_seconds: float


# LLM: 子代理归属必须来自调用方的 canonical 查询；此回执不调用主模型，也不替孩子续跑。
# 函数用途: 告知来源编排误投的工作仍由原子代理负责，让原账本按既有规则确认。
def _subagent_owned_report(kwargs: dict) -> BackgroundMainAgentReport:
    return BackgroundMainAgentReport(
        thread_id=str(kwargs.get("thread_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        reason=str(kwargs.get("reason") or "subagent_runner_owned"),
        response="",
        route_channel=str(kwargs.get("route_channel") or "internal"),
        route_target=str(kwargs.get("route_target") or ""),
        created_at=now(kwargs.get("now")),
        delivery_status="suppressed",
        delivery_reason="subagent_runner_owns_continuation",
        wake_handled=True,
    )


# LLM: 领取前登记中断身份，直到本片结算后才释放；终态先关闭 claim 再退休来源，回合中断只关闭 claim。
# 函数用途: 防止排队期间已停止或待恢复的任务开始新副作用，忙碌车道不启动心跳或模型。
def run_claimed(
    dependencies: BackgroundClaimDependencies,
    kwargs: dict,
) -> BackgroundMainAgentReport | None:
    task_id = str(kwargs.get("task_id") or "").strip()
    registration = register_interruptible(conversation_request_interrupt_name(task_id)) if task_id else nullcontext()
    with registration:
        if dependencies.child_owns_task(kwargs.get("task_id")):
            return _subagent_owned_report(kwargs)
        claim_scope_id = dependencies.claim_scope_id(
            str(kwargs.get("thread_id") or ""),
            str(kwargs.get("task_id") or ""),
        )
        claim = dependencies.claims.acquire(
            {
                "thread_id": kwargs.get("thread_id", ""),
                "claim_scope_id": claim_scope_id,
                "task_id": kwargs.get("task_id", ""),
                "reason": kwargs.get("reason", ""),
                "lease_seconds": dependencies.lease_seconds,
                "now": kwargs.get("now"),
            }
        )
        if claim is None:
            return None
        if is_interrupted():
            _finish_nonexecuted_claim(
                dependencies, kwargs, claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""), admission="turn_interrupted",
            )
            return None
        if dependencies.terminal_task(kwargs):
            _finish_nonexecuted_claim(
                dependencies,
                kwargs,
                claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""),
                admission="terminal_task_link",
            )
            dependencies.retire_source(kwargs)
            return None
        recovery_block = dependencies.recovery_block(str(kwargs.get("task_id") or "").strip())
        if recovery_block is not None:
            _finish_nonexecuted_claim(
                dependencies,
                kwargs,
                claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""),
                admission="authority_recovery_required",
                recovery_block=recovery_block,
            )
            return None
        return run_with_heartbeat(
            dependencies,
            str(claim.get("claim_id") or ""),
            kwargs,
            claim_scope_id=claim_scope_id,
        )


# LLM: 调用者持有本片中断登记；异常类别及 stop→facts→finish→普通失败账顺序保持，None/假值正常结算。
# 函数用途: 运行已经领取的后台工作片，持续续租，退出后保存真实结果；不把取消或压缩让出写成失败。
def run_with_heartbeat(
    dependencies: BackgroundClaimDependencies,
    claim_id: str,
    kwargs: dict,
    *,
    claim_scope_id: str = "",
) -> BackgroundMainAgentReport | None:
    heartbeat = _start_heartbeat(
        dependencies,
        claim_id,
        kwargs["thread_id"],
        claim_scope_id=claim_scope_id,
    )
    status = "finished"
    error: BaseException | None = None
    try:
        if is_interrupted():
            raise InterruptedError("后台工作片已被中断")
        return dependencies.run_once(kwargs)
    except InterruptedError:
        # 回合中断只关闭本次执行权；目标续跑和任务资源停止分别由原控制入口负责。
        status = "cancelled"
        return None
    except BackgroundCompactSliceYield:
        # 本片让出不消费原 wake/observation/policy，下片沿 canonical checkpoint 继续。
        return None
    except BaseException as exc:
        status = "failed"
        error = exc
        raise
    finally:
        heartbeat.stop()
        dependencies.claims.finish(
            {
                "thread_id": kwargs["thread_id"],
                "claim_scope_id": claim_scope_id,
                "claim_id": claim_id,
                "task_id": kwargs.get("task_id", ""),
                "status": status,
                "error": error,
                "runtime_facts": dependencies.runtime_facts(),
                "now": now(),
            }
        )
        if (status == "failed" and error is not None and not is_provider_transient_error(error)
                and not is_model_configuration_unavailable(error)):
            dependencies.record_policy_failure(kwargs)


# LLM: 共享心跳原接口读取 store.claims；只绑定同一个 claims 域，不传入完整 Store 或另建续租协议。
# 函数用途: 启动原会话租约心跳；启动仍在执行 try 之前，参数和异常范围保持原行为。
def _start_heartbeat(
    dependencies: BackgroundClaimDependencies,
    claim_id: str,
    thread_id: str,
    *,
    claim_scope_id: str = "",
) -> ConversationRunClaimHeartbeat:
    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": SimpleNamespace(claims=dependencies.claims),
            "thread_id": thread_id,
            "claim_scope_id": claim_scope_id,
            "claim_id": claim_id,
            "lease_seconds": dependencies.lease_seconds,
            "interval_seconds": dependencies.heartbeat_interval_seconds,
        }
    )
    heartbeat.start()
    return heartbeat


# LLM: 只结算本次确切 claim；复制恢复事实，不在这里判断或消费来源，保持取消准入的持久格式。
# 函数用途: 记录领取后未获准执行的原因，保留调度器决定后续来源处理的权利。
def _finish_nonexecuted_claim(
    dependencies: BackgroundClaimDependencies,
    kwargs: dict,
    *,
    claim_scope_id: str,
    claim_id: str,
    admission: str,
    recovery_block: dict[str, str] | None = None,
) -> None:
    runtime_facts: dict[str, object] = {"admission": admission}
    if recovery_block is not None:
        runtime_facts["recovery_block"] = dict(recovery_block)
    dependencies.claims.finish(
        {
            "thread_id": kwargs.get("thread_id", ""),
            "claim_scope_id": claim_scope_id,
            "claim_id": claim_id,
            "task_id": kwargs.get("task_id", ""),
            "status": "cancelled",
            "runtime_facts": runtime_facts,
            "now": now(),
        }
    )
