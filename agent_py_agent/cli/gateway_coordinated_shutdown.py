# LLM: 只消费本次原C资源DTO与GatewayDrainRuntime；关producer在前，原W/Thread join与六源观察在门外，未知不删P3/pin或伪证已排空。
# 模块用途: 关闭原协调者资源；完整正常收尾后封存本代v2并发表绑定末态，未知或提交失败仍给不完整回执。
from __future__ import annotations

import time

from ..agent.gateway_parts.daemon_control import (
    remove_gateway_stop_request_if_owned,
    remove_pid_file_if_owned,
)


# LLM: 一次原assess成功后prepare仅消费同次内部观察并close_native，不在rechecks停止后再次核实；失败保原异常与未知。
# 函数用途: 按原物理观察核对协调者与worker收口情况，返回真实阻塞原因。
def _assess_coordinator_resources(request, runtime, *, worker_error, producer_error, alive_threads):
    complete = not alive_threads and not worker_error
    blockers = []
    if runtime is None or producer_error:
        complete = False
        blockers.append({"kind": "inventory", "item_ref": "original-runtime", "reason": producer_error or "original_runtime_missing"})
    else:
        try:
            assessments = runtime.assess()
            complete = complete and bool(assessments) and all(item.drained for item in assessments)
            blockers = [{"kind": block.kind, "item_ref": block.item_ref, "reason": block.reason}
                        for assessment in assessments for block in assessment.blockers]
        except (OSError, RuntimeError, ValueError) as error:
            complete = False
            blockers.append({"kind": "inventory", "item_ref": "original-runtime", "reason": type(error).__name__})
    if complete:
        try:
            runtime.prepare_generation_closure()
        except (OSError, RuntimeError, ValueError) as error:
            complete, worker_error = False, type(error).__name__
    return complete, blockers, worker_error


# LLM: 仅join DTO内本次实际Thread；启动未到该步骤可None，所有已born原资源仍由runtime collector独立复核，缺字段不是退出证明。
# 函数用途: 等待已经创建的协调者循环，部分启动失败也能走同一收尾入口。
def _join_original_loops(request):
    threads = {"heartbeat": request.heartbeat_thread, "request": request.request_thread,
               "background": request.background_thread}
    for thread in threads.values():
        if thread is not None and thread.ident is not None:
            thread.join(timeout=2)
    return [name for name, thread in threads.items() if thread is not None and thread.is_alive()]


# LLM: 原lease内关producer/W/HTTP/循环，原runtime完整观察后关native；全部原收尾完成才一次封存v2，失败保未知，不构造Agent或追补旧代。
# 函数用途: 收起本代的实际设施并保留未结资源，返回可供正式CLI判断退出码的结构化回执。
def cleanup_coordinator_resources(request):
    from . import gateway_process as api
    from .gateway_coordinated_run import coordinated_state_fields

    context, agent, paths = request.context, request.context.agent, request.context.paths
    coordinated_state_fields(context)
    request.stop_event.set()
    runtime = context.drain_runtime
    producer_error = worker_error = ""
    if runtime is not None:
        try:
            runtime.stop_producers()
        except (OSError, RuntimeError, ValueError) as error:
            producer_error = type(error).__name__
    if agent is not None:
        api._cancel_active_decisions(request)
    if request.coordinator_workers is not None:
        try:
            request.coordinator_workers.close(defer_native=True)
        except Exception as error:
            worker_error = type(error).__name__
    if request.http_server is not None:
        request.http_server.stop()
    alive = _join_original_loops(request)
    complete, blockers, worker_error = _assess_coordinator_resources(
        request, runtime, worker_error=worker_error, producer_error=producer_error, alive_threads=alive,
    )
    calls = executors = background = 0
    if agent is not None:
        calls = api._settle_interrupted_model_calls(request, drain_complete=complete)
        executors = api._mark_host_shutdown_executors(request)
        background = api._record_surviving_background_sessions(request)
        api._flush_decision_reach_counts(request)
    try:
        remove_pid_file_if_owned(paths.pid)
    except OSError:
        pass
    remove_gateway_stop_request_if_owned(paths.stop_request, api._gateway_context_process_identity(context),
                                        process_started_at=context.process_started_at)
    api._write_gateway_heartbeat(paths, agent, status=request.termination_status, pid=request.pid)
    payload = {"status": "cleanup", "pid": request.pid, "termination_status": request.termination_status,
               "termination_reason": request.termination_reason, "drain_complete": complete,
               "alive_threads": alive, "worker_exit_error": worker_error, "drain_blockers": blockers,
               "interrupted_model_calls": calls, "host_shutdown_executors": executors,
               "surviving_background_sessions": background, "updated_at": time.time()}
    if complete:
        try:
            runtime.seal_generation()
        except (OSError, RuntimeError, ValueError) as error:
            complete = False
            payload.update(drain_complete=False, worker_exit_error=type(error).__name__)
            payload['drain_blockers'].append({'kind': 'inventory', 'item_ref': 'original-generation',
                                              'reason': type(error).__name__})
    if agent is not None:
        api.log_gateway_event(agent, "gateway_run_cleanup", payload)
    print(f"[gateway-run] status={request.termination_status} pid={request.pid} drain_complete={str(complete).lower()}", flush=True)
    return payload
