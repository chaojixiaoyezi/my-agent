# LLM: 后台执行只沿原 claims 领取、续租和结算；领取后重查精确来源，须联测取消、Compact、恢复和来源消费。
# 不持有 Agent/完整 Store，不消费普通来源或创建执行器；共享心跳继续使用 run_claim.py。
# 模块用途: 领取一个后台执行车道，在模型执行前重查状态，并按本次真实结果释放精确租约。
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..backends.errors import (
    is_provider_environment_fault,
    is_provider_transient_error,
)
from ..concurrency.interrupt import is_interrupted, register_interruptible
from ..settings.thread_model_selection import is_model_configuration_unavailable
from .background_execution import BackgroundCompactSliceYield
from .compact_guard import is_provider_quota_failure
from .control_commands import (
    conversation_request_interrupt_name,
    session_task_interrupt_name,
)
from .models import BackgroundMainAgentReport
from .run_claim import ConversationRunClaimHeartbeat
from .store_io import now

if TYPE_CHECKING:
    from .store_claims import ClaimStore

# 领取后准入里"来源已经处理完"的码：会话消息的内容已被目标自己的回合消费（runtime._session_message_consumed_admission）。
SESSION_MESSAGE_CONSUMED_ADMISSION = "session_message_consumed"
# 命中这些准入码时，除结 claim 外还要经 retire_source 结案来源，否则唤醒留在 pending 每拍被重新认领（空转）；
#   其它准入码（来源已变化、读不出、已不在 pending）只结 claim，交还原队列重新选择。
_SOURCE_FINISHED_ADMISSIONS = frozenset({SESSION_MESSAGE_CONSUMED_ADMISSION})


# LLM: 状态、任务归属和来源查询在原分支调用；来源只读查询不能消费信封，claims 为唯一持久域。
# 类用途: 列明一片后台执行需要的能力，不缓存准入结论、不暴露模型权限或其它存储领域。
@dataclass(frozen=True)
class BackgroundClaimDependencies:
    claims: ClaimStore
    claim_scope_id: Callable[[str, str], str]
    child_owns_task: Callable[[object], bool]
    terminal_task: Callable[[dict], bool]
    recovery_block: Callable[[str], dict[str, str] | None]
    source_admission: Callable[[object], str]
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


# LLM: 绑定回合号只从唤醒信封 metadata 里的结构化字段读（派活工具写入的会话任务 id），
#   读不到就返回空串——此时**不注册专用名**（fail closed），不猜、不回退 task_id。
# 函数用途: 给出这一片宿主投递唤醒已绑定的会话任务/回合号；没有绑定时返回空串。
def _host_delivery_bound_turn(kwargs: dict) -> str:
    signal = kwargs.get("wake_signal")
    metadata = getattr(signal, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = signal.get("metadata") if isinstance(signal, dict) else None
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("session_task_id") or "").strip()


# LLM: 领取前登记中断身份，直到本片结算后释放；控制与恢复先裁决，过期来源只关闭 claim，不消费其它信封。
# 函数用途: 防止排队期间已停止、待恢复或已被处理的工作开始副作用，忙碌车道不启动心跳或模型。
def run_claimed(
    dependencies: BackgroundClaimDependencies,
    kwargs: dict,
) -> BackgroundMainAgentReport | None:
    task_id = str(kwargs.get("task_id") or "").strip()
    # LLM: 宿主投递类唤醒（另一会话的派活/消息）的 task_id 为空，原逻辑会退化成 nullcontext()——
    #   这一轮**根本没注册可中断名**，取消永远停不下来（场景 5）。改成用**认领时已写下的会话任务绑定**
    #   （唤醒信封 metadata.session_task_id）作为专用可中断名 `session-task-turn:`，与前台
    #   `conversation-request:` 不同空间；**不挪用 task_id**（它挪用会让整片因 Skill 绑定校验开不出来）。
    #   其它唤醒来源逐字保持原逻辑，行为不变。
    registration = (
        register_interruptible(session_task_interrupt_name(_host_delivery_bound_turn(kwargs)))
        if _host_delivery_bound_turn(kwargs)
        else (register_interruptible(conversation_request_interrupt_name(task_id)) if task_id else nullcontext())
    )
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
        source_admission = dependencies.source_admission(kwargs.get("wake_signal"))
        if source_admission:
            _finish_nonexecuted_claim(
                dependencies, kwargs, claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""), admission=source_admission,
            )
            _retire_finished_source(dependencies, kwargs, source_admission)
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
        return _run_once_with_bound_turn_check(dependencies, kwargs)
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
        if status == "failed" and error is not None and _counts_as_policy_failure(error):
            dependencies.record_policy_failure(kwargs)


# LLM: 只在准入码属于 _SOURCE_FINISHED_ADMISSIONS 时调用 retire_source（与 terminal_task_link 同一条结案路径，不另起 mark_handled）；
#   其它准入码不动来源。副作用：经 retire_source 写唤醒 handled 回执。
# 函数用途: 领取后发现来源已经处理完（会话消息已被消费）时，把这条来源结案，不让它每拍被重新认领。
def _retire_finished_source(dependencies: BackgroundClaimDependencies, kwargs: dict, admission: str) -> None:
    if admission in _SOURCE_FINISHED_ADMISSIONS:
        dependencies.retire_source(kwargs)


# LLM: 只对**绑定了会话任务**的回合（宿主投递类唤醒）加这一道：它的答复已经在 run_once 内部的
#   _complete_background_slice 落账交出，本段**不负责阻止交付**，只负责**把认领结算成 cancelled**
#   （不要把这片记成正常结束）。真正挡住迟到答复的是调用方线程等模型结果时的中断检查
#   （tool_model_generation._wait_for_generation_result）和交付前按任务已取消的持久检查
#   （runtime._run_agent），都在本函数之外。
#   **其它唤醒来源一个字都不变**：普通后台运行（带 task_id、注册 conversation-request: 名字）
#   是能被普通 /stop 打到的，对它们套这道检查会把已交付的答复丢掉、认领记成 cancelled、
#   唤醒留 pending 被重跑（全仓 test_thread_interrupt 抓到的回归）。
# 函数用途: 执行这一片工作；绑定了会话任务的回合在 run_once 返回后再查一次中断旗，停止已到就按取消结算。
def _run_once_with_bound_turn_check(
    dependencies: BackgroundClaimDependencies, kwargs: dict,
) -> BackgroundMainAgentReport | None:
    report = dependencies.run_once(kwargs)
    if _host_delivery_bound_turn(kwargs) and is_interrupted():
        raise InterruptedError("后台工作片已被中断")
    return report


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


# LLM: 持久策略失败账只记这条工作本身的问题。不记：供应瞬时（供应退避接管）、模型配置暂缺（等配置）、环境级故障
#   （backends.errors.is_provider_environment_fault）与额度用完（compact_guard.is_provider_quota_failure，含压缩调用撞额度的包装）
#   ——修好密钥或额度重置后策略还在，不因此退休，重试节奏交给
#   Gateway 车道暂停；与车道环境暂停同一组判定（cli/gateway_lane_retry._next_failure），与毒丸"环境级故障不计数"同一原则。
#   只按异常类型和结构化状态码判断，不读正文；改动联测 test_background_claim_execution 与策略退休测试。
# 函数用途: 判断一次后台失败要不要记进进度策略的持久失败账（连续 3 次退休）。
def _counts_as_policy_failure(error: BaseException) -> bool:
    if is_provider_transient_error(error) or is_model_configuration_unavailable(error):
        return False
    return not (is_provider_environment_fault(error) or is_provider_quota_failure(error))
