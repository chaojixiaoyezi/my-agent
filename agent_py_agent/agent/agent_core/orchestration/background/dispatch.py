
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ....concurrency.interrupt import register_interruptible
from ....runtime_errors import runtime_error_report
from ....subagents.models import FailureType
from ...parameters import _bool_param
from ...runner.context import current_subagent_run_id
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


def auto_start_tasks(agent, tasks: list, request_params: dict[str, object]) -> dict[str, object]:
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
        result = _start_background_dispatch(agent, run_ids)
        if held:
            result["conversation_gate"] = held
        if deferred_run_ids:
            result["deferred_run_ids"] = deferred_run_ids
            result["deferred_reason"] = "item.defer_start=true"
        return result
    except Exception as exc:
        return {"status": "failed", "run_ids": run_ids, "error": f"{type(exc).__name__}: {exc}"}


def _start_background_dispatch(agent, run_ids: list[str]) -> dict[str, object]:
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
    mark_errors = mark_background_start(request, status="launching")
    if _use_inprocess_autostart(agent):
        return _start_inprocess_dispatch(agent, request, mark_errors)
    process = _spawn_background_dispatch_process(agent, request)
    # Persist the PID before giving the child enough startup time to enter the
    # runner and contend for the same canonical task guard.  The previous
    # poll-first order gave the child a 250 ms head start; on a busy task tree
    # the parent then waited behind repeated runner saves for tens of seconds.
    # An immediate exit is still detected below and is overwritten with the
    # typed startup-failure state.
    mark_errors.extend(mark_background_start(request, status="running", pid=process.pid))
    if (returncode := process_startup_returncode(process)) is not None:
        return _background_process_startup_failure(
            _ProcessStartupFailureRequest(agent, request, process, returncode, mark_errors)
        )
    # 孤儿回收前提:pid 必须落盘到任务权威记录(R6a 实锤:此前 pid 只进内存
    # registry,主代理退出后无人能定位后台进程;cancel_subagents 的
    # background_start.pid 终止路径因此永远 no_pid)。
    _remember_background_dispatch(agent, launch_id, run_ids, f"pid:{process.pid}")
    _start_background_process_reaper(agent, request, process)
    payload = _background_process_started_payload(agent, request, process)
    attach_mark_errors(payload, mark_errors)
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
    # 隔离 owner(飞书等 per-用户 scoped owner)真飞书多用户实锤:auto-start 子进程命令只带
    # --config/--workspace-root、不带 owner 身份,子进程重新解析 config 丢成默认 base owner
    # (local/main)→ 跑错 owner home、找不到这批 run_id → 不派 runner → 子代理永卡"创建工单"。
    # 这类 owner 改走进程内线程派工:用进程内已是正确 owner 的 scoped agent(owner 不跨进程边界),
    # 线程在常驻 Gateway 进程里自然活得过单条请求。线程必须是 daemon：任务、attempt 和
    # 租约都已持久化并有旧进程 fencing，Gateway 重启后由统一恢复接管；反而让一个卡住的
    # provider 调用作为 non-daemon 阻塞进程退出，会让服务无法完成安全重启。base owner
    # (provider 空/local)仍走 durable 子进程,行为不变。
    provider = str(getattr(config, "my_agent_owner_provider", "") or "").strip().lower()
    return bool(provider) and provider != "local"


def _captured_backend_override(agent) -> object | None:
    explicit = getattr(agent, "_subagent_worker_backend_override", None)
    if explicit is not None:
        return explicit
    if _use_inprocess_autostart(agent):
        return getattr(agent, "backend", None)
    return None


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


def mark_background_channel_failure(request: _BackgroundDispatchRequest, *, error: str) -> list[dict[str, object]]:
    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    for run_id in request.run_ids:
        try:
            task = manager.load(run_id)
            task.status = "CHANNEL_ERROR"
            task.channel_status = "BROKEN"
            task.failure_type = FailureType.BACKGROUND_DISPATCH_STARTUP.value
            task.result = error
            task.updated_at = time.time()
            manager.save(task)
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
    from ....subagents.process_control import BackgroundStartUpdate, build_background_start_record

    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    update = BackgroundStartUpdate(
        launch_id=str(getattr(request, "launch_id", "") or ""),
        status=status,
        error=error,
        pid=pid,
        replace_launch=status == "launching",
    )
    for run_id in list(getattr(request, "run_ids", []) or []):
        try:
            task = manager.load(run_id)
        except Exception as exc:
            mark_errors.append(_background_mark_error(str(run_id), exc, "background_dispatch.mark_start.load"))
            continue
        if getattr(task, "id", "") != run_id:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = build_background_start_record(attrs.get("background_start"), update)
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception as exc:
            mark_errors.append(_background_mark_error(str(run_id), exc, "background_dispatch.mark_start.save"))
    return mark_errors


def attach_mark_errors(payload: dict[str, object], errors: list[dict[str, object]]) -> None:
    if errors:
        payload["background_mark_errors"] = errors


def _background_mark_error(run_id: str, exc: BaseException, context: str) -> dict[str, object]:
    return {"run_id": run_id, **runtime_error_report(exc, context=context)}


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


def _background_dispatch_worker_inner(request: _BackgroundDispatchRequest) -> None:
    previous_backend_override = getattr(request.agent, "_subagent_worker_backend_override", None)
    previous_present = hasattr(request.agent, "_subagent_worker_backend_override")
    if request.backend_override is not None:
        request.agent._subagent_worker_backend_override = request.backend_override
    try:
        mark_background_start(request, status="running")
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
