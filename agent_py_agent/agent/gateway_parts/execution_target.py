# LLM: 活动目标只投影原claim/请求/task；可选blocking/deadline仅约束foreground原读锁，默认算法不变，不授执行权。
# 模块用途: 区分实际执行者与等待请求，供 TUI、IM 和 Gateway 的插话/停止共用精确目标。
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from .daemon_metadata import process_identity_is_live
from .io import locked_file_transition, read_json_file_report
from .paths import GatewayPaths

_TARGET_SCHEMA = "gateway_execution_target.v1"


# LLM: 不可变投影不是新执行权；claim_id 固定工作片，transport attempt 和 runtime authority 固定请求实际代次。
# 类用途: 保存一次用户操作所看到的实际执行绑定，供原控制入口拒绝过期操作。
@dataclass(frozen=True)
class GatewayExecutionTarget:
    thread_id: str
    kind: str
    turn_id: str
    claim_id: str
    execution_attempt_id: str = ""
    runtime_authority: dict[str, str] = field(default_factory=dict)
    task_id: str = ""

    # LLM: 只输出身份字段，不输出任务正文、路径、模型内容或凭据。
    # 函数用途: 向客户端返回可携回原控制入口复核的执行快照。
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": _TARGET_SCHEMA, **asdict(self)}

    # LLM: 只接受本版结构；缺失、不支持或混合类型不补猜目标。客户端恢复与服务端比较共用此校验。
    # 函数用途: 校验收到的执行快照，坏快照返回空且不取得控制资格。
    @classmethod
    def from_dict(cls, data: object) -> GatewayExecutionTarget | None:
        if not isinstance(data, dict) or data.get("schema_version") != _TARGET_SCHEMA:
            return None
        if any(not isinstance(data.get(key), str) or not data[key] for key in ("thread_id", "kind", "turn_id", "claim_id")):
            return None
        if data["kind"] not in {"request", "task"}:
            return None
        attempt = data.get("execution_attempt_id", "")
        authority = data.get("runtime_authority", {})
        task_id = data.get("task_id", "")
        if not isinstance(attempt, str) or not isinstance(authority, dict) or not isinstance(task_id, str):
            return None
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in authority.items()):
            return None
        return cls(data["thread_id"], data["kind"], data["turn_id"], data["claim_id"], attempt, dict(authority), task_id)


# LLM: 调用方已按认证 owner 解析 store/thread；只读原账两次核对 claim，单有 processing、task active 或预绑定均不够。
#   失败不创建线程、不抢 claim、不发模型请求。多进程沿同一 owner/thread 原账读取，不创造 worker 身份。
#   显式资源约束在每段原读取之间核同一deadline；claims/task原只读不取锁，foreground仅原JSON读锁可显式try。
# 函数用途: 找到当前会话真正持有执行车道的前台请求或后台任务。
def read_gateway_execution_target(
    paths: GatewayPaths, store: object, thread_id: str, *, blocking: bool = True, deadline: float | None = None,
) -> GatewayExecutionTarget | None:
    from ..common.json_io import check_io_deadline
    try:
        check_io_deadline(deadline)
        claim = store.claims.load(thread_id)
        check_io_deadline(deadline)
        if not _claim_is_current(claim, thread_id):
            return None
        if claim.get("reason") == "gateway_foreground_turn":
            options = {} if blocking and deadline is None else {'blocking': blocking, 'deadline': deadline}
            target = _foreground_target(paths, claim, thread_id, **options)
        else:
            target = _background_target(store, claim, thread_id)
        check_io_deadline(deadline)
        current = store.claims.load(thread_id)
        check_io_deadline(deadline)
        if not _claim_is_current(current, thread_id) or current.get("claim_id") != claim.get("claim_id"):
            return None
        return target
    except (BlockingIOError, InterruptedError):
        if not blocking or deadline is not None:
            raise
        return None
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return None


# LLM: 不从消息或客户端 thread 取得 owner 权限；调用方给出的 store 必须已由认证 scope 解析。
# 函数用途: 按客户端原快照核对执行代次；运行绑定尚未发布时，车道与运输代次仍能固定同一次执行。
def execution_target_matches(expected: GatewayExecutionTarget, current: GatewayExecutionTarget | None) -> bool:
    return current is not None and all(
        getattr(expected, key) == getattr(current, key)
        for key in ("thread_id", "kind", "turn_id", "claim_id", "execution_attempt_id", "task_id")
    ) and (not expected.runtime_authority or expected.runtime_authority == current.runtime_authority)


# LLM: 调用方先持需要的 Gateway T、guidance M、task 锁，最后进原 claim JSON 锁 C；锁内不得续租或结束 claim。
#   不创建第二份执行状态，不等待模型/工具；阻止校验后下一轮抢车道，原租约过期则无资格。
# 函数用途: 在一次插话或控制提交期间固定真实执行者，返回是否仍与用户原快照相同。
@contextmanager
def execution_target_guard(paths: GatewayPaths, store: object, expected: GatewayExecutionTarget):
    with locked_file_transition(store.storage.background_claim_path(expected.thread_id)):
        yield execution_target_matches(expected, read_gateway_execution_target(paths, store, expected.thread_id))


# LLM: 只用原 claim 协议、线程、租约和宿主出生身份，过期或明确死宿主不投影为实际执行。
# 函数用途: 校验执行车道快照仍属于当前线程，不写心跳或执行状态。
def _claim_is_current(claim: object, thread_id: str) -> bool:
    return (
        isinstance(claim, dict)
        and claim.get("schema_version") == "background_run_claim.v1"
        and claim.get("status") == "running"
        and claim.get("thread_id") == thread_id
        and claim.get("claim_scope_id") == thread_id
        and isinstance(claim.get("claim_id"), str) and bool(claim["claim_id"])
        and not claim.get("load_error")
        and type(claim.get("expires_at")) in {int, float}
        and math.isfinite(claim["expires_at"])
        and float(claim.get("expires_at") or 0) > time.time()
        and process_identity_is_live(claim.get("owner_process")) is not False
    )


# LLM: gateway:<request_id> 是原前台 claim 协议；同时核对请求中 write-ahead 绑定，等待者不能借别人的 claim。
#   显式资源参数仅透传同一原JSON读锁；默认调用形状和目标选择不变，忙不能伪装为target缺失。
# 函数用途: 从真实前台租约读取对应请求代次和 core 已发布的运行绑定，不枚举或读取任务内容。
def _foreground_target(paths: GatewayPaths, claim: dict, thread_id: str, *, blocking=True, deadline=None) -> GatewayExecutionTarget | None:
    from .request_binding import gateway_runtime_authority

    task_id = str(claim.get("task_id") or "")
    if not task_id.startswith("gateway:"):
        return None
    request_id = task_id[len("gateway:"):]
    if not request_id or "/" in request_id or "\\" in request_id:
        return None
    if (paths.terminal / f"{request_id}.json").exists():
        return None
    options = {} if blocking and deadline is None else {'blocking': blocking, 'deadline': deadline}
    report = read_json_file_report(paths.processing / f"{request_id}.json", context="gateway.execution_target.request", **options)
    if (not blocking or deadline is not None) and report.lock_busy:
        raise BlockingIOError('gateway execution target request is busy')
    payload = report.payload
    if report.load_error or payload.get("id") != request_id:
        return None
    binding = payload.get("conversation_claim")
    if not isinstance(binding, dict) or any(binding.get(key) != value for key, value in {
        "schema_version": "gateway_conversation_claim.v1", "request_id": request_id,
        "thread_id": thread_id, "task_id": task_id,
    }.items()):
        return None
    if payload.get("turn_phase", "open") != "open" or payload.get("cancel_requested"):
        return None
    if payload.get("status") != "processing" or payload.get("control_status") == "stopping":
        return None
    return GatewayExecutionTarget(
        thread_id, "request", request_id, claim["claim_id"],
        str(payload.get("execution_attempt_id") or ""), gateway_runtime_authority(payload, request_id) or {},
    )


# LLM: task ID 必须来自同一 running claim 并回查同线程链接；活动 Goal、展示焦点或更新时刻都不能代替。
# 函数用途: 返回普通后台主任务的执行目标，不把命名 Audit 或别的线程当成当前聊天。
def _background_target(store: object, claim: dict, thread_id: str) -> GatewayExecutionTarget | None:
    task_id = str(claim.get("task_id") or "")
    if not task_id:
        return None
    link = store.tasks.load(task_id)
    if link is None or link.thread_id != thread_id or link.status != "active" or link.work_kind == "audit":
        return None
    authority = claim.get("runtime_authority", {})
    if not isinstance(authority, dict):
        return None
    if authority and (
        not isinstance(authority, dict) or authority.get("schema_version") != "conversation_claim_execution.v1"
        or authority.get("claim_id") != claim["claim_id"] or authority.get("task_id") != task_id
        or any(not isinstance(authority.get(key), str) or not authority[key]
               for key in ("invocation_run_id", "run_id", "agent_run_id", "attempt_id"))
    ):
        return None
    return GatewayExecutionTarget(
        thread_id, "task", authority.get("invocation_run_id", task_id), claim["claim_id"],
        authority.get("attempt_id", ""), dict(authority), task_id,
    )


# LLM: HTTP 轮询缺少 Gateway 原路径时不猜请求位置；空目标明确清除客户端旧快照。
# 函数用途: 将实际执行绑定转换成客户端轮询载荷，不创建执行状态。
def execution_target_payload(paths, store, thread_id) -> dict | None:
    target = read_gateway_execution_target(paths, store, thread_id) if paths is not None else None
    return target.to_dict() if target is not None else None


__all__ = ["GatewayExecutionTarget", "read_gateway_execution_target", "execution_target_payload"]
