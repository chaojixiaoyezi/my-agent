# LLM: 仅内部正count启动原C，先只读定位再原生命周期锁/接手门；不以配置、DTO、JSON或None Agent签业务权，Count0不导入本模块。
#   C state原publisher与角色双读按同controller→lease共门，失败/closing元数据不借业务许可过滤；同步restart取消固定caller锁序。
# 模块用途: 把多进程协调者接到正式CLI启动、原资源准备和精确停止回执，保持完整元数据发表与来源双读互斥。
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import replace

from ..agent.auth.configuration import AuthConfigurationError
from ..agent.gateway_parts.http_service import GatewayLocalCredentialRequired
from ..agent.gateway_parts.io import read_json_file, write_json_file_atomic
from ..agent.gateway_parts.multiprocess_coordinator import (
    CoordinatorUnavailable,
    coordinator_guard,
    current_coordinator,
)
from ..agent.gateway_parts.paths import gateway_paths_from_root
from .models import GatewayRunCleanupRequest, GatewayThreadsRequest


# LLM: 拒绝只输出已登记结构码，不写状态、建owner或从异常文案猜恢复；调用方保原生命周期责任。
# 函数用途: 对内部启动条件不满足返回退出码2，不启动业务。
def coordinated_refusal(code: str, *, reason: str = "") -> int:
    print(json.dumps({"ok": False, "error_code": code, "reason": reason}, sort_keys=True), file=sys.stderr)
    return 2


# LLM: 仅冷读同CLI配置/规范路径，不构造Agent、Home或登记；原C锁取得后仍由build_control_plane复核同根。
# 函数用途: 在业务构造前定位本安装的原协调者锁目录。
def _cold_gateway_paths(args):
    from .common import apply_runtime_config_environment, load_config
    from .workspace_resolution import runtime_paths_for_config

    config = apply_runtime_config_environment(load_config(args.config))
    return gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])


# LLM: 正count仅内部Namespace，校验先于配置/目录；完整原coordinator_guard包围真实准备、服务和收尾，原P2决定owner物化。
# 函数用途: 在等待前任成功后争取原C单例，再运行本次真正的多进程生命周期。
def run_coordinated_gateway(args, *, predecessor_exited: bool) -> int:
    count = getattr(args, "_gateway_worker_count", 0)
    if type(count) is not int or count <= 0:
        return coordinated_refusal("COORDINATOR_PLATFORM_OR_MODE_UNSUPPORTED")
    if not predecessor_exited:
        return coordinated_refusal("GATEWAY_PREDECESSOR_TIMEOUT")
    try:
        paths = _cold_gateway_paths(args)
        with coordinator_guard(paths.root, count):
            return _construct_coordinated_gateway(args, paths)
    except CoordinatorUnavailable as error:
        return coordinated_refusal(error.error_code)


# LLM: 只有实际持锁C调用原接手controller和冷控制组合；冻结args为本次副本，不覆盖外部调用者或用假Agent填接口。
# 函数用途: 固定本代控制面与配置，业务factory继续由原P2门决定是否执行。
def _construct_coordinated_gateway(args, paths) -> int:
    from ..agent.gateway_parts.multiprocess_takeover_runtime import prepare_coordinator_takeover
    from . import gateway_process as api
    from .gateway_control_plane import build_control_plane

    generation = getattr(args, "_gateway_c_run_id", "") or uuid.uuid4().hex
    controller = prepare_coordinator_takeover(paths, args._gateway_worker_count, generation)
    if controller is None:
        return coordinated_refusal("COORDINATOR_ALREADY_RUNNING", reason="coordinator_context_unavailable")
    control = build_control_plane(args, paths, controller, api.make_agent)
    frozen_args = copy.copy(args)
    frozen_args._gateway_control_plane = control
    frozen_args._gateway_frozen_config = control.config
    context = api._gateway_run_context_from_args(api._GatewayRunBuildRequest(None, paths, frozen_args))
    try:
        from .gateway_control_plane import preflight_control_credential

        preflight_control_credential(control)
    except (GatewayLocalCredentialRequired, AuthConfigurationError) as error:
        print(str(error), file=sys.stderr)
        return 2
    cleanup = _prepare_coordinated_resources(context)
    return _serve_coordinated_gateway(cleanup)


# LLM: 只从当前原lease投影代次，不读旧state充权；Count0不调用，发布不能冒充ready或退出证明。
# 函数用途: 生成正式C状态中需要携带的准确分区数和代次。
def coordinated_state_fields(context) -> dict:
    from ..agent.gateway_parts.control_plane import GatewayControlPlane

    control = context.control_plane
    lease = current_coordinator(context.paths.root, context.coordinator_worker_count)
    if (type(control) is not GatewayControlPlane or lease is None or not lease.c_run_id
            or context.paths is not control.paths or context.coordinator_worker_count != control.worker_count
            or getattr(control, "_coordinator_role_lease", None) is not lease
            or lease.control_plane is not control or control.c_run_id != lease.c_run_id):
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    return {"c_run_id": lease.c_run_id, "worker_count": lease.count}


# LLM: 只从同一真实原lease.root和既有路径合同取准确state值；可变DTO晚改不是重定位授权，读写都消费返回的不可变Path值，不二次读DTO。
# 函数用途: 固定本代规范元数据文件，拒绝把协调者状态改写到其它位置。
def coordinated_state_path(context):
    coordinated_state_fields(context)
    original = context.control_plane._coordinator_role_lease
    target = gateway_paths_from_root(original.root).state
    if context.paths.state != target:
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    return target


# LLM: payload仍门外准备；原controller→lease与完整角色双读共门，末核同实际source才原子写，不以隔离/Root closing/recheck停止拒绝失败元数据。
# 函数用途: 串行发表本代完整state，避免合法启动投影撞双读；撤权、换控制器/代次或错路径仍拒绝旧writer。
def publish_coordinated_state(context, payload) -> None:
    from ..agent.gateway_parts.control_plane import GatewayControlPlane
    from ..agent.gateway_parts.multiprocess_takeover_runtime import CoordinatorTakeover

    control = context.control_plane
    original = getattr(control, "_coordinator_role_lease", None)
    controller = control.controller() if type(control) is GatewayControlPlane else None
    if (type(controller) is not CoordinatorTakeover or original is None
            or original.controller is not controller or controller._lease is not original):
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    with controller._mutex, original._lifecycle_gate:
        if (context.control_plane is not control or control.controller() is not controller
                or original.controller is not controller or controller._lease is not original
                or controller.paths is not control.paths or controller.count != control.worker_count
                or controller.c_run_id != control.c_run_id):
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        target = coordinated_state_path(context)
        fields = coordinated_state_fields(context)
        write_json_file_atomic(target, {**payload, **fields})


# LLM: 仅原C/Root在任何设施出生前调用一次；用同一原P3门和startup=True恢复，不从cold/首次tick猜预算。
#   B4不在本批：原有效计划标记只读预检后明确拒绝且保留，不消费/伪发续跑；未知P2/Runtime仍保原责任。
# 函数用途: 沿原恢复函数处理启动遗留processing；不构造Agent、改重试规则或把恢复摘要当物理退出证明。
def recover_coordinated_startup(context):
    from ..agent.gateway_parts import is_pid_alive
    from ..agent.gateway_parts.multiprocess_admission import coordinator_admission_gate
    from ..agent.gateway_parts.recovery import (
        _non_negative_int,
        _RecoveryContext,
        recover_gateway_processing_requests_report,
    )
    from ..agent.gateway_parts.restart_service import consume_restart_marker
    from .gateway_loops import _require_coordinator_loop_source

    _require_coordinator_loop_source(context)
    coordinated_state_path(context)
    control, runtime = context.control_plane, context.drain_runtime
    canonical_paths = gateway_paths_from_root(runtime._lease_origin.root)
    if consume_restart_marker(canonical_paths, old_pid_alive=is_pid_alive, consume=False) is not None:
        raise CoordinatorUnavailable("GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED")
    gate = coordinator_admission_gate(control, context.paths, context)
    runtime.retain_admission(gate)
    config = control.config
    params = _RecoveryContext(
        time.time(), _non_negative_int(config.gateway_request_max_attempts, default=1),
        max(1, int(config.gateway_processing_timeout_seconds or 1)), True, control, None,
        admission_gate=gate,
    )
    report = recover_gateway_processing_requests_report(context.paths, params=params)
    _require_coordinator_loop_source(context)
    publish_coordinated_state(context, {
        **read_json_file(coordinated_state_path(context)),
        "requeued_requests": report.summary["requeued"],
        "failed_processing_requests": report.summary["failed"],
    })
    if report.load_errors:
        print(json.dumps({"event": "gateway_startup_recovery", "load_error_count": len(report.load_errors),
                          **report.summary}, sort_keys=True), flush=True)
    return report


# LLM: 原HTTP/worker/Thread出生前建同一个原P6观察根；业务构造由control原P2门执行，初始化异常只清本次实际资源。
# 函数用途: 先沿原门恢复启动遗留，再准备监听/业务Agent及worker；恢复错误保原事实，全部原资源留到收尾DTO。
def _prepare_coordinated_resources(context):
    from ..agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from . import gateway_process as api
    from .gateway_control_plane import setup_control_run
    from .gateway_worker_runtime import CoordinatorWorkers

    control = context.control_plane
    context = replace(context, drain_runtime=GatewayDrainRuntime(control))
    cleanup = GatewayRunCleanupRequest(context=context, pid=os.getpid(), stop_event=threading.Event(),
                                       heartbeat_thread=None, request_thread=None, background_thread=None,
                                       http_server=None, termination_status="failed")
    try:
        requeued, pid = setup_control_run(context)
        recovery = recover_coordinated_startup(context)
        requeued, failed = recovery.summary["requeued"], recovery.summary["failed"]
        port = getattr(context.config, "gateway_port", 0) or 0
        server = api._start_gateway_http(None, context.paths, port, control, context.drain_runtime)
        cleanup = replace(cleanup, http_server=server)
        agent = control.start_business(control.agent_factory)
        if agent is not None:
            agent._gateway_control_plane = control
        context = replace(context, agent=agent)
        cleanup = replace(cleanup, context=context)
        if server is not None:
            server.agent = agent
            port = server.server.server_address[1]
            state_request = GatewayThreadsRequest(context=context, requeued=requeued, failed=failed, http_port=port)
            publish_coordinated_state(context, api._build_run_state(state_request, pid, "starting"))
            workers = CoordinatorWorkers(control, context.config_path.resolve(), port)
            context.drain_runtime.retain_workers(workers)
            cleanup = replace(cleanup, coordinator_workers=workers)
            workers.start()
        stop, heartbeat, request, background, server = api._cmd_gateway_run_threads(
            GatewayThreadsRequest(context=context, requeued=requeued, failed=failed, http_port=port,
                                  http_server=server, stop_event=cleanup.stop_event)
        )
        return replace(cleanup, stop_event=stop, heartbeat_thread=heartbeat, request_thread=request,
                       background_thread=background, http_server=server)
    except BaseException as error:
        _cleanup_starting_gateway(cleanup, error)
        raise


# LLM: 原初始化异常优先；同一原runtime持有已born设施，部分DTO允许None但不能用缺句柄证明排空。清理异常仅记录类型，不覆盖原异常或重放业务。
# 函数用途: 在任何启动步骤失败后停止本次实际资源，并留下明确的未结清理事实。
def _cleanup_starting_gateway(cleanup, original_error):
    from . import gateway_process as api

    facts = {"status": "failed", "pid": cleanup.pid, "error_type": type(original_error).__name__,
             "phase": "startup", "updated_at": time.time()}
    try:
        result = api._cmd_gateway_run_cleanup(replace(cleanup, termination_reason="startup_failed"))
        facts.update(drain_complete=result.get("drain_complete") is True,
                     drain_blockers=result.get("drain_blockers", []))
    except BaseException as cleanup_error:
        facts.update(drain_complete=False, cleanup_error_type=type(cleanup_error).__name__)
    try:
        publish_coordinated_state(cleanup.context, facts)
    except (OSError, RuntimeError, ValueError) as state_error:
        print(json.dumps({"status": "failed", "error_type": type(original_error).__name__,
                          "state_error_type": type(state_error).__name__}, sort_keys=True), file=sys.stderr)


# LLM: 原设施Thread在start前记入同一P6根，ACK异常保原对象；不签线程业务权、不把Future或名字当物理退出。
# 函数用途: 启动正式协调者的原循环线程，初始化失败留下准确未结资源。
def start_coordinated_thread(context, kind, reference, thread):
    from ..agent.gateway_parts.multiprocess_http_transport import original_http_birth

    runtime = context.drain_runtime
    with original_http_birth(runtime, control=context.control_plane, paths=context.paths):
        group = runtime.retain_thread(kind, reference, thread)
        try:
            thread.start()
        except BaseException as error:
            group.retain_fault(error)
            raise


# LLM: 原C后台构造先核实际来源/Root；原HTTP已绑定才允许running。所有原Thread先被同一Root保存再start，初始化失败置停止并保fault，不遗失出生责任。
# 函数用途: 启动本代协调者的三条原循环，复用已经绑定的控制HTTP实例。
def start_coordinated_loops(request):
    from ..agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )
    from . import gateway_process as api

    context = request.context
    coordinated_state_fields(context)
    CoordinatorBackgroundRequests(context.control_plane, drain_runtime=context.drain_runtime)
    stop = request.stop_event if request.stop_event is not None else threading.Event()
    targets = (("control", "heartbeat", api._gateway_heartbeat_loop, (context, stop)),
               ("foreground", "request", api._gateway_request_loop, (context, context.paths, stop)),
               ("background", "background", api._gateway_background_main_loop, (context, stop)))
    threads = {name: threading.Thread(target=target, args=args, daemon=True) for _kind, name, target, args in targets}
    try:
        for kind, name, _target, _args in (targets[1], targets[2], targets[0]):
            start_coordinated_thread(context, kind, f"gateway/{name}-loop", threads[name])
        publish_coordinated_state(context, api._build_run_state(request, os.getpid()))
        if context.agent is not None:
            api.log_gateway_event(context.agent, "gateway_run_running", api._build_run_payload(request, os.getpid()))
        print(f"[gateway-run] status=running pid={os.getpid()} http_port={request.http_port}", flush=True)
    except BaseException:
        stop.set()
        context.drain_runtime.stop_producers()
        raise
    return stop, threads["heartbeat"], threads["request"], threads["background"], request.http_server


# LLM: 沿原服务分类/信号身份/资源DTO；正常v2末态由原shutdown发布，不完整显式drain_complete=False，不重建责任或走旧单进程接班。
# 函数用途: 等待本代服务结束，保存真实终止原因并在原C锁内完成资源收尾。
def _serve_coordinated_gateway(cleanup) -> int:
    from . import gateway_process as api

    context, paths, agent, pid = cleanup.context, cleanup.context.paths, cleanup.context.agent, cleanup.pid
    previous = api._install_gateway_signal_handlers(paths, api._gateway_context_process_identity(context))
    status, reason, exit_code = "failed", "gateway run ended before termination was classified", 2
    try:
        report = api._run_gateway_service_loop(context)
        termination = api._classify_gateway_service_return(context, report)
        status, reason, exit_code = termination.status, termination.reason, termination.exit_code
        api._record_gateway_service_termination(paths, agent, pid, termination, context=context)
    except KeyboardInterrupt:
        status, reason, exit_code = "interrupted", "keyboard interrupt", 130
        publish_coordinated_state(context, {"status": status, "pid": pid, "stopped_at": time.time()})
        if agent is not None:
            api.log_gateway_event(agent, "gateway_run_interrupted", {"status": status, "pid": pid})
    except Exception as error:
        status, exit_code = "failed", 2
        reason = f"{type(error).__name__}: {error}"
        publish_coordinated_state(context, {"status": status, "pid": pid, "error_type": type(error).__name__, "updated_at": time.time()})
        if agent is not None:
            api.log_gateway_event(agent, "gateway_run_failed", {"status": status, "pid": pid, "error_type": type(error).__name__})
    finally:
        try:
            result = api._cmd_gateway_run_cleanup(replace(cleanup, termination_status=status, termination_reason=reason))
        finally:
            api._restore_gateway_signal_handlers(previous)
    if not result.get("drain_complete"):
        payload = {**read_json_file(coordinated_state_path(context)), "status": "failed", "pid": pid,
                   "error_code": "GATEWAY_DRAIN_INCOMPLETE", "termination_kind": "drain_incomplete",
                   "drain_blockers": result.get("drain_blockers", []), "drain_complete": False,
                   "alive_threads": result.get("alive_threads", []), "exit_code": 2,
                   "updated_at": time.time()}
        publish_coordinated_state(context, payload)
        if agent is not None:
            api.log_gateway_event(agent, "gateway_run_failed", payload)
        exit_code = 2
    if result.get("surviving_background_sessions"):
        publish_coordinated_state(context, {**read_json_file(coordinated_state_path(context)),
                                  "surviving_background_sessions": result["surviving_background_sessions"],
                                  "updated_at": time.time()})
    return exit_code
