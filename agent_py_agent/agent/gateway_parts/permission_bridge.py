# LLM: 本模块是 Gateway 工具审批的跨进程文件桥；路径由 chunk/request identity 哈希推导，读取后仍需核验完整 request binding。
# 模块用途: 让 TUI 把审批决定原子写回正在等待的 Gateway 工具调用，并支持取消令牌中止等待。

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from pathlib import Path

from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from .io import read_json_file_report, write_json_file_atomic

_APPROVAL_POLL_SECONDS = 0.05


# LLM: 决定文件必须放在 processing 的隐藏子目录，不能混入顶层 *.json 请求扫描或接受 permission_id 路径穿越。
# 函数用途: 从 live chunk 路径和审批身份推导唯一决定文件路径。
def gateway_permission_decision_path(
    chunk_path: Path,
    request: ToolApprovalRequest,
) -> Path:
    request_hash = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:24]
    permission_hash = hashlib.sha256(request.permission_id.encode("utf-8")).hexdigest()[:24]
    return Path(chunk_path).parent / ".approvals" / request_hash / f"{permission_hash}.json"


# LLM: 写回同时复制请求的精确 binding；Gateway 等待端会逐字段核验，UI label/feedback 无法替换另一条调用的授权。
# 函数用途: 原子提交一条 Gateway 审批决定并返回落盘路径。
def write_gateway_permission_decision(
    chunk_path: Path,
    request_value: ToolApprovalRequest | Mapping[str, object],
    decision_value: ToolApprovalDecision | Mapping[str, object],
) -> Path:
    request = _approval_request(request_value)
    decision = _approval_decision(decision_value)
    if decision.permission_id != request.permission_id:
        raise ValueError("gateway permission decision id mismatch")
    target = gateway_permission_decision_path(chunk_path, request)
    write_json_file_atomic(
        target,
        {
            **decision.to_dict(),
            "request_id": request.request_id,
            "binding": dict(request.binding),
        },
    )
    return target


# LLM: 等待只接受 schema 校验、request_id、permission_id 和完整 binding 均匹配的原子文件；取消永远优先于批准。
# 函数用途: 阻塞等待 Gateway 审批答复，或在运行被取消时返回 cancelled。
def wait_for_gateway_permission_decision(
    chunk_path: Path,
    request_value: ToolApprovalRequest | Mapping[str, object],
    *,
    cancellation_token: object | None = None,
    poll_seconds: float = _APPROVAL_POLL_SECONDS,
) -> ToolApprovalDecision:
    request = _approval_request(request_value)
    target = gateway_permission_decision_path(chunk_path, request)
    interval = max(0.01, float(poll_seconds or _APPROVAL_POLL_SECONDS))
    while True:
        if _cancelled(cancellation_token):
            return ToolApprovalDecision(request.permission_id, "cancelled")
        report = read_json_file_report(target, context="gateway.permission.decision.read")
        if report.load_error is not None:
            return ToolApprovalDecision(request.permission_id, "unavailable")
        if report.payload:
            decision = _verified_gateway_decision(request, report.payload)
            _remove_consumed_decision(target)
            return decision
        time.sleep(interval)


# LLM: disabled/noninteractive clients fail closed immediately，不能因为 writer 恰好有同名方法而无限等待无人消费的审批。
# 函数用途: 创建一条明确表示当前客户端不支持交互审批的决定。
def unavailable_gateway_permission_decision(
    request_value: ToolApprovalRequest | Mapping[str, object],
) -> ToolApprovalDecision:
    request = _approval_request(request_value)
    return ToolApprovalDecision(request.permission_id, "unavailable")


# LLM: 文件内容核验逐字段比较 canonical binding；任何缺失、额外类型或错配都 fail-closed 为 unavailable。
# 函数用途: 校验并恢复 Gateway 决定文件。
def _verified_gateway_decision(
    request: ToolApprovalRequest,
    payload: Mapping[str, object],
) -> ToolApprovalDecision:
    try:
        decision = ToolApprovalDecision.from_mapping(payload)
    except (TypeError, ValueError):
        return ToolApprovalDecision(request.permission_id, "unavailable")
    raw_binding = payload.get("binding")
    binding = (
        {str(key): str(value or "").strip() for key, value in raw_binding.items()}
        if isinstance(raw_binding, Mapping)
        else {}
    )
    if (
        decision.permission_id != request.permission_id
        or str(payload.get("request_id") or "").strip() != request.request_id
        or binding != request.binding
    ):
        return ToolApprovalDecision(request.permission_id, "unavailable")
    return decision


# LLM: 消费后仅删除这一条精确临时决定和空父目录；不得扫描或清理其它请求的审批文件。
# 函数用途: 移除已消费的决定文件并尽力回收两个空隐藏目录。
def _remove_consumed_decision(target: Path) -> None:
    try:
        target.unlink()
    except OSError:
        return
    for parent in (target.parent, target.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            break


# LLM: cancellation_token 只读显式 cancelled 字段或结构化 is_cancelled 方法，不解析异常/文本原因。
# 函数用途: 判断等待中的工具回合是否已被取消。
def _cancelled(cancellation_token: object | None) -> bool:
    if cancellation_token is None:
        return False
    checker = getattr(cancellation_token, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(cancellation_token, "cancelled", False))


# LLM: request 转换统一走 ToolApprovalRequest 校验，禁止桥接层直接信任 dict。
# 函数用途: 规范请求对象。
def _approval_request(
    value: ToolApprovalRequest | Mapping[str, object],
) -> ToolApprovalRequest:
    return value if isinstance(value, ToolApprovalRequest) else ToolApprovalRequest.from_mapping(value)


# LLM: decision 转换统一走有限枚举校验，未知 payload 不得默认批准。
# 函数用途: 规范决定对象。
def _approval_decision(
    value: ToolApprovalDecision | Mapping[str, object],
) -> ToolApprovalDecision:
    return value if isinstance(value, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(value)


__all__ = [
    "gateway_permission_decision_path",
    "unavailable_gateway_permission_decision",
    "wait_for_gateway_permission_decision",
    "write_gateway_permission_decision",
]
