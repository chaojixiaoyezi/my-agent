# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""execution helpers keep one claimed gateway request inside focused contexts."""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent_core.dispatch_params import WatchParams
from ..agent_core.models import AgentRunResult
from ..agent_core.runtime_mixin import RunParams
from ..agent_core.subagent_dispatch_closeout_resolution import (
    blocking_task_ids as closeout_blocking_task_ids,
)
from ..agent_core.subagent_dispatch_closeout_resolution import (
    task_resolved_for_closeout,
)
from ..capabilities import CapabilityRouter
from ..capability.runtime_config import (
    default_capability_config_path,
    load_capability_config_snapshot,
)
from ..capability_config import CapabilityConfig
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .background_projection import gateway_background_request_snapshots
from .chunk_service import close_chunk_stream, open_chunk_stream, write_chunk
from .io import gateway_response_path, read_json_file
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import gateway_chunk_path, gateway_paths
from .recovery import _gateway_request_attempts

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"
_BACKGROUND_RECOVERY_DEFAULT_CONFIG_CYCLES = 20
_BACKGROUND_RECOVERY_DEFAULT_MAX_CYCLES = 60


# LLM: _GatewayResponseBaseContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关响应基础上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayResponseBaseContext:

    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


# LLM: _GatewayAskRunContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关askrun上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayAskRunContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object


# LLM: _GatewayLeaseStartContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关租约start上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayLeaseStartContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


# LLM: _build_gateway_response_base 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建网关响应基础所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_gateway_response_base(context: _GatewayResponseBaseContext) -> dict:
    request = context.request
    return {
        "id": context.request_id,
        "kind": context.kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(context.request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


# LLM: _update_response_from_result 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新来自响应结果对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新请求队列、租约文件、进程状态和响应渲染，需避免破坏既有状态机约定。
def _update_response_from_result(response: dict, result, request: dict) -> None:
    response.update(
        {
            "ok": True,
            "status": "done",
            "response": result.response,
            "backend": result.backend,
            "used_memories": result.used_memories,
            "tool_rounds": result.tool_rounds,
            "prompt": result.prompt if request.get("include_prompt") else "",
            "prompt_token_estimate": result.prompt_token_estimate,
            "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
            "recovery_snapshot_id": result.recovery_snapshot_id,
            "recovery_snapshot_path": result.recovery_snapshot_path,
            "recovery_snapshot_error": result.recovery_snapshot_error,
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
        }
    )


# LLM: _start_gateway_request_lease 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求租约的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = context.refresh_lease or str(context.request.get("status") or "") == "processing"
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    refresh_processing_lease(context.request_path, request_id=context.request_id, worker_id=lease_worker)
    return start_lease_heartbeat(context.agent, context.request_path, request_id=context.request_id, worker_id=lease_worker)


# LLM: _run_gateway_ask 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关ask的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _run_gateway_ask(context: _GatewayAskRunContext):
    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    if str(request.get("context_scope") or "").strip() == "control_plane":
        return _run_gateway_control_plane_ask(context.agent, prompt)
    return context.agent.run(
        prompt,
        params=RunParams(
            inject=[str(item) for item in request.get("inject", [])],
            prompt_files=[str(item) for item in request.get("prompt_files", [])],
            save=bool(request.get("save", True)),
            request_id=context.request_id,
            source="gateway",
            recovery_snapshot=bool(request.get("save", True)),
            resume_context=request.get("resume_context") if "resume_context" in request else None,
            task_attributes=_gateway_task_attributes(request),
            recovery_next_actions=[
                "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
            ],
            recovery_content_paths=[str(context.request_path), str(context.response_path)],
            on_chunk=context.on_chunk,
            context_scope=str(request.get("context_scope") or "default"),
            background_intake=not bool(request.get("client_wait", True)),
            model_request_timeout_seconds=_gateway_model_request_timeout_seconds(
                context.agent,
                request,
            ),
        ),
    )


# LLM: background gateway asks use a machine flag to get a longer model budget without changing foreground chat.
# 函数用途: 从结构化 client_wait 标志和配置派生本次模型请求超时；0 或前台请求保持全局 request_timeout。
def _gateway_model_request_timeout_seconds(agent: SimpleAgent, request: dict) -> float | None:
    if bool(request.get("client_wait", True)):
        return None
    raw = getattr(agent.config, "gateway_background_model_request_timeout", 0)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


# LLM: gateway task attributes carry explicit contracts into the root run without parsing prompts.
# 函数用途: 规范化请求里的 task_attributes；空字典保持 None，避免普通请求多带状态。
def _gateway_task_attributes(request: dict) -> dict | None:
    attrs = request.get("task_attributes")
    if not isinstance(attrs, dict) or not attrs:
        return None
    return dict(attrs)


# LLM: _run_gateway_control_plane_ask answers read-only status probes without a model turn.
# 函数用途: control_plane 是机器控制面，直接读任务卡/看板，避免前台状态查询被长模型输出卡死。
def _run_gateway_control_plane_ask(agent: SimpleAgent, prompt: str) -> AgentRunResult:
    del prompt
    response = _control_plane_status_sentence(agent)
    return AgentRunResult(
        prompt="",
        response=response,
        backend="control_plane",
        used_memories=0,
        tool_rounds=0,
    )


# LLM: _control_plane_status_sentence is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _control_plane_status_sentence(agent: SimpleAgent) -> str:
    try:
        board = agent.subagents.write_board(recent_limit=10)
    except Exception as exc:
        return f"控制面状态读取失败：{type(exc).__name__}: {exc}"
    items = list(getattr(board, "items", []) or [])
    tasks = _subagent_task_snapshot(agent) or items
    summary = getattr(board, "summary", {}) or {}
    total = int(_summary_value(summary, "total", len(items)) or 0)
    background_parts = _gateway_background_status_parts(agent)
    if total <= 0:
        if background_parts:
            return "当前没有进行中的子代理任务；" + "；".join(background_parts) + "。"
        return "当前没有进行中的子代理任务。"
    status_counts = _status_counts(summary, items)
    blocking = _blocking_run_ids(tasks)
    running = status_counts.get("RUNNING", 0)
    unverified = _unresolved_verification_count(tasks, "UNVERIFIED")
    parts = [f"当前共有 {total} 个子代理任务"]
    if running:
        parts.append(f"RUNNING {running}")
    if unverified:
        parts.append(f"UNVERIFIED {unverified}")
    if blocking:
        parts.append(f"阻塞/未完成 run_id: {', '.join(blocking[:5])}")
    else:
        parts.append("暂无阻塞任务")
    parts.extend(background_parts)
    return "；".join(parts) + "。"


# LLM: control_plane must surface no-wait main-agent work from gateway refs, not only subagent cards.
# 函数用途: 生成后台主代理请求的控制面摘要；只读机器状态和 chunk 预览。
def _gateway_background_status_parts(agent: SimpleAgent) -> list[str]:
    try:
        snapshots = gateway_background_request_snapshots(gateway_paths(agent), limit=5)
    except Exception:
        return []
    if not snapshots:
        return []
    processing = sum(1 for item in snapshots if str(item.get("status") or "").lower() == "processing")
    pending = sum(1 for item in snapshots if str(item.get("status") or "").lower() == "pending")
    failed = sum(1 for item in snapshots if str(item.get("status") or "").lower() == "failed")
    latest = snapshots[0]
    parts = [f"后台主代理请求 {len(snapshots)} 个"]
    if processing:
        parts.append(f"processing {processing}")
    if pending:
        parts.append(f"pending {pending}")
    if failed:
        parts.append(f"failed {failed}")
    request_id = str(latest.get("request_id") or "")
    if request_id:
        parts.append(f"最近 request_id={request_id}")
    preview = str(latest.get("last_chunk_preview") or "").strip()
    if preview:
        parts.append(f"最近进度={preview[:160]}")
    return parts


# LLM: _subagent_task_snapshot is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _subagent_task_snapshot(agent: SimpleAgent) -> list[object]:
    try:
        return list(agent.subagents.list_runs())
    except Exception:
        return []


# LLM: _summary_value is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _summary_value(summary: object, key: str, default: object = 0) -> object:
    return summary.get(key, default) if isinstance(summary, dict) else default


# LLM: _status_counts is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _status_counts(summary: object, items: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(summary, dict):
        for key, value in summary.items():
            text = str(key or "").upper()
            if text in {"PLANNING", "RUNNING", "AWAITING_ACCEPTANCE", "DONE", "BLOCKED", "FAILED", "TIMEOUT"}:
                counts[text] = int(value or 0)
    if counts:
        return counts
    for item in items:
        status = str(getattr(item, "status", "") or "").upper()
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts


# LLM: _blocking_run_ids is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _blocking_run_ids(items: list[object]) -> list[str]:
    try:
        return closeout_blocking_task_ids(items)
    except Exception:
        return _fallback_blocking_run_ids(items)


# LLM: _fallback_blocking_run_ids is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _fallback_blocking_run_ids(items: list[object]) -> list[str]:
    blocking_statuses = {"PLANNING", "RUNNING", "BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}
    result: list[str] = []
    for item in items:
        status = str(getattr(item, "status", "") or "").upper()
        verification = str(getattr(item, "verification_status", "") or "").upper()
        if status in blocking_statuses or verification in {"UNVERIFIED", "NEEDS_ACCEPTANCE"}:
            run_id = str(getattr(item, "id", "") or "").strip()
            if run_id:
                result.append(run_id)
    return result


# LLM: _unresolved_verification_count is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _unresolved_verification_count(items: list[Any], expected: str) -> int:
    count = 0
    for item in items:
        if str(getattr(item, "verification_status", "") or "").upper() != expected:
            continue
        try:
            if task_resolved_for_closeout(item, items):
                continue
        except Exception:
            pass
        count += 1
    return count


# LLM: _stop_gateway_request_lease 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求租约的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


# LLM: _copy_final_lease_fields 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理copyfinal租约字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    final_request = read_json_file(request_path)
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


# LLM: _execute_gateway_request_body 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求body的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    except Exception as exc:
        recovered = _recover_background_subagent_work_after_gateway_error(context, exc)
        if recovered is None:
            raise
        _update_response_from_result(response, recovered, context["request"])
        response["gateway_recovered_from_error"] = type(exc).__name__
        response["gateway_recovery_error"] = f"{type(exc).__name__}: {exc}"
        return
    recovered = _recover_background_subagent_work_after_gateway_result(context, result)
    if recovered is not None:
        _update_response_from_result(response, recovered, context["request"])
        response["gateway_recovered_after_result"] = str(getattr(result, "backend", "") or "root")
        return
    _update_response_from_result(response, result, context["request"])


# LLM: Background gateway asks may time out after creating task cards; recover by running the task ledger.
# 函数用途: no-wait 长任务中，主模型异常后若已有子代理任务卡，gateway 直接按系统合同推进，而不是留下 PLANNING 孤儿任务。
def _recover_background_subagent_work_after_gateway_error(
    context: dict,
    exc: Exception,
) -> AgentRunResult | None:
    request = context["request"]
    if bool(request.get("client_wait", True)):
        return None
    agent = context["agent"]
    if not _has_recoverable_subagent_work(agent):
        return None
    response = _background_deferred_response(agent, exc)
    return AgentRunResult(
        prompt="",
        response=response,
        backend="gateway_subagent_deferred",
        used_memories=0,
        tool_rounds=0,
    )


# LLM: _recover_background_subagent_work_after_gateway_result is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _recover_background_subagent_work_after_gateway_result(
    context: dict,
    result: AgentRunResult,
) -> AgentRunResult | None:
    request = context["request"]
    if bool(request.get("client_wait", True)):
        return None
    if str(getattr(result, "backend", "") or "") in {"gateway_subagent_recovery", "gateway_subagent_deferred"}:
        return None
    agent = context["agent"]
    if not _has_recoverable_subagent_work(agent):
        return None
    response = _background_deferred_response(agent, "root_result_left_recoverable_subagent_work")
    return AgentRunResult(
        prompt="",
        response=response,
        backend="gateway_subagent_deferred",
        used_memories=0,
        tool_rounds=0,
    )


# LLM: _has_recoverable_subagent_work is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _has_recoverable_subagent_work(agent: SimpleAgent) -> bool:
    try:
        runs = list(agent.subagents.list_runs())
    except Exception:
        return False
    return any(_subagent_run_needs_recovery(run) for run in runs)


# LLM: _subagent_run_needs_recovery is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _subagent_run_needs_recovery(run: object) -> bool:
    status = str(getattr(run, "status", "") or "").upper()
    verification = str(getattr(run, "verification_status", "") or "").upper()
    if status in {"DONE", "CANCELLED"} and verification in {"", "VERIFIED"}:
        return False
    return bool(status)


# LLM: _gateway_runtime_capability_config is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _gateway_runtime_capability_config(agent: SimpleAgent) -> CapabilityConfig:
    path = Path(getattr(agent, "capability_config_path", "") or default_capability_config_path(getattr(agent, "root", ".")))
    try:
        snapshot = load_capability_config_snapshot(path)
    except (FileNotFoundError, OSError, ValueError):
        return CapabilityConfig()
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = snapshot
    return snapshot.config


# LLM: _gateway_capability_router is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _gateway_capability_router(agent: SimpleAgent, cfg: CapabilityConfig) -> CapabilityRouter:
    tool_specs = [spec for spec in agent.tools.specs() if getattr(spec, "category", "") != "orchestration"]
    return CapabilityRouter(config=cfg, tool_specs=tool_specs)


# LLM: _gateway_background_recovery_watch_params is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _gateway_background_recovery_watch_params(agent: SimpleAgent) -> WatchParams:
    config = getattr(agent, "config", None)
    return WatchParams(
        apply=True,
        execute_runners=True,
        execute_acceptance_tests=True,
        auto_apply_acceptance_followup=True,
        workflow_mode="off",
        max_runners=_positive_int_config(config, "dispatch_default_max_runners", 1),
        limit=_positive_int_config(config, "dispatch_default_limit", 20),
        reviewer="gateway-background-recovery",
        note="gateway background request recovered after root model error",
        max_cards=0,
        probe=True,
        interval=0,
        max_cycles=_gateway_background_recovery_max_cycles(config),
        force_lock=True,
    )


# LLM: _positive_int_config is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _positive_int_config(config: object, field: str, default: int) -> int:
    try:
        value = int(getattr(config, field, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


# LLM: _gateway_background_recovery_max_cycles is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _gateway_background_recovery_max_cycles(config: object) -> int:
    try:
        raw = int(getattr(config, "dispatch_max_consecutive_rounds", _BACKGROUND_RECOVERY_DEFAULT_CONFIG_CYCLES) or 0)
    except (TypeError, ValueError):
        raw = _BACKGROUND_RECOVERY_DEFAULT_CONFIG_CYCLES
    if raw <= 0:
        return _BACKGROUND_RECOVERY_DEFAULT_MAX_CYCLES
    if raw == _BACKGROUND_RECOVERY_DEFAULT_CONFIG_CYCLES:
        return _BACKGROUND_RECOVERY_DEFAULT_MAX_CYCLES
    return max(1, raw)


# LLM: _background_recovery_response is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _background_recovery_response(agent: SimpleAgent, report: object, reason: object) -> str:
    board_sentence = _control_plane_status_sentence(agent)
    summary = getattr(report, "summary", {}) or {}
    total_cycles = summary.get("total", len(getattr(report, "records", []) or [])) if isinstance(summary, dict) else 0
    reason_text = type(reason).__name__ if isinstance(reason, Exception) else str(reason or "unknown")
    prefix = "后台主代理模型调用异常" if isinstance(reason, Exception) else "后台主代理返回时仍有未完成任务卡"
    return (
        f"{prefix}，系统已检测到已创建的任务卡，并自动交给调度/验收链路继续推进。"
        f"恢复轮次={total_cycles}；恢复原因={reason_text}；{board_sentence}"
    )


# LLM: _background_deferred_response is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _background_deferred_response(agent: SimpleAgent, reason: object) -> str:
    board_sentence = _control_plane_status_sentence(agent)
    reason_text = type(reason).__name__ if isinstance(reason, Exception) else str(reason or "unknown")
    prefix = "后台主代理模型调用异常" if isinstance(reason, Exception) else "后台主代理返回时仍有未完成任务卡"
    return (
        f"{prefix}，系统已检测到已创建的任务卡，并已交给 gateway daemon/worker 调度链路继续推进。"
        f"恢复原因={reason_text}；{board_sentence}"
    )


# LLM: _prepare_gateway_request_context 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理prepare网关请求上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_json_file(response_path)
    if existing_response:
        return {"existing_response": existing_response}
    started_at = time.time()
    response = _build_gateway_response_base(
        _GatewayResponseBaseContext(request, request_path, request_id, kind, started_at)
    )
    context = {
        "request": request,
        "request_id": request_id,
        "kind": kind,
        "started_at": started_at,
        "request_path": request_path,
        "response_path": response_path,
        "response": response,
    }
    audit_request_processing(agent, context)
    return context


# LLM: _finalize_gateway_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理finalize网关响应相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _finalize_gateway_response(context: dict, response: dict) -> None:
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)
    _finalize_runtime_task_card(context, response)


# LLM: _finalize_runtime_task_card closes the durable TaskCard that was opened for a no-wait gateway request.
# 函数用途: 根据 gateway 响应把 runtime task 标记为 completed/failed，并记录响应产物或错误。
def _finalize_runtime_task_card(context: dict, response: dict) -> None:
    request = context["request"]
    task_id = str(request.get("runtime_task_id") or "").strip()
    agent = context.get("agent")
    if not task_id or agent is None:
        return
    cards_root = getattr(agent, "runtime_cards_root", None)
    messages_root = getattr(agent, "runtime_messages_root", None)
    if cards_root is None or messages_root is None:
        return
    from ..cards import CardStore, TaskStatus
    from ..messages import MessageStore, MessageTool
    from ..runtime import TaskRuntime

    cards = CardStore(cards_root)
    runtime = TaskRuntime(cards, MessageTool(MessageStore(messages_root)))
    if bool(response.get("ok")):
        runtime.complete_task(task_id, artifact_refs=[str(context["response_path"])])
        response["runtime_task_status"] = TaskStatus.COMPLETED.value
        return
    task = cards.get_task(task_id)
    task.metadata["gateway_error_code"] = response.get("error_code", "")
    task.metadata["gateway_error"] = response.get("error", "")
    cards.save_task(task)
    cards.update_task_status(task_id, TaskStatus.FAILED, reason=str(response.get("error_code") or "gateway_failed"))
    response["runtime_task_status"] = TaskStatus.FAILED.value


# LLM: _complete_gateway_request_audit 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理complete网关请求audit相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _complete_gateway_request_audit(agent: SimpleAgent, context: dict, request_path: Path, response: dict) -> None:
    audit_request_completed(
        agent,
        params=AuditRequestCompletedParams(
            response=response,
            request=context["request"],
            request_path=request_path,
            response_path=context["response_path"],
        ),
    )


# LLM: _handle_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    response = context["response"]
    lease_stop, lease_thread = _start_gateway_request_lease(
        _GatewayLeaseStartContext(
            agent,
            context["request"],
            request_path,
            context["request_id"],
            refresh_lease,
            worker_id,
        )
    )
    chunk_path = gateway_chunk_path(gateway_paths(agent), context["request_id"])
    chunk_path_abs, _ = open_chunk_stream(chunk_path)

    try:
        _execute_gateway_request_body({**context, "agent": agent}, lambda chunk: write_chunk(chunk_path_abs, chunk))
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": response.get("error_code") or type(exc).__name__.upper(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        close_chunk_stream(chunk_path_abs)
    _finalize_gateway_response({**context, "agent": agent}, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response
