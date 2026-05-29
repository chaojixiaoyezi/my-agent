# LLM: Background subagent dispatch keeps create_subagents non-blocking.
# 模块用途: 创建子代理后后台启动 runner，并把状态写回任务树供父代理查询。

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import CapabilityRouter
from .agent_tree_status import agent_tree_status_payload
from .dispatch_params import DispatchParams
from .orchestration_create_idempotency import dispatchable_tasks
from .orchestration_dispatch_tool_helpers import _dispatch_capability_config
from .parameters import _bool_param
from .runner_context import current_subagent_run_id


# LLM: _BackgroundDispatchRequest bundles async dispatch inputs so helpers stay small.
# 类用途: 保存后台启动线程所需的 agent、run_ids、router、配置和 dispatch 参数。
@dataclass(frozen=True)
class _BackgroundDispatchRequest:
    agent: object
    run_ids: list[str]
    launch_id: str
    router: object
    cfg: object
    params: DispatchParams


# LLM: auto_start_tasks starts new subagents unless the caller explicitly defers.
# 函数用途: create_subagents 创建任务后立即后台启动可运行 run，并返回状态。
def auto_start_tasks(agent, tasks: list, request_params: dict[str, object]) -> dict[str, object]:
    skipped_run_ids = [_safe_task_id(task) for task in tasks if _safe_task_id(task)]
    dispatchable = dispatchable_tasks(tasks)
    if _bool_param(request_params.get("defer_start"), default=False):
        run_ids = [_safe_task_id(task) for task in dispatchable if _safe_task_id(task)]
        return {"status": "deferred", "run_ids": run_ids, "reason": "defer_start=true"}
    startable = [task for task in dispatchable if not _task_defer_start(task)]
    deferred_run_ids = [_safe_task_id(task) for task in dispatchable if _task_defer_start(task) and _safe_task_id(task)]
    run_ids = [_safe_task_id(task) for task in startable if _safe_task_id(task)]
    if not run_ids:
        status = "deferred" if deferred_run_ids else "not_needed"
        reason = "item.defer_start=true" if deferred_run_ids else ""
        return {
            "status": status,
            "run_ids": [],
            "deferred_run_ids": deferred_run_ids,
            "skipped_run_ids": skipped_run_ids,
            "reason": reason,
        }
    if not callable(getattr(agent, "dispatch_subagents", None)):
        return {"status": "unavailable", "run_ids": run_ids, "reason": "agent has no dispatch_subagents"}
    try:
        result = _start_background_dispatch(agent, run_ids)
        if deferred_run_ids:
            result["deferred_run_ids"] = deferred_run_ids
            result["deferred_reason"] = "item.defer_start=true"
        return result
    except Exception as exc:
        return {"status": "failed", "run_ids": run_ids, "error": f"{type(exc).__name__}: {exc}"}


# LLM: _start_background_dispatch launches runner work without blocking the parent turn.
# 函数用途: 写入 launching 状态、启动独立 dispatch 进程，并返回 run_id/tree/status。
def _start_background_dispatch(agent, run_ids: list[str]) -> dict[str, object]:
    launch_id = f"subagent-start-{time.time_ns()}"
    router, cfg, dispatch_params = _auto_start_dispatch_args(
        agent,
        run_ids,
        background_launch_id=launch_id,
    )
    request = _BackgroundDispatchRequest(agent, run_ids, launch_id, router, cfg, dispatch_params)
    _mark_background_start(request, status="launching")
    if _use_inprocess_autostart(agent):
        return _start_inprocess_dispatch(agent, request)
    process = _spawn_background_dispatch_process(agent, request)
    _remember_background_dispatch(agent, launch_id, run_ids, f"pid:{process.pid}")
    return {
        "status": "started",
        "dispatch_mode": "background",
        "background_backend": "process",
        "run_ids": run_ids,
        "launch_id": launch_id,
        "pid": process.pid,
        "log_path": _background_log_path(agent, launch_id),
        "summary": "subagent dispatch launched in a durable process; parent should inspect agent tree for progress",
        "agent_tree": _safe_agent_tree(agent),
    }


# LLM: _use_inprocess_autostart keeps echo tests lightweight while real backends use a subprocess.
# 函数用途: 判断是否用进程内线程启动子代理；只允许 echo 后端走这条测试快路。
def _use_inprocess_autostart(agent) -> bool:
    return str(getattr(getattr(agent, "config", None), "model_backend", "") or "").strip() == "echo"


# LLM: _start_inprocess_dispatch is the echo-backend test path, not the real model runtime path.
# 函数用途: 在 echo 后端用非 daemon 线程跑后台 dispatch，避免单测额外启动进程。
def _start_inprocess_dispatch(agent, request: _BackgroundDispatchRequest) -> dict[str, object]:
    thread = threading.Thread(
        target=_background_dispatch_worker,
        name=f"my-agent-{request.launch_id}",
        args=(request,),
        daemon=False,
    )
    _remember_background_dispatch(agent, request.launch_id, request.run_ids, thread.name)
    thread.start()
    return {
        "status": "started",
        "dispatch_mode": "background",
        "background_backend": "thread",
        "run_ids": request.run_ids,
        "launch_id": request.launch_id,
        "thread_name": thread.name,
        "summary": "subagent dispatch launched in-process for echo backend; parent should inspect agent tree for progress",
        "agent_tree": _safe_agent_tree(agent),
    }


# LLM: _spawn_background_dispatch_process starts durable runner dispatch for real model backends.
# 函数用途: 启动独立 subagents-dispatch 进程，并把日志写到任务工作区。
def _spawn_background_dispatch_process(agent, request: _BackgroundDispatchRequest) -> subprocess.Popen:
    log_path = Path(_background_log_path(agent, request.launch_id))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    command = _background_dispatch_command(agent, request)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(_project_root()),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log_handle.close()
        return process
    except Exception:
        log_handle.close()
        raise


# LLM: _background_dispatch_command scopes the subprocess to only the run_ids created in this tool call.
# 函数用途: 生成后台 dispatch 命令，带 run_id、launch_id 和无缓冲输出参数。
def _background_dispatch_command(agent, request: _BackgroundDispatchRequest) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "agent_py_agent",
        "--config",
        _config_path(agent),
        "subagents-dispatch",
        "--apply",
        "--execute-runners",
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


# LLM: _config_path preserves the foreground agent config for the background dispatch subprocess.
# 函数用途: 找到当前 agent 配置路径；没有显式路径时回退仓库默认配置。
def _config_path(agent) -> str:
    config = getattr(agent, "config", None)
    configured = str(getattr(config, "config_path", "") or "").strip()
    if configured:
        return configured
    return str(_project_root() / "agent_py_agent" / "config" / "agent_config.yaml")


# LLM: _project_root anchors subprocess cwd and default config lookup.
# 函数用途: 返回仓库根目录，供后台进程和日志路径使用。
def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


# LLM: _background_log_path keeps auto-start logs near the subagent workspace.
# 函数用途: 计算后台启动日志路径，方便父代理或人工排查启动状态。
def _background_log_path(agent, launch_id: str) -> str:
    workspace = Path(getattr(getattr(agent, "subagents", None), "workspace", _project_root() / "data" / "subagents"))
    return str(workspace / "background_dispatch" / f"{launch_id}.log")


# LLM: _background_dispatch_worker is the only async runner side effect.
# 函数用途: 在后台调用 dispatch_subagents，并把成功/失败写回启动状态。
def _background_dispatch_worker(request: _BackgroundDispatchRequest) -> None:
    try:
        _mark_background_start(request, status="running")
        report = request.agent.dispatch_subagents(request.router, request.cfg, params=request.params)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _mark_background_start(request, status="failed", error=error)
        _remember_background_result(request.agent, request.launch_id, {"ok": False, "error": error})
        return
    _mark_background_start(request, status="finished")
    _remember_background_result(request.agent, request.launch_id, _auto_start_report(request.run_ids, report))


# LLM: _mark_background_start records launch progress on each task node.
# 函数用途: 把 launching/running/finished/failed 写入 task.attributes.background_start。
def _mark_background_start(request: _BackgroundDispatchRequest, *, status: str, error: str = "") -> None:
    now = time.time()
    manager = getattr(request.agent, "subagents", None)
    for run_id in request.run_ids:
        try:
            task = manager.load(run_id)
        except Exception:
            continue
        if getattr(task, "id", "") != run_id:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = {
            "launch_id": request.launch_id,
            "status": status,
            "updated_at": now,
            "error": error,
        }
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception:
            continue


# LLM: _remember_background_dispatch keeps an in-memory index for the foreground parent.
# 函数用途: 记录本轮启动的后台线程，便于父代理或测试读取。
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


# LLM: _remember_background_result stores the final lightweight dispatch outcome.
# 函数用途: 更新后台启动登记项的 finished/failed 状态和摘要结果。
def _remember_background_result(agent, launch_id: str, result: dict[str, object]) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return
    item = dict(registry.get(launch_id) or {})
    status = "finished" if result.get("ok", True) else "failed"
    item.update({"status": status, "result": result, "finished_at": time.time()})
    registry[launch_id] = item


# LLM: _safe_agent_tree gives create_subagents a best-effort status snapshot.
# 函数用途: 读取任务树失败时返回结构化 warning，而不是阻断创建。
def _safe_agent_tree(agent) -> dict[str, object]:
    try:
        return agent_tree_status_payload(agent, {"scope": "root_tree"})
    except Exception as exc:
        return {"schema_version": "agent_tree_status.v1", "warnings": [f"agent_tree_unavailable:{type(exc).__name__}"]}


# LLM: _auto_start_dispatch_args builds the existing dispatch_subagents call.
# 函数用途: 复用原调度器参数，只把调用放到后台执行。
def _auto_start_dispatch_args(
    agent,
    run_ids: list[str],
    *,
    background_launch_id: str = "",
) -> tuple[object, object, DispatchParams]:
    cfg = _dispatch_capability_config(agent)
    tool_specs = [spec for spec in agent.tools.specs() if getattr(spec, "category", "") != "orchestration"]
    router = CapabilityRouter(config=cfg, tool_specs=tool_specs)
    params = DispatchParams(
        apply=True,
        execute_runners=True,
        workflow_mode="off",
        max_runners=len(run_ids),
        limit=max(20, len(run_ids)),
        reviewer="create-subagents-auto-start",
        note="auto-start after create_subagents",
        parent_run_id=current_subagent_run_id(agent),
        include_run_ids=run_ids,
        background_launch_id=background_launch_id,
    )
    return router, cfg, params


# LLM: _auto_start_report normalizes dispatch reports from real agents and test doubles.
# 函数用途: 把后台 dispatch 结果压成小型 JSON 字段。
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


# LLM: _safe_task_id extracts persisted ids without leaking MagicMock objects.
# 函数用途: 从任务对象读取字符串 id，非法值返回空字符串。
def _safe_task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _task_defer_start reads the model-requested defer_start flag from persisted task attributes.
# 函数用途: 判断单个子代理是否只建不跑；坏 attributes 形态按不延迟处理。
def _task_defer_start(task: object) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    return _bool_param(attrs.get("defer_start"), default=False)


__all__ = ["auto_start_tasks"]
