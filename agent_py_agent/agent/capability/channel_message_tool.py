# LLM: 当前 owner 的原生消息发送工具。目标身份只取可信 owner 结构，不接受模型提供任意用户 ID；
#   附件必须命中 owner artifact registry、状态 ready、hash 未漂移且真实路径仍在 owner 根内。
#   真发送统一复用 DeliveryService，修改时同步 delivery/、conversation/channels.py 和测试。
# 模块用途: 让已连接飞书的 Agent 能把文字和已有产物直接发回自己的用户，而不是只会在服务器写文件。
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..artifacts.registry import ArtifactRegistryRecord, resolve_artifact_record_report
from ..conversation.channels import project_user_reply, redact_host_absolute_paths
from ..delivery import (
    ChannelAttachment,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MAX_ATTACHMENTS = 10


# LLM: send_message 是通道无关的模型入口，目标固定为当前 owner；不要新增 feishu_send 等重叠工具。
# 函数用途: 返回发送文字或 owner 已登记附件的工具说明书。
def build_send_message_spec() -> ToolSpec:
    return ToolSpec(
        name="send_message",
        category="messaging",
        effect="mutating",
        idempotency_scope="operation",
        description=(
            "把文字或已经生成的文件原生发送到当前用户连接的聊天通道。"
            "用户说‘发我/传给我/作为附件发送’时，在文件生成后调用；不需要用户提供飞书 ID。"
            "attachments 每项使用 Recent Artifact Refs 里的完整 path 原值。"
        ),
        use_cases=[
            "用户要求把刚生成的文件作为附件发回当前飞书会话",
            "用户追问‘把上一个文件发我’，直接复用 Recent Artifact Refs，不要重新生成或复制",
            "需要主动给当前 owner 发送一条短消息",
        ],
        avoid_when=[
            "只需要在当前轮正常回复文字时，直接给最终回复，不要额外发送一遍",
            "文件尚未生成或尚未登记时，先完成文件产物",
            "不能用它给其他用户或任意群发送消息；目标由当前 owner 身份固定决定",
        ],
        keywords=[
            "发我", "发送", "飞书", "附件", "传给我", "把文件给我", "send", "message", "attachment",
        ],
        parameters={
            "message": "可选。随附件一起发送的简短说明；只发附件时可留空。",
            "attachments": "可选。Recent Artifact Refs 中 path 组成的数组，最多 10 项。",
        },
        parameter_schema={
            "message": {"type": "string"},
            "attachments": {"type": "array", "items": {"type": "string"}, "maxItems": _MAX_ATTACHMENTS},
        },
        required_parameters=[],
        internal_parameters=["__run_scope", "__tool_call_id"],
        examples=[
            '{"tool":"send_message","message":"文件写好了。","attachments":["<Recent Artifact Refs path>"]}',
            '{"tool":"send_message","attachments":["<已登记产物的完整 path>"]}',
        ],
    )


# LLM: 工具实例只负责发送业务；执行前占位、终态保存和重放统一由 Tool Gateway 操作账本负责。
# 类用途: 校验当前用户和产物后，通过飞书等已连接通道发送消息及附件。
class SendMessageTool(BaseTool):
    # LLM: 每个 owner agent 各持有自己的 adapter hub；跨 owner 隔离由 owner registry 和操作账本共同保证。
    # 函数用途: 创建当前 agent 的原生消息发送工具。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_send_message_spec()
        self._delivery: DeliveryService = agent.delivery_service

    # LLM: 外部副作用前必须先完成 owner target、registry、真实路径和 hash 四层校验。
    # 函数用途: 执行一次发给当前用户的文字/附件发送，并返回不含服务器路径的回执。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        message = str(params.get("message") or "").strip()
        attachment_refs = _attachment_refs(params.get("attachments"))
        if isinstance(attachment_refs, ToolExecutionResult):
            return attachment_refs
        if not message and not attachment_refs:
            return _error("message 和 attachments 不能同时为空", "TOOL_INVALID_ARGUMENTS")
        provider, target, owner_root = _owner_delivery_identity(self.agent)
        capabilities = self._delivery.registry.capabilities_for(provider)
        if not capabilities.proactive or not target or owner_root is None:
            return _error("当前 owner 没有可用的外部消息通道", "CHANNEL_ADAPTER_UNAVAILABLE")
        attachments = _resolve_attachments(owner_root, attachment_refs)
        if isinstance(attachments, ToolExecutionResult):
            return attachments
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        delivery_context = DeliveryContext(
            channel=provider,
            target=target,
            mode="proactive",
            request_id=str(scope.get("request_id") or ""),
            task_id=str(scope.get("task_id") or scope.get("run_id") or ""),
        )
        envelope = ReplyEnvelope(
            content=message,
            attachments=attachments,
        )
        receipt_key = _receipt_key(delivery_context, envelope, params)
        receipt = self._delivery.deliver(delivery_context, envelope)
        if receipt.delivery_status != "sent":
            return _error(
                "消息或附件没有成功送达当前聊天通道",
                receipt.error_code or "CHANNEL_SEND_FAILED",
            )
        payload = _success_payload(provider, message, attachments, receipt_key)
        return _success_result(provider, message, attachments, payload)


# LLM: attachments 是开放世界的 artifact 引用数组；这里只限制资源数量和元素标量类型。
# 函数用途: 清洗并去重模型传入的附件引用。
def _attachment_refs(value: object) -> list[str] | ToolExecutionResult:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        return _error("attachments 必须是字符串数组", "TOOL_INVALID_ARGUMENTS")
    refs = [str(item or "").strip() for item in value if isinstance(item, str) and str(item).strip()]
    if len(refs) != len(value):
        return _error("attachments 每一项都必须是非空字符串", "TOOL_INVALID_ARGUMENTS")
    refs = list(dict.fromkeys(refs))
    if len(refs) > _MAX_ATTACHMENTS:
        return _error(f"一次最多发送 {_MAX_ATTACHMENTS} 个附件", "TOOL_INVALID_ARGUMENTS")
    return refs


# LLM: owner 身份是唯一发送目标权威；收件 open_id 取 scoped config 原始值，不把内部 owner 路径当地址。
# 函数用途: 从当前 agent 的可信 owner 配置取得 provider、目标 ID 和隔离根目录。
def _owner_delivery_identity(agent: object) -> tuple[str, str, Path | None]:
    home = getattr(agent, "home_paths", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip().lower()
    config = getattr(agent, "config", None)
    target = str(getattr(config, "my_agent_owner_id", "") or "").strip()
    root = getattr(home, "owner_home_dir", None)
    try:
        owner_root = Path(root).expanduser().resolve(strict=True) if root else None
    except OSError:
        owner_root = None
    return provider, target, owner_root


# LLM: registry 记录和当前文件 hash 必须一致；路径即使在记录中也要重新 resolve 防 symlink 逃逸。
# 函数用途: 把 artifact_id/路径引用解析成通过 owner 边界验证的通道附件。
def _resolve_attachments(
    owner_root: Path,
    refs: list[str],
) -> tuple[ChannelAttachment, ...] | ToolExecutionResult:
    attachments: list[ChannelAttachment] = []
    for ref in refs:
        record = _resolve_registered_artifact(owner_root, ref)
        if isinstance(record, ToolExecutionResult):
            return record
        if record is None:
            return _error(f"没有找到已登记产物：{Path(ref).name or ref}", "ARTIFACT_NOT_REGISTERED")
        if str(record.status or "").strip().lower() != "ready":
            return _error(f"产物尚未处于可发送状态：{Path(record.path).name}", "ARTIFACT_VALIDATION_FAILED")
        try:
            path = Path(record.path).expanduser().resolve(strict=True)
            path.relative_to(owner_root)
        except (OSError, ValueError):
            return _error("产物路径不在当前用户的隔离目录内", "PATH_OUTSIDE_WORKSPACE")
        if not path.is_file():
            return _error(f"产物文件不存在：{path.name}", "PATH_NOT_FOUND")
        digest = _sha256_file(path)
        if record.sha256 and digest != record.sha256:
            return _error(f"产物内容已变化，需要重新登记：{path.name}", "ARTIFACT_VALIDATION_FAILED")
        attachments.append(
            ChannelAttachment(
                artifact_id=record.artifact_id,
                path=str(path),
                name=path.name,
                kind=record.kind or path.suffix.lower().lstrip(".") or "file",
                sha256=digest,
                size_bytes=path.stat().st_size,
            )
        )
    return tuple(attachments)


# LLM: artifact registry 跟随任务根保存，不固定在 owner 根；绝对 path 允许沿其祖先查找，但绝不全盘扫描。
# 函数用途: 在 owner 边界内定位登记该产物的最近任务 registry，并返回它的当前记录。
def _resolve_registered_artifact(
    owner_root: Path,
    ref: str,
) -> ArtifactRegistryRecord | ToolExecutionResult | None:
    roots = _artifact_registry_roots(owner_root, ref)
    for root in roots:
        report = resolve_artifact_record_report(root, ref, path=ref)
        if report.errors:
            return _error("产物登记表不可可靠读取，为避免错发已安全停止", "ARTIFACT_VALIDATION_FAILED")
        if report.record is not None:
            return report.record
    return None


# LLM: 只有 ref 的真实绝对路径才可派生任务根；路径逃出 owner 时仅返回 owner 根并由后续边界校验拒绝。
# 函数用途: 枚举 owner 根与文件祖先中的少量候选 registry 根。
def _artifact_registry_roots(owner_root: Path, ref: str) -> tuple[Path, ...]:
    roots = [owner_root]
    raw = Path(ref).expanduser()
    if not raw.is_absolute():
        return tuple(roots)
    try:
        path = raw.resolve(strict=False)
        path.relative_to(owner_root)
    except (OSError, ValueError):
        return tuple(roots)
    current = path.parent
    while current != owner_root:
        if (current / "data" / "artifacts" / "registry.jsonl").is_file():
            roots.insert(0, current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return tuple(dict.fromkeys(roots))


# LLM: 幂等键包含 request scope、call id、正文和附件 hash；新用户请求可再次发送，同一调用重试不可重复。
# 函数用途: 为一次外部发送生成稳定回执键。
def _receipt_key(
    context: DeliveryContext,
    envelope: ReplyEnvelope,
    params: dict[str, object],
) -> str:
    scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
    payload = {
        "provider": context.channel,
        "target": context.target,
        "message": envelope.content,
        "request_id": str(scope.get("request_id") or ""),
        "run_id": str(scope.get("run_id") or ""),
        "call_id": str(params.get("__tool_call_id") or ""),
        "attachments": [(item.artifact_id, item.sha256) for item in envelope.attachments],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: 返回给模型的成功事实不得包含 owner id、服务器绝对路径或飞书凭据。
# 函数用途: 生成简洁、可机读的发送成功回执。
def _success_payload(
    provider: str,
    message: str,
    attachments: tuple[ChannelAttachment, ...],
    receipt_key: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "delivery_status": "sent",
        "channel": provider,
        "message_sent": bool(message),
        "attachments": [
            {
                "artifact_id": item.artifact_id,
                "name": item.name,
                "kind": item.kind,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in attachments
        ],
        "receipt_id": receipt_key[:20],
    }


# LLM: 成功外发必须同时返回一份结构化 delivery evidence，供本轮唯一回复出口判断是否还需自动发送；
#   不能让上层从模型最终正文或日志文本猜测“是不是已经发过”。
# 函数用途: 构造带当前 owner 已送达事实的工具结果，同时保持公开 output 不暴露 owner 路径。
def _success_result(
    provider: str,
    message: str,
    attachments: tuple[ChannelAttachment, ...],
    payload: dict[str, Any],
) -> ToolExecutionResult:
    output = dict(payload)
    return ToolExecutionResult(
        "send_message",
        True,
        json.dumps(output, ensure_ascii=False),
        result_envelope={
            "delivery_evidence": {
                "schema_version": "message_tool_delivery.v1",
                "delivery_status": "sent",
                "source_owner_delivery": True,
                "channel": provider,
                "content": redact_host_absolute_paths(project_user_reply(message).content),
                "receipt_id": str(payload.get("receipt_id") or ""),
                "deduplicated": False,
                # 路径只在内部结构化运行事实中保留，供同一 owner transcript 复用附件；
                # ToolExecutionResult.output 仍只暴露不含路径的 _success_payload。
                "attachments": [
                    {
                        "artifact_id": item.artifact_id,
                        "path": item.path,
                        "name": item.name,
                        "kind": item.kind,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                        "ok": True,
                    }
                    for item in attachments
                ],
            }
        },
    )


# LLM: hash 用于验证 registry 指向的内容没有在登记后漂移。
# 函数用途: 分块计算文件 SHA-256。
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# LLM: 工具失败统一返回结构化错误码，避免 UNKNOWN_ERROR 让模型误判为不可恢复。
# 函数用途: 构造 send_message 的失败结果。
def _error(message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        "send_message",
        False,
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        error_code=code,
    )


__all__ = ["SendMessageTool", "build_send_message_spec"]
