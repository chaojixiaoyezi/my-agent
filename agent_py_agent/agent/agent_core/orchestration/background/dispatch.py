
from __future__ import annotations

# LLM: 后台派工只传任务身份与显式注入；生产模型由 child thread 引用恢复，不能捕获共享宿主的当前连接。
# 模块用途: 将子代理交给持久进程或隔离线程执行，保存启动回执、失败与结果，不另设模型路由。
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ....concurrency.interrupt import register_interruptible
from ....runtime_context import current_subagent_run_id
from ....runtime_errors import runtime_error_report
from ....subagents.models import FailureType
from ...parameters import _bool_param
from ..create_constraints import dispatchable_tasks
from ..dispatch.conversation_lifecycle_gate import conversation_lifecycle_decisions
from ..dispatch.params import DispatchExecutionPlan, DispatchParams
from ..dispatch.tool_helpers import _dispatch_capability_config


@dataclass(frozen=True)
class _BackgroundDispatchRequest:
    agent: object
    run_ids: list[str]
    launch_id: str
    router: object
    cfg: object
    params: DispatchParams
    backend_override: object | None = None


@dataclass(frozen=True)
class _ProcessStartupFailureRequest:
    agent: object
    dispatch: _BackgroundDispatchRequest
    process: subprocess.Popen
    returncode: int
    mark_errors: list[dict[str, object]]


# LLM: expected_attempt_ids 只接受宿主关键字，不能从模型 request_params 读取；进程启动在 creation 锁外。
# 函数用途: 自动启动可执行孩子，并沿用控制入口已经预留的准确执行轮。
def auto_start_tasks(
    agent, tasks: list, request_params: dict[str, object], *,
    expected_attempt_ids: dict[str, str] | None = None,
) -> dict[str, object]:
    skipped_run_ids = [_safe_task_id(task) for task in tasks if _safe_task_id(task)]
    dispatchable = dispatchable_tasks(tasks)
    if _bool_param(request_params.get("defer_start"), default=False):
        run_ids = [_safe_task_id(task) for task in dispatchable if _safe_task_id(task)]
        return {"status": "deferred", "run_ids": run_ids, "reason": "defer_start=true"}
    startable = [task for task in dispatchable if not _task_defer_start(task)]
    deferred_run_ids = [_safe_task_id(task) for task in dispatchable if _task_defer_start(task) and _safe_task_id(task)]
    decisions = conversation_lifecycle_decisions(agent, startable)
    held = [
        decisions[_safe_task_id(task)].payload()
        for task in startable
        if not decisions[_safe_task_id(task)].allowed
    ]
    startable = [task for task in startable if decisions[_safe_task_id(task)].allowed]
    run_ids = [_safe_task_id(task) for task in startable if _safe_task_id(task)]
    if not run_ids:
        status = "deferred" if deferred_run_ids else "blocked" if held else "not_needed"
        reason = "item.defer_start=true" if deferred_run_ids else "conversation_lifecycle_gate" if held else ""
        return {
            "status": status,
            "run_ids": [],
            "deferred_run_ids": deferred_run_ids,
            "skipped_run_ids": skipped_run_ids,
            "reason": reason,
            "conversation_gate": held,
        }
    if not callable(getattr(agent, "dispatch_subagents", None)):
        return {"status": "unavailable", "run_ids": run_ids, "reason": "agent has no dispatch_subagents"}
    # 结构化前置闸:subagents.workspace 必须是真实路径类型(str/Path)才允许后台派工。
    # 否则(测试里的 mock agent、坏对象)派工线程会把启动日志按对象的 fspath 落成相对路径,
    # 写进当前工作目录任意位置(实锤:MagicMock.__fspath__ 默认值把 repo 里写出
    # MagicMock/mock.subagents.workspace/... 目录树),还白起真线程。
    workspace = getattr(getattr(agent, "subagents", None), "workspace", None)
    if workspace is not None and not isinstance(workspace, (str, Path)):
        return {"status": "unavailable", "run_ids": run_ids, "reason": "subagents workspace is not a real path"}
    try:
        result = _start_background_dispatch(agent, run_ids, expected_attempt_ids=expected_attempt_ids)
        if held:
            result["conversation_gate"] = held
        if deferred_run_ids:
            result["deferred_run_ids"] = deferred_run_ids
            result["deferred_reason"] = "item.defer_start=true"
        return result
    except Exception as exc:
        return {"status": "failed", "run_ids": run_ids, "error": f"{type(exc).__name__}: {exc}"}


# LLM: 先在原创建锁固定 pending 和 launch，再释放锁启动线程/进程；任一接纳错误都不能交给执行器。
# 函数用途: 把本次后台启动准确绑定到一批已预留轮次，停止可撤销这些原轮次。
def _start_background_dispatch(
    agent, run_ids: list[str], *, expected_attempt_ids: dict[str, str] | None = None,
) -> dict[str, object]:
    from ....subagents.runner_start import existing_runner_launch, reserve_runner_start

    if expected_attempt_ids is not None and set(expected_attempt_ids) != set(run_ids):
        raise ValueError("启动身份必须与本批运行范围逐项对应")
    launch_id = f"subagent-start-{time.time_ns()}"
    router, cfg, dispatch_params = _auto_start_dispatch_args(
        agent,
        run_ids,
        background_launch_id=launch_id,
    )
    request = _BackgroundDispatchRequest(
        agent,
        run_ids,
        launch_id,
        router,
        cfg,
        dispatch_params,
        _captured_backend_override(agent),
    )
    with agent.subagents.creation_guard():
        reused = [run_id for run_id in run_ids if existing_runner_launch(
            agent.subagents, run_id, expected_attempt_ids[run_id] if expected_attempt_ids is not None else None,
        )]
        run_ids = [run_id for run_id in run_ids if run_id not in reused]
        if not run_ids:
            return {"status": "started", "run_ids": reused, "reused_run_ids": reused, "acceptance_status": "accepted"}
        request = replace(request, run_ids=run_ids)
        dispatch_params.include_run_ids = list(run_ids)
        dispatch_params.expected_attempt_ids = {
            run_id: reserve_runner_start(
                agent.subagents, run_id,
                expected_attempt_id=expected_attempt_ids[run_id] if expected_attempt_ids is not None else None,
                launch_id=launch_id,
            )
            for run_id in run_ids
        }
        mark_errors = mark_background_start(request, status="launching")
    if mark_errors:
        mark_errors.extend(mark_background_start(request, status="failed", error="启动批次未全部接纳"))
        return _with_reused_starts({"status": "failed", "run_ids": run_ids, "background_mark_errors": mark_errors}, reused)
    if _use_inprocess_autostart(agent):
        return _with_reused_starts(_start_inprocess_dispatch(agent, request, mark_errors), reused)
    process = _spawn_background_dispatch_process(agent, request)
    # Persist the PID before giving the child enough startup time to enter the
    # runner and contend for the same canonical task guard.  The previous
    # poll-first order gave the child a 250 ms head start; on a busy task tree
    # the parent then waited behind repeated runner saves for tens of seconds.
    # An immediate exit is still detected below and is overwritten with the
    # typed startup-failure state.
    mark_errors.extend(mark_background_start(request, status="running", pid=process.pid))
    if (returncode := process_startup_returncode(process)) is not None:
        return _with_reused_starts(_background_process_startup_failure(
            _ProcessStartupFailureRequest(agent, request, process, returncode, mark_errors)
        ), reused)
    # 孤儿回收前提:pid 必须落盘到任务权威记录(R6a 实锤:此前 pid 只进内存
    # registry,主代理退出后无人能定位后台进程;cancel_subagents 的
    # background_start.pid 终止路径因此永远 no_pid)。
    _remember_background_dispatch(agent, launch_id, run_ids, f"pid:{process.pid}")
    _start_background_process_reaper(agent, request, process)
    payload = _background_process_started_payload(agent, request, process)
    attach_mark_errors(payload, mark_errors)
    return _with_reused_starts(payload, reused)


# LLM: 混合批次分别报告本次新启动与原接纳；失败不能把仍有效的旧启动说成已取消。
# 函数用途: 合并重复投递的启动回执，不修改任何运行状态。
def _with_reused_starts(payload: dict[str, object], reused: list[str]) -> dict[str, object]:
    if reused:
        current = list(payload.get("run_ids") or [])
        payload["run_ids"] = [*reused, *current]
        payload["reused_run_ids"] = list(reused)
        payload["started_run_ids"] = [*reused, *current] if payload.get("status") == "started" else list(reused)
    return payload


def _background_process_startup_failure(request: _ProcessStartupFailureRequest) -> dict[str, object]:
    error = f"background dispatch process exited during startup returncode={request.returncode}"
    dispatch = request.dispatch
    request.mark_errors.extend(mark_background_start(dispatch, status="failed", error=error))
    request.mark_errors.extend(mark_background_channel_failure(dispatch, error=error))
    payload = {
        "status": "failed",
        "dispatch_mode": "background",
        "background_backend": "process",
        "failure_type": FailureType.BACKGROUND_DISPATCH_STARTUP.value,
        "run_ids": dispatch.run_ids,
        "launch_id": dispatch.launch_id,
        "pid": request.process.pid,
        "returncode": request.returncode,
        "log_path": _background_log_path(request.agent, dispatch.launch_id),
        "summary": "subagent dispatch process exited before runner startup; affected tasks were marked channel failed",
    }
    attach_mark_errors(payload, request.mark_errors)
    return payload


# LLM: Startup receipts notify the direct parent through lifecycle events and
# must not advertise an agent-tree polling surface.
# 函数用途: 生成持久子进程已接收本批 child 的结构化启动回执。
def _background_process_started_payload(
    agent,
    request: _BackgroundDispatchRequest,
    process: subprocess.Popen,
) -> dict[str, object]:
    return {
        "status": "started",
        "dispatch_mode": "background",
        "background_backend": "process",
        "run_ids": request.run_ids,
        "launch_id": request.launch_id,
        "pid": process.pid,
        "log_path": _background_log_path(agent, request.launch_id),
        "acceptance_status": "accepted",
        "summary": "subagent dispatch accepted by a durable process; lifecycle changes will notify the direct parent",
    }


def _use_inprocess_autostart(agent) -> bool:
    config = getattr(agent, "config", None)
    if str(getattr(config, "model_backend", "") or "").strip() == "echo":
        return True
    # 隔离 owner（飞书以及本机 local/user thin TUI）真机实锤：auto-start 子进程命令只带
    # 基础 --config/--workspace-root，不带本次请求冻结的 owner 身份；子进程会重新解析成
    # local/main，跑错 owner home、找不到这批 run_id，子代理因此永久卡在“排队中”。
    # 不能只按 provider 判断，因为 local 同时包含 local/main 与 local/user。这里复用唯一
    # OwnerIdentity 解析，所有非 local/main owner 都改走进程内线程派工：用进程内已经正确
    # scoped 的 agent（owner 不跨进程边界），
    # 线程在常驻 Gateway 进程里自然活得过单条请求。线程必须是 daemon：任务、attempt 和
    # 租约都已持久化并有旧进程 fencing，Gateway 重启后由统一恢复接管；反而让一个卡住的
    # provider 调用作为 non-daemon 阻塞进程退出，会让服务无法完成安全重启。只有精确
    # local/main 仍走 durable 子进程，行为不变。
    raw_provider = str(getattr(config, "my_agent_owner_provider", "") or "").strip()
    if not raw_provider:
        return False

    from ....user_space.owner_resolver import owner_identity_from_config

    owner = owner_identity_from_config(config)
    return not (owner.provider == "local" and owner.owner_kind == "main")


# LLM: 仅保留宿主显式依赖注入；普通模型连接必须由 worker 按 canonical child 配置构造，不继承调度线程的默认连接。
# 函数用途: 捕获测试或嵌入宿主明确提供的替身，防止恢复时未配置的 Gateway 后端覆盖已选子代理模型。
def _captured_backend_override(agent) -> object | None:
    return getattr(agent, "_subagent_worker_backend_override", None)


# LLM: In-process startup uses the same event-driven receipt as subprocess
# startup; thread creation must not restore model polling instructions.
# 函数用途: 在线程内启动本批 runner，并返回等待生命周期事件的回执。
def _start_inprocess_dispatch(
    agent,
    request: _BackgroundDispatchRequest,
    mark_errors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    thread = threading.Thread(
        target=_background_dispatch_worker,
        name=f"my-agent-{request.launch_id}",
        args=(request,),
        daemon=True,
    )
    _remember_background_dispatch(agent, request.launch_id, request.run_ids, thread.name)
    thread.start()
    payload = {
        "status": "started",
        "acceptance_status": "accepted",
        "dispatch_mode": "background",
        "background_backend": "thread",
        "run_ids": request.run_ids,
        "launch_id": request.launch_id,
        "thread_name": thread.name,
        "summary": "subagent dispatch accepted in-process; lifecycle changes will notify the direct parent",
    }
    attach_mark_errors(payload, mark_errors or [])
    return payload


def _spawn_background_dispatch_process(agent, request: _BackgroundDispatchRequest) -> subprocess.Popen:
    log_path = Path(_background_log_path(agent, request.launch_id))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    command = _background_dispatch_command(agent, request)
    # 标记后台派工子进程:子进程里记录的 runner 会话据此标 in_process=False,宿主已死回收对它
    # 保留原 pid-liveness(不被网关重启的 epoch 换代判据误杀——它是独立进程,不随网关死)。
    from ....subagents.process_control import RUNNER_SUBPROCESS_ENV

    child_env = {**os.environ, RUNNER_SUBPROCESS_ENV: "1"}
    try:
        process = subprocess.Popen(
            command,
            cwd=str(_project_root()),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
        log_handle.close()
        return process
    except Exception:
        log_handle.close()
        raise


def process_startup_returncode(process: subprocess.Popen) -> int | None:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return None
    for index in range(6):
        returncode = poll()
        if returncode is not None:
            return int(returncode)
        if index < 5:
            time.sleep(0.05)
    return None


def _start_background_process_reaper(
    agent,
    request: _BackgroundDispatchRequest,
    process: subprocess.Popen,
) -> None:
    """Retain and reap a successfully started dispatch child.

    ``Popen`` must remain owned until ``wait()`` completes.  Dropping the only
    handle leaves a signalled child as a zombie until another subprocess spawn
    happens to run Python's lazy cleanup.  Audit workers start and stop for a
    long time, so cleanup cannot depend on a future spawn.
    """
    if not callable(getattr(process, "wait", None)):
        return
    thread = threading.Thread(
        target=_reap_background_process,
        name=f"my-agent-reap-{request.launch_id}",
        args=(agent, request.launch_id, process),
        daemon=True,
    )
    thread.start()


def _reap_background_process(agent, launch_id: str, process: subprocess.Popen) -> None:
    try:
        returncode = int(process.wait())
        error = ""
    except Exception as exc:
        returncode = None
        error = f"{type(exc).__name__}: {exc}"
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return
    item = dict(registry.get(launch_id) or {})
    item.update(
        {
            "status": "finished" if returncode == 0 else "failed",
            "returncode": returncode,
            "finished_at": time.time(),
        }
    )
    if error:
        item["reap_error"] = error
    registry[launch_id] = item


# LLM: 通道失败也须按原 launch/attempt 条件提交，不能用失败回执覆盖新轮任务状态。
# 函数用途: 仅标记本次仍有效的启动失败，并保留每项未确认原因。
def mark_background_channel_failure(request: _BackgroundDispatchRequest, *, error: str) -> list[dict[str, object]]:
    from ....subagents.process_control import BackgroundStartUpdate

    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    for run_id in request.run_ids:
        try:
            manager.lifecycle.update_background_start(run_id, BackgroundStartUpdate(
                launch_id=request.launch_id, status="failed", error=error,
                attempt_id=request.params.expected_attempt_ids[run_id],
            ), channel_failure=True)
        except Exception as exc:
            mark_errors.append(
                {"run_id": str(run_id), **runtime_error_report(exc, context="background_dispatch.channel_failure.save")}
            )
    return mark_errors


# LLM: background_start 是每个被派任务上的后台启动权威记录(任务 attributes 内,
#   随 manager.save 落盘)。记录构造统一走 process_control.
#   build_background_start_record(R7a 实锤:CLI 侧 mark_background_launch 曾手写
#   dict 抹掉 pid,孤儿回收进程层失效)——pid 是孤儿回收与 cancel_subagents 终止
#   路径的定位事实,任何状态更新不得抹掉。注意:subprocess 路径的 dispatch CLI
#   进程会经 cli/dispatch_background 更新此状态(running/finished/failed),
#   消费方判断进程死活仍只能验 pid 活性,不能信 status 字段。
# 函数用途: 把"这批任务的后台派工进行到哪一步了"写进每个任务的属性里,失败的
#   写不进去的逐个记错误返回,不打断其他任务。
def mark_background_start(
    request: _BackgroundDispatchRequest,
    *,
    status: str,
    error: str = "",
    pid: int = 0,
) -> list[dict[str, object]]:
    from ....subagents.process_control import BackgroundStartUpdate

    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    for run_id in list(getattr(request, "run_ids", []) or []):
        try:
            manager.lifecycle.update_background_start(run_id, BackgroundStartUpdate(
                launch_id=request.launch_id, status=status, error=error, pid=pid,
                replace_launch=status == "launching",
                attempt_id=request.params.expected_attempt_ids[run_id],
            ))
        except Exception as exc:
            mark_errors.append(_background_mark_error(str(run_id), exc, "background_dispatch.mark_start.update"))
    return mark_errors


def attach_mark_errors(payload: dict[str, object], errors: list[dict[str, object]]) -> None:
    if errors:
        payload["background_mark_errors"] = errors


def _background_mark_error(run_id: str, exc: BaseException, context: str) -> dict[str, object]:
    return {"run_id": run_id, **runtime_error_report(exc, context=context)}


# LLM: argv 数组显式运输宿主 pending 身份；不拼 shell、不借自然语言备注传权限。
# 函数用途: 让独立派工进程消费与进程内执行相同的一次启动接纳。
def _background_dispatch_command(agent, request: _BackgroundDispatchRequest) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "agent_py_agent",
        "--config",
        _config_path(agent),
        "subagents-dispatch",
        "--workspace-root",
        _workspace_root_arg(agent),
        "--apply",
        "--start-runners",
        "--max-runners",
        str(max(1, len(request.run_ids))),
        "--limit",
        str(max(20, len(request.run_ids))),
        "--reviewer",
        "create-subagents-auto-start",
        "--note",
        f"auto-start after create_subagents launch_id={request.launch_id}",
        "--background-launch-id",
        request.launch_id,
    ]
    for run_id in request.run_ids:
        command.extend(["--run-id", run_id])
        command.extend(["--expected-attempt", run_id, request.params.expected_attempt_ids[run_id]])
    return command


def _workspace_root_arg(agent) -> str:
    root = getattr(agent, "root", None)
    if isinstance(root, (str, Path)) and str(root).strip():
        return str(Path(root).expanduser().resolve())
    manager = getattr(agent, "subagents", None)
    workspace_root = getattr(manager, "workspace_root", None)
    if isinstance(workspace_root, (str, Path)) and str(workspace_root).strip():
        return str(Path(workspace_root).expanduser().resolve())
    return str(_project_root())


def _config_path(agent) -> str:
    config = getattr(agent, "config", None)
    configured = str(getattr(config, "config_path", "") or "").strip()
    if configured:
        return configured
    return str(_project_root() / "agent_py_agent" / "config" / "agent_config.yaml")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _background_log_path(agent, launch_id: str) -> str:
    workspace = Path(getattr(getattr(agent, "subagents", None), "workspace", _project_root() / "data" / "subagents"))
    return str(workspace / "background_dispatch" / f"{launch_id}.log")


def _background_dispatch_worker(request: _BackgroundDispatchRequest) -> None:
    # 协作中断(批3):以线程名登记,cancel_subagents 可按名递中断旗;
    # finally 自动清旗,线程复用不带脏状态。
    with register_interruptible(f"my-agent-{request.launch_id}"):
        _background_dispatch_worker_inner(request)


# LLM: 运行标记失败时不得派工；显式后端注入在 finally 恢复，收尾只修改原启动记录。
# 函数用途: 执行已接纳的后台派工并保存该次真实结果。
def _background_dispatch_worker_inner(request: _BackgroundDispatchRequest) -> None:
    previous_backend_override = getattr(request.agent, "_subagent_worker_backend_override", None)
    previous_present = hasattr(request.agent, "_subagent_worker_backend_override")
    if request.backend_override is not None:
        request.agent._subagent_worker_backend_override = request.backend_override
    try:
        if mark_background_start(request, status="running"):
            raise RuntimeError("后台启动身份未确认，未进入派工")
        report = request.agent.dispatch_subagents(request.router, request.cfg, params=request.params)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        mark_background_start(request, status="failed", error=error)
        _remember_background_result(request.agent, request.launch_id, {"ok": False, "error": error})
        return
    finally:
        _restore_background_backend_override(request, previous_present, previous_backend_override)
    mark_background_start(request, status="finished")
    _remember_background_result(request.agent, request.launch_id, _auto_start_report(request.run_ids, report))


def _restore_background_backend_override(
    request: _BackgroundDispatchRequest,
    previous_present: bool,
    previous_backend_override: object,
) -> None:
    if request.backend_override is None:
        return
    if previous_present:
        request.agent._subagent_worker_backend_override = previous_backend_override
        return
    try:
        delattr(request.agent, "_subagent_worker_backend_override")
    except AttributeError:
        return


def _remember_background_dispatch(agent, launch_id: str, run_ids: list[str], thread_name: str) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        registry = {}
        agent._background_subagent_dispatches = registry
    registry[launch_id] = {
        "run_ids": list(run_ids),
        "thread_name": thread_name,
        "status": "running",
        "started_at": time.time(),
    }


def _remember_background_result(agent, launch_id: str, result: dict[str, object]) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return
    item = dict(registry.get(launch_id) or {})
    status = "finished" if result.get("ok") is True else "failed"
    item.update({"status": status, "result": result, "finished_at": time.time()})
    registry[launch_id] = item


def _auto_start_dispatch_args(
    agent,
    run_ids: list[str],
    *,
    background_launch_id: str = "",
) -> tuple[object, object, DispatchParams]:
    cfg = _dispatch_capability_config(agent)
    router = getattr(agent, "capability_router", None)
    if router is None:
        raise RuntimeError("agent capability router is unavailable")
    params = DispatchParams(
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=True,
            start_runners=True,
            max_runners=len(run_ids),
        ),
        limit=max(20, len(run_ids)),
        reviewer="create-subagents-auto-start",
        note="auto-start after create_subagents",
        parent_run_id=current_subagent_run_id(agent),
        include_run_ids=run_ids,
        background_launch_id=background_launch_id,
    )
    return router, cfg, params


def _auto_start_report(run_ids: list[str], report: object) -> dict[str, object]:
    summary = getattr(report, "summary", "")
    records = getattr(report, "records", [])
    if not isinstance(summary, str) or not isinstance(records, list):
        return {"status": "started", "run_ids": run_ids, "summary": "dispatch started"}
    return {
        "status": "started",
        "run_ids": run_ids,
        "summary": summary,
        "record_count": len(records),
        "dry_run": bool(getattr(report, "dry_run", False)),
    }


def _safe_task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""


def _task_defer_start(task: object) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    return _bool_param(attrs.get("defer_start"), default=False)


__all__ = ["auto_start_tasks"]
