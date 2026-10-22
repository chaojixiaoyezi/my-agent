
from __future__ import annotations

# LLM: 子代理复用原attempt/权限/心跳；正MP原timeout Thread ACK也须原stop/join，未知不ordinary结果，原roots/可恢复片/Count0保留。
# 模块用途: 启动和收口子代理工作片，线程内绑定可信排队 owner，并把新结果交回直属父级。
import logging
import threading
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from ...backends.provider_headers import provider_agent_owner_scope
from ...common.runner_thread_origin import _capture_runner_thread, _finish_original_runner_thread
from ...runtime_db.operations import RuntimeConflictError
from ...settings import AgentConfig
from ...settings.services.runtime_config_task import apply_task_runtime_config_overlay
from ...subagents.authorization_gate import OperationRequest, authorize_operation
from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import FailureType, SubAgentRunnerResult
from ...subagents.runner_control import runner_attempt_cancelled
from ..subagent.params import SubagentRunParams
from .activity_diagnostics import observe_runner_activity
from .session_pool import (
    RunnerSessionPoolLease,
    _raise_transport_unknown,
    runner_session_lease,
)
from .slice_closeout import settle_resumable_worker_slice

_LOGGER = logging.getLogger(__name__)
# 子代理 runner 会话的固定心跳秒数：与后台会话租约无关（原来借用的 background_claim_heartbeat_interval_seconds 已删除）。
_RUNNER_SESSION_HEARTBEAT_SECONDS = 5.0


# LLM: expected_attempt_id固定原宿主接纳；request_transport只来自当前独立child来源，不能序列化/构造scope补权；Count0默认None。
# 类用途: 为一个 worker 保存不可变启动参数，等待期间不重新选取执行轮。
@dataclass(frozen=True)
class RunSubagentWorkerParams:
    config: AgentConfig
    root: Path
    run_id: str
    instruction: str
    dry_run: bool
    max_cards: int
    probe: bool
    retry_reason: str
    timeout_seconds: float = 0.0
    local_store: object | None = None
    backend_override: object | None = None
    expected_attempt_id: str | None = None
    request_transport: object | None = None


# LLM: worker 先精确激活再发布 session，退出后把原 attempt 传给待续跑核对；拒绝接纳不写 runner 结果或租约，不能覆盖后来轮次。
#   入口先问准入关门（_admission_closed_refusal）：这是顺序批次（gate.run_single_runner）与并行批次（gate._counted_worker）
#   共同的启动边界，关门时不建 worker、不激活、不续派也不唤醒父级。
#   worker退出确定性关闭独立MCP工具；清理异常不覆盖业务结果，也不关闭父agent共享工具。
# 函数用途: 运行子代理工作片并收尾独立MCP客户端，保留原attempt及待续跑合同。
def _run_subagent_worker(params: RunSubagentWorkerParams) -> SubAgentRunnerResult:
    from ...core import SimpleAgent

    if (refusal := _admission_closed_refusal(params)) is not None:
        return refusal
    worker = _build_worker_agent(SimpleAgent, params)
    from ...gateway_parts.multiprocess_subagent_execution import child_worker_scope

    try:
        if params.request_transport is not None:
            params = replace(params, request_transport=replace(params.request_transport, worker_agent=worker))
        with child_worker_scope(params.request_transport, worker):
            return _run_bound_subagent_worker(worker, params)
    finally:
        _close_worker_mcp_clients(worker, params)


# LLM: 仅清理worker独立tools；父身份取原运输scope，不猜owner或关闭共享注册表，异常只告警。
# 函数用途: 在worker被替换或退出时释放其MCP客户端，不掩盖业务返回或异常。
def _close_worker_mcp_clients(worker, params: RunSubagentWorkerParams) -> None:
    tools = getattr(worker, "tools", None)
    scope = getattr(params.request_transport, "scope", None)
    parent = getattr(getattr(scope, "execution", None), "agent", None)
    if tools is None or tools is getattr(parent, "tools", None):
        return
    try:
        tools.close_mcp_clients()
    except Exception:
        _LOGGER.warning("WORKER_MCP_CLEANUP_UNAVAILABLE", exc_info=True)


# LLM: builder已核原dispatch与canonical child，原激活/配置/租约/结果/可恢复片收口顺序保main；只有原请求短门授写，模型等待不持T。
# 函数用途: 执行当前准确归属的子代理工作片，未结沿原异常出口保留责任。
def _run_bound_subagent_worker(worker, params):
    attempt_id = ""
    launch_id = ""
    if not params.dry_run:
        try:
            prepared = worker.subagents.lifecycle.prepare_runner_attempt(
                params.run_id, retry_reason=params.retry_reason,
                expected_attempt_id=params.expected_attempt_id,
            )
        except RuntimeConflictError as exc:
            return SubAgentRunnerResult(
                run_id=params.run_id, dry_run=False, ok=False, status="CANCELLED",
                verification_status="UNVERIFIED", message=f"启动接纳已失效：{exc}",
            )
        attempt_id = prepared.runner_active_attempt_id
        start = (prepared.attributes or {}).get("background_start") or {}
        if start.get("attempt_id") == attempt_id:
            launch_id = str(start.get("launch_id") or "")
        worker._runner_activity_attempt_id = attempt_id
    try:
        if not params.dry_run:
            _record_effective_config_overlay(worker, params.run_id, prepared, attempt_id=attempt_id)
        with runner_session_lease(
            RunnerSessionPoolLease(
                manager=worker.subagents,
                run_id=params.run_id,
                worker_id=f"subagent-worker:{params.run_id}",
                interval_seconds=_RUNNER_SESSION_HEARTBEAT_SECONDS,
                activity_observer=partial(observe_runner_activity, worker, params.run_id),
                attempt_id=attempt_id,
                launch_id=launch_id,
                request_transport=params.request_transport,
            )
        ):
            if params.dry_run:
                result = worker.run_subagent(
                    params=SubagentRunParams(
                        run_id=params.run_id,
                        instruction=params.instruction,
                        dry_run=params.dry_run,
                        max_cards=params.max_cards,
                        probe=params.probe,
                        retry_reason=params.retry_reason,
                    )
                )
            elif params.timeout_seconds <= 0:
                result = _run_subagent_worker_interruptibly(worker, params, attempt_id=attempt_id)
            else:
                result = _run_subagent_worker_with_timeout(worker, params, attempt_id=attempt_id)
    except InterruptedError:
        raise
    except Exception as exc:
        _raise_transport_unknown(exc)
        if params.dry_run:
            raise
        # 首次租约发布失败也要沿原结果门释放已领取的执行权，不能留下活进程持有的空运行轮。
        result = worker.subagents.runner_result.record_runner_result(RecordRunnerResultParams(
            run_id=params.run_id, attempt_id=attempt_id, dry_run=False, ok=False,
            message=f"runner worker failed: {exc}", status="FAILED",
            verification_status="UNVERIFIED", failure_type=FailureType.RUNNER_WORKER_ERROR.value,
        ))
    result = _reconcile_timed_out_runner(
        worker,
        params.run_id,
        result,
        timeout_seconds=params.timeout_seconds,
    )
    settle_resumable_worker_slice(worker, params.run_id, result, attempt_id=attempt_id, dry_run=params.dry_run)
    _continue_pending_run_after_session(worker, params.run_id, attempt_id=attempt_id)
    _resume_direct_parent_after_session(worker, params.run_id)
    return result


# LLM: 只读本进程模型调用准入表（contracts.model_call_ledger.model_call_admission_closure），不读文本。宿主停机关门后，
#   新启动的 runner 第一次模型调用必被拒，会白占尝试次数并写成 FAILED；所以这里不激活执行轮、不写结果或 FAILED，返回只在内存里的
#   拒绝结果（status=PENDING），原预留（RuntimeDB pending attempt / 文件 background_start）原样保留，重启后孤儿监督或同步派工
#   按同一执行轮续上。local/main 的派工子进程有自己的准入表，不随 Gateway 关门，也不随它退出，这里对它没有影响。
#   预览（dry_run）不发模型调用，不拦。改动同步 test_subagent_reserved_start_restart_pickup。
# 函数用途: 宿主停机后不再启动新的子代理 runner，返回不落盘的拒绝结果；没停机或只是预览时返回 None。
def _admission_closed_refusal(params: RunSubagentWorkerParams) -> SubAgentRunnerResult | None:
    from ...contracts.model_call_ledger import (
        MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE,
        model_call_admission_closure,
    )

    if params.dry_run or model_call_admission_closure() is None:
        return None
    return SubAgentRunnerResult(
        run_id=params.run_id, dry_run=False, ok=False, status="PENDING", verification_status="UNVERIFIED",
        message="宿主停机中：模型调用准入已关闭，没有启动 runner，预留的执行轮留到重启后续上",
        turn_end_reason=FailureType.MODEL_CALL_ADMISSION_CLOSED.value,
        runner_last_error=MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE,
    )


# LLM: 原 result 可能早于并发授权；session 结束后复读 canonical，只为原 attempt 的 PENDING 接续。
# 不改历史结果，不复活 BLOCKED/控制终态；等待、session、RuntimeDB 与启动防重仍由原 auto-start 守门。
# 函数用途: 工作片退出后把最新待续跑状态交给原派工入口，避免已消费的授权事件留下无人接续的任务。
def _continue_pending_run_after_session(worker, run_id: str, *, attempt_id: str) -> None:
    if not attempt_id:
        return
    try:
        from ...subagents.direct_parent_lifecycle import parent_wait_blocks_dispatch

        manager = getattr(worker, "subagents", None)
        if manager is None:
            return
        task = manager.load(run_id)
        if str(task.status or "").strip().upper() != "PENDING":
            return
        session = (getattr(task, "attributes", {}) or {}).get("runner_session") or {}
        if getattr(task, "runner_active_attempt_id", "") or str(session.get("attempt_id") or "") != attempt_id:
            return
        if parent_wait_blocks_dispatch(task):
            # This PENDING state is an intentional event wait, not an orphan.
            return
        from ..orchestration.dispatch.capability_auto_sweep import (
            auto_start_orphan_run,
        )

        auto_start_orphan_run(worker, run_id)
    except Exception:
        # A continuation launch failure must not turn an already persisted
        # runner result into a process failure. Periodic supervision will
        # re-read the same PENDING run and retry from durable state.
        _LOGGER.warning(
            "source continuation auto-start failed (run_id=%s)",
            run_id,
            exc_info=True,
        )
        return


# LLM: 新结果只解除直属父级的等待，不等全部兄弟；原 auto-start 的 session/attempt 栅栏阻止双执行。
# 函数用途: 孩子收口后让空闲父级及时处理结果，已在工作的父级不会另开副本。
def _resume_direct_parent_after_session(worker: object, child_run_id: str) -> None:
    try:
        from ...subagents.direct_parent_lifecycle import (
            reconcile_parent_wait_for_child,
        )
        from ..orchestration.dispatch.capability_auto_sweep import (
            auto_start_orphan_run,
        )

        decision = reconcile_parent_wait_for_child(worker.subagents, child_run_id)
        if decision.should_resume:
            auto_start_orphan_run(worker, decision.parent_run_id)
    except Exception:
        # The durable wait marker remains recoverable by periodic supervision;
        # a parent wake failure must not rewrite the child's persisted result.
        _LOGGER.warning(
            "direct parent resume failed (child_run_id=%s)",
            child_run_id,
            exc_info=True,
        )


# LLM: 保原owner/dispatch授权与模型引用；配置重建前清理旧worker独立MCP，child原roots/dispatch来源保持。
# 函数用途: 构造子代理依赖并恢复正确模型配置，元数据在准确激活后才条件保存。
def _build_worker_agent(simple_agent_cls, params: RunSubagentWorkerParams):
    worker = _construct_transported_worker(simple_agent_cls, params, params.config)
    _attach_worker_runtime(worker, params)
    # 3.txt B.4：dispatch/resume 执行阶段过统一授权查询门。系统驱动（调度器）
    # 无 requester run_id——只做 owner 一致性 + ID 形态 + 存在性；owner 不
    # 匹配 = 数据异常（任务归属另一 owner 域），fail-closed 拒绝派工。
    task = authorize_operation(
        worker.subagents,
        OperationRequest(
            operation="dispatch",
            run_id=params.run_id,
            requester_owner=str(getattr(worker.subagents, "owner_id", "") or ""),
        ),
    )
    from ...settings.model_profiles import inherited_model_config

    effective_config = apply_task_runtime_config_overlay(
        inherited_model_config(worker, task),
        task,
        workspace_root=worker.subagents.workspace_root,
    )
    if effective_config is not params.config:
        _close_worker_mcp_clients(worker, params)
        worker = _construct_transported_worker(simple_agent_cls, params, effective_config)
        _attach_worker_runtime(worker, params)
    if params.request_transport is not None:
        from ...gateway_parts.multiprocess_child_execution_binding import (
            capture_original_child_dispatch,
        )

        capture_original_child_dispatch(params.request_transport, worker, task)
    return worker


# LLM: 无运输保main构造签名；真实child仅复用原scope实际Agent的constructor roots及W bootstrap，不从task/请求补根或更换owner。
# 函数用途: 构造同来源子代理依赖，W不能借普通Agent初始化旁路绕过原资源门。
def _construct_transported_worker(simple_agent_cls, params, config):
    if params.request_transport is None:
        return simple_agent_cls(config, params.root)
    original = params.request_transport.scope.execution.agent
    options = {"workspace_roots": original.workspace_roots}
    frozen = getattr(original, "_worker_bootstrap", None)
    if frozen is not None:
        options["worker_bootstrap"] = frozen
    return simple_agent_cls(config, params.root, **options)


# LLM: 每个建好的 worker（含按有效配置重建的那个）都经这里。它的模型调用账本在构造时已登记进进程准入表
#   （contracts.model_call_ledger），随 Gateway 停机一并关门结清，这里不用再登记。
# 函数用途: 给 worker 装上后端覆盖和本地存储。
def _attach_worker_runtime(worker, params: RunSubagentWorkerParams) -> None:
    if params.backend_override is not None:
        worker.backend = params.backend_override
        worker._subagent_worker_backend_override = params.backend_override
    _attach_worker_local_store(worker, params.local_store)


# LLM: 激活后在 C 内按原 attempt 窄更新；不以锁外 load/save 覆盖停止或换代后的任务。
# 函数用途: 记录本轮实际配置来源，迟到 worker 不能改写新轮元数据。
def _record_effective_config_overlay(worker, run_id: str, task, *, attempt_id: str) -> None:
    from ...common.request_execution_ports import gateway_short_transaction

    with gateway_short_transaction("child_config_overlay_commit"):
        identity = getattr(task, "runtime_identity", None)
        overlay_ref = str(getattr(identity, "config_overlay_ref", "") or "").strip()
        if not overlay_ref:
            return
        overlay = {
            "schema_version": "runtime_config_overlay.v1",
            "overlay_ref": overlay_ref,
            "scope": str(getattr(identity, "config_scope", "") or "run"),
            "config_sources": getattr(worker.config, "config_sources", {}),
            "config_layers": list(getattr(worker.config, "config_layers", []) or []),
            "warnings": list(getattr(worker.config, "config_warnings", []) or []),
        }

        # LLM: 同一 canonical mutation 再核准确执行轮；异常不写入，不重建整个旧任务快照。
        # 函数用途: 只保存本次负责的配置诊断字段。
        def record(current):
            if current.runner_active_attempt_id != attempt_id or current.status != "RUNNING":
                raise RuntimeConflictError("配置元数据的原执行轮已失效")
            current.attributes = {**(current.attributes or {}), "runtime_config_overlay": overlay}

        with worker.subagents.creation_guard():
            if runner_attempt_cancelled(worker.subagents, run_id, attempt_id):
                raise RuntimeConflictError("配置元数据的原执行权已关闭")
            worker.subagents.mutate(run_id, record)


def _attach_worker_local_store(worker, local_store: object | None) -> None:
    if local_store is None:
        return
    worker.local_store = local_store
    worker.subagents.local_store = local_store
    worker.memory.local_store = local_store


# LLM: 真实执行前绑定 exact attempt 和已解析 worker.home_paths 的排队 owner；退出复位，不改变取消/权限权威。
# 函数用途: 给普通子代理建立可中断边界与匿名公平身份，停止不影响其它代理。
def _run_subagent_worker_interruptibly(
    worker,
    params: RunSubagentWorkerParams,
    *,
    attempt_id: str,
):
    from ...concurrency.interrupt import register_interruptible

    interrupt_name = f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    with register_interruptible(interrupt_name), provider_agent_owner_scope(worker):
        if runner_attempt_cancelled(worker.subagents, params.run_id, attempt_id):
            raise InterruptedError("原子代理执行轮已停止")
        return worker.run_subagent(
            params=SubagentRunParams(
                run_id=params.run_id,
                instruction=params.instruction,
                dry_run=False,
                max_cards=params.max_cards,
                probe=params.probe,
                retry_reason=params.retry_reason,
                attempt_id=attempt_id,
            )
        )


# LLM: 显式timeout线程绑定原attempt/owner/child source；正MP的start ACK也进门外stop/join，unknown同原出处上抛，Count0原序保持。
# 函数用途: 为有时间预算的子代理启动准确身份的执行线程，到期按原合同收口。
def _run_subagent_worker_with_timeout(worker, params: RunSubagentWorkerParams, *, attempt_id: str):
    from ...concurrency.interrupt import (
        interrupt_by_name,
        register_interruptible,
        set_interrupt,
    )

    interrupt_name = (
        f"subagent-runner-attempt:{params.run_id}:{attempt_id}"
    )
    payload: dict[str, object] = {}
    timeout_requested = threading.Event()

    # LLM: 新线程不隐式继承ContextVar；原transport显式重入，ACK失败的原abort事件先于业务，不把迟到出生变新模型调用。
    # 函数用途: 执行可中断的子代理调用，把真实结果或异常交回原超时收口。
    def _target() -> None:
        try:
            if params.request_transport is not None and timeout_requested.is_set():
                raise InterruptedError("原worker启动确认失败，停止未进入的业务")
            from ...gateway_parts.multiprocess_subagent_execution import child_worker_scope

            with child_worker_scope(params.request_transport, worker), register_interruptible(interrupt_name), provider_agent_owner_scope(worker):
                if runner_attempt_cancelled(worker.subagents, params.run_id, attempt_id):
                    raise InterruptedError("原子代理执行轮已停止")
                # Close the registration race: a very short timeout may fire
                # before this thread is scheduled.  In that case the target
                # marks itself interrupted before entering any model/tool work.
                if timeout_requested.is_set():
                    set_interrupt(True)
                payload["result"] = worker.run_subagent(
                    params=SubagentRunParams(
                        run_id=params.run_id,
                        instruction=params.instruction,
                        dry_run=False,
                        max_cards=params.max_cards,
                        probe=params.probe,
                        retry_reason=params.retry_reason,
                        attempt_id=attempt_id,
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            payload["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    origin = (_capture_runner_thread(thread, "child_runner_worker_stop", params.run_id, attempt_id)
              if params.request_transport is not None else None)
    if origin is None:
        thread.start()
    primary = None

    # LLM: 只停止原设施，event同时关闭ACK失败后尚未进入的目标；不修改canonical、claim或新线程。
    # 函数用途: 在门外请求原timeout worker退出，并覆盖中断注册前的窗口。
    def stop_original_worker():
        timeout_requested.set()
        interrupt_by_name(interrupt_name)

    try:
        if origin is not None:
            thread.start()
        try:
            task = worker.subagents.load(params.run_id)
        except (AttributeError, OSError, TypeError, ValueError):
            task = None
        if _uses_inactivity_timeout(task):
            _join_until_inactive(
                worker,
                params.run_id,
                thread,
                idle_timeout_seconds=params.timeout_seconds,
            )
        else:
            thread.join(params.timeout_seconds)
        if thread.is_alive():
            return _close_timed_out_worker(worker, params, attempt_id, thread, timeout_requested, interrupt_name)
        if "error" in payload:
            raise payload["error"]  # type: ignore[misc]
        return payload["result"]  # type: ignore[return-value]

    except BaseException as exc:
        primary = exc
        raise
    finally:
        if origin is not None:
            _finish_original_runner_thread(origin, stop_original_worker, primary)


# LLM: 原TIMEOUT先持久fence再中断，未确认原线程退出不能让独立child运输封口；join只在T/业务锁外，Count0仍即时返回，不声称任意线程可强制终止。
# 函数用途: 保存原超时事实并让运输等原实际执行线程退出，避免提前释放P3。
def _close_timed_out_worker(worker, params, attempt_id, thread, timeout_requested, interrupt_name):
    from ...concurrency.interrupt import interrupt_by_name

    if params.request_transport is None:
        result = _record_original_timeout(worker, params, attempt_id)
        timeout_requested.set()
        interrupt_by_name(interrupt_name)
        return result
    try:
        return _record_original_timeout(worker, params, attempt_id)
    finally:
        timeout_requested.set()
        interrupt_by_name(interrupt_name)
        thread.join()


# LLM: 原runner_result/abandon顺序与字段保持main，短写由原末门核；不能把物理未结伪造成未进入或重放许可。
# 函数用途: 保存同一attempt的超时结果并撤销迟到提交权限。
def _record_original_timeout(worker, params, attempt_id):
    timeout_message = f"runner timed out after {params.timeout_seconds:.2f}s"
    result = worker.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message=timeout_message,
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type=FailureType.RUNNER_TIMEOUT.value,
        )
    )
    worker.subagents.lifecycle.abandon_runner_attempt(params.run_id, attempt_id, reason=timeout_message)
    return result


def _uses_inactivity_timeout(task: object) -> bool:
    from ...common.audit_activation import (
        structured_audit_supervised_worker_attributes,
    )

    return structured_audit_supervised_worker_attributes(
        getattr(task, "attributes", {}) or {}
    )


def _join_until_inactive(
    worker: object,
    run_id: str,
    thread: threading.Thread,
    *,
    idle_timeout_seconds: float,
) -> None:
    """Wait for one Audit attempt, timing out only after no real activity.

    Model stream chunks and persisted successful tool progress are the two
    authoritative activity sources.  Runner-session heartbeats are omitted:
    they prove only that the host loop is alive, not that work is advancing.
    """

    idle_timeout = max(0.01, float(idle_timeout_seconds or 0.0))
    poll_seconds = min(1.0, max(0.01, idle_timeout / 10.0))
    last_activity = time.monotonic()
    previous = _runner_activity_token(worker, run_id)
    while thread.is_alive():
        thread.join(poll_seconds)
        if not thread.is_alive():
            return
        current = _runner_activity_token(worker, run_id)
        if current != previous:
            previous = current
            last_activity = time.monotonic()
            continue
        if time.monotonic() - last_activity > idle_timeout:
            # Close the tiny sample/check race: a stream chunk can arrive
            # after the snapshot above but before the timeout comparison.
            latest = _runner_activity_token(worker, run_id)
            if latest != current:
                previous = latest
                last_activity = time.monotonic()
                continue
            return


def _runner_activity_token(worker: object, run_id: str) -> tuple[object, ...]:
    ledger = getattr(worker, "_model_call_ledger", None)
    records = getattr(ledger, "records", None)
    model_activity: tuple[tuple[object, ...], ...] = ()
    if callable(records):
        try:
            model_activity = tuple(
                (
                    str(getattr(record, "call_id", "") or ""),
                    str(getattr(record, "status", "") or ""),
                    float(getattr(record, "last_activity_at", 0.0) or 0.0),
                )
                for record in records()
                if not run_id or str(getattr(record, "run_id", "") or "") == run_id
            )
        except (AttributeError, TypeError, ValueError):
            model_activity = ()
    try:
        task = worker.subagents.load(run_id)
        tool_progress = float(getattr(task, "last_progress_at", 0.0) or 0.0)
    except (AttributeError, OSError, TypeError, ValueError):
        tool_progress = 0.0
    return (model_activity, tool_progress)


def _reconcile_timed_out_runner(
    worker,
    run_id: str,
    result: SubAgentRunnerResult,
    *,
    timeout_seconds: float = 0.0,
) -> SubAgentRunnerResult:
    """Make the authoritative timeout win after the session heartbeat stops."""
    if str(result.status or "").upper() != "TIMEOUT":
        return result
    task = worker.subagents.load(run_id)
    if str(task.status or "").upper() in {"ABANDONED", "CANCELLED", "TAKEN_OVER"} or task.takeover_by:
        return result
    from ...common.audit_activation import (
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    if structured_audit_source_worker_attributes(
        getattr(task, "attributes", {}) or {}
    ):
        return _reconcile_audit_source_worker_slice(
            worker,
            task,
            result,
            timeout_seconds=timeout_seconds,
        )
    if structured_audit_source_binding_attributes(
        getattr(task, "attributes", {}) or {}
    ):
        binding_result = _reconcile_audit_source_binding_slice(
            worker,
            task,
            result,
        )
        if binding_result is not None:
            return binding_result
    now = time.time()
    task.status = "TIMEOUT"
    task.verification_status = result.verification_status or "UNVERIFIED"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.result = result.message
    task.runner_attempts = max(
        int(task.runner_attempts or 0),
        int(result.runner_attempts or 0),
        len(task.runner_abandoned_attempt_ids),
    )
    task.runner_last_error = result.runner_last_error or result.message
    task.runner_active_attempt_id = ""
    task.ended_at = now
    task.updated_at = now
    task.heartbeat_at = now
    worker.subagents.save(task)
    return result


# LLM: A timed-out Audit source runner loses its attempt and ephemeral lease,
# while the durable watch, pending ACKs and stable worker task remain canonical.
# 函数用途: 来源子代理卡死或工作片到期时原地续派；窗口已结束且欠账清零则结构化完成。
def _reconcile_audit_source_worker_slice(
    worker,
    task,
    result: SubAgentRunnerResult,
    *,
    timeout_seconds: float = 0.0,
) -> SubAgentRunnerResult:
    from ...ingestion.source_worker import (
        record_source_worker_recovery,
        source_worker_task_incomplete,
    )
    from ...subagents.process_control import reclaim_background_start

    now = time.time()
    incomplete = source_worker_task_incomplete(task, now=now)
    progressed_at = float(getattr(task, "last_progress_at", 0.0) or 0.0)
    # record_runner_result() writes runner_last_attempt_at at result time, so it
    # cannot be reused as this attempt's start boundary here.  The timeout
    # window is the execution boundary already enforced by the runner.  A
    # successful tool progress event inside that window proves this was a
    # bounded slice rotation; no such event means the slice actually stalled.
    progress_boundary = now - max(0.0, float(timeout_seconds or 0.0))
    recovery_reason = (
        "slice_rotation"
        if timeout_seconds > 0 and progressed_at >= progress_boundary
        else "runner_timeout"
    )
    record_source_worker_recovery(task, reason=recovery_reason, now=now)
    task.runner_active_attempt_id = ""
    task.updated_at = now
    task.failure_type = ""
    task.runner_last_error = ""
    if incomplete:
        task.status = "PENDING"
        task.verification_status = "UNVERIFIED"
        task.ended_at = 0.0
        reclaim_background_start(task)
        result.ok = False
        result.status = "PENDING"
        result.verification_status = "UNVERIFIED"
        result.message = "Audit source worker slice expired; durable work requeued"
        log_status = "PENDING"
    else:
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        task.ended_at = now
        result.ok = True
        result.status = "DONE"
        result.verification_status = "VERIFIED"
        result.message = "Audit source window complete and backlog settled"
        log_status = "DONE"
    worker.subagents.save(task)
    worker.subagents.actions._append_task_work_log(
        task,
        f"audit_source_slice: status={log_status} reason={recovery_reason}",
    )
    return result


def _reconcile_audit_source_binding_slice(
    worker,
    task,
    result: SubAgentRunnerResult,
) -> SubAgentRunnerResult | None:
    """Retry the same pre-binding source run while its named Audit is active."""

    from ...common.audit_activation import (
        AUDIT_DEADLINE_ATTR,
    )
    from ...conversation.authority import CONVERSATION_REQUEST_ID_ATTR
    from ...ingestion.source_worker import (
        audit_parent_reconcile_state,
        record_source_worker_recovery,
    )
    from ...subagents.process_control import reclaim_background_start

    attrs = dict(getattr(task, "attributes", {}) or {})
    now = time.time()
    try:
        deadline = float(attrs.get(AUDIT_DEADLINE_ATTR) or 0.0)
    except (TypeError, ValueError):
        deadline = 0.0
    if deadline > 0 and now >= deadline:
        return None
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    active, _state = audit_parent_reconcile_state(worker, audit_id)
    if not active:
        return None
    record_source_worker_recovery(task, reason="runner_timeout", now=now)
    task.runner_active_attempt_id = ""
    task.status = "PENDING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.runner_last_error = ""
    task.ended_at = 0.0
    task.updated_at = now
    reclaim_background_start(task)
    result.ok = False
    result.status = "PENDING"
    result.verification_status = "UNVERIFIED"
    result.message = "Audit source binding slice expired; same run requeued"
    worker.subagents.save(task)
    worker.subagents.actions._append_task_work_log(
        task,
        "audit_source_binding_slice: status=PENDING reason=runner_timeout",
    )
    return result
