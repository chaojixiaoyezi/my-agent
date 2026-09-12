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
from ..conversation.channels import (
    project_host_paths_for_channel,
    project_user_reply,
    redact_host_absolute_paths,
)
from ..delivery import (
    ChannelAttachment,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolOperationReconciliation,
    ToolOperationReconciliationContext,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MAX_ATTACHMENTS = 10
_MAX_EVIDENCE_REFS = 64


# LLM: send_message 是通道无关的模型入口，目标固定为当前 owner；不要新增 feishu_send 等重叠工具。
# 函数用途: 返回发送文字或 owner 已登记附件的工具说明书。
def build_send_message_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="send_message",
        description=(
            "把文字或已经生成的文件原生发送到当前用户连接的聊天通道。"
            "用户说‘发我/传给我/作为附件发送’时，在文件生成后调用；不需要用户提供飞书 ID。"
            "attachments 每项使用 Recent Artifact Refs 里的完整 path 原值。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "随附件发送的简短说明；只发附件时可留空。"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_ATTACHMENTS,
                    "description": "Recent Artifact Refs 中 path 组成的数组。",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_EVIDENCE_REFS,
                    "description": "正文实际依据的结构化证据引用；不从正文猜测。",
                },
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="messaging",
            use_cases=(
                "用户要求把刚生成的文件作为附件发回当前会话",
                "用户追问把上一个文件发我时复用 Recent Artifact Refs",
                "需要主动给当前 owner 发送一条短消息",
            ),
            avoid_when=(
                "当前轮正常回复文字时直接最终回复，不额外发送",
                "文件尚未生成或登记时先完成产物",
                "不能给其他用户或任意群发送消息",
            ),
            keywords=("发我", "发送", "飞书", "附件", "传给我", "把文件给我", "send", "message", "attachment"),
            examples=(
                '{"tool":"send_message","message":"文件写好了。","attachments":["<Recent Artifact Refs path>"]}',
                '{"tool":"send_message","attachments":["<已登记产物的完整 path>"]}',
            ),
        ),
    )


# LLM: 工具实例只负责发送业务；执行前占位、终态保存和重放统一由 Tool Gateway 操作账本负责。
# 类用途: 校验当前用户和产物后，通过飞书等已连接通道发送消息及附件。
class SendMessageTool(BaseTool):
    # LLM: 每个 owner agent 各持有自己的 adapter hub；跨 owner 隔离由 owner registry 和操作账本共同保证。
    # 函数用途: 创建当前 agent 的原生消息发送工具。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.model_spec = build_send_message_model_spec()
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"),
            idempotency_policy=IdempotencyPolicy("business"),
            resource_scopes=ResourceScopePolicy(
                mode="declared",
                static_scopes=("current_owner_channel",),
            ),
            input_policy=ToolInputPolicy(
                internal_parameters=("__run_scope", "__tool_call_id"),
                trusted_parameter_bindings=((
                    "evidence_refs",
                    TrustedParameterBinding(
                        source_refs=("run_scope.delivery_evidence_refs",),
                        authority="host_authoritative",
                    ),
                ),),
            ),
        )
        self._delivery: DeliveryService = agent.delivery_service

    # LLM: Model visibility and execution preflight must use the same owner identity and
    # registry capability facts; an unbound local transcript must not advertise proactive send.
    # 函数用途: 在每轮工具快照前判断当前 owner 是否真有可用外部消息目标，避免模型空烧发送调用。
    def availability(self) -> ToolAvailability:
        provider, target, owner_root = _owner_delivery_identity(self.agent)
        capabilities = self._delivery.registry.capabilities_for(provider)
        if not capabilities.proactive or not target or owner_root is None:
            return ToolAvailability.unavailable(
                "current owner has no proactive channel binding",
                error_code="CHANNEL_ADAPTER_UNAVAILABLE",
            )
        return ToolAvailability.ready()

    # LLM: 同一 owner 请求里的同一外发内容是一个业务动作；call_id 不得进入键，否则超时后换调用 ID 可绕过去重。
    # 函数用途: 用可信 request/run scope、固定 owner 目标和结构化正文/附件引用生成稳定发送键。
    def business_idempotency_key(self, params: dict[str, Any]) -> str:
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        request_scope = str(
            scope.get("request_id") or scope.get("run_id") or ""
        ).strip()
        provider, target, _owner_root = _owner_delivery_identity(self.agent)
        if not request_scope or not provider or not target:
            return ""
        attachment_refs = params.get("attachments")
        refs = []
        if isinstance(attachment_refs, list):
            refs = list(
                dict.fromkeys(
                    str(item).strip()
                    for item in attachment_refs
                    if isinstance(item, str) and str(item).strip()
                )
            )
        evidence_refs = _string_refs(params.get("evidence_refs"))
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        refs, evidence_refs = _canonical_scoped_delivery_refs(
            scope,
            refs,
            evidence_refs,
        )
        payload = {
            "provider": provider,
            "target": target,
            "request_scope": request_scope,
            "message": str(params.get("message") or "").strip(),
            "attachments": refs,
            "evidence_refs": evidence_refs,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"send_message:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    # LLM: 未知发送只有在当前 provider 明确声明原生幂等且账本保留稳定业务键时才可重放；
    # 其他通道继续 unknown fail-closed，不能因“可能发过”而盲目发送第二次。
    # 函数用途: 把通道注册表的 provider 幂等事实转换为统一操作账本的安全重试结论。
    def reconcile_operation(
        self,
        params: dict[str, Any],
        context: ToolOperationReconciliationContext,
    ) -> ToolOperationReconciliation:
        provider, target, _owner_root = _owner_delivery_identity(self.agent)
        capabilities = self._delivery.registry.capabilities_for(provider)
        if (
            not provider
            or not target
            or not str(context.idempotency_key or "").strip()
            or not capabilities.provider_idempotency
        ):
            return ToolOperationReconciliation(
                reason="provider_idempotency_unavailable"
            )
        return ToolOperationReconciliation(
            outcome="safe_to_retry",
            source_ref=f"channel_capability:{provider}:provider_idempotency.v1",
        )

    # LLM: 外部副作用前必须先完成 owner target、registry、真实路径和 hash 四层校验。
    # 函数用途: 执行一次发给当前用户的文字/附件发送，并返回不含服务器路径的回执。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        message = str(params.get("message") or "").strip()
        attachment_refs = _attachment_refs(params.get("attachments"))
        if isinstance(attachment_refs, ToolHandlerOutcome):
            return attachment_refs
        evidence_refs = _evidence_refs(params.get("evidence_refs"))
        if isinstance(evidence_refs, ToolHandlerOutcome):
            return evidence_refs
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        attachment_refs, evidence_refs = _canonical_scoped_delivery_refs(
            scope,
            attachment_refs,
            evidence_refs,
        )
        scope_error = _scoped_delivery_evidence_error(scope, evidence_refs)
        if scope_error is not None:
            return scope_error
        if not message and not attachment_refs:
            return _error("message 和 attachments 不能同时为空", "TOOL_INVALID_ARGUMENTS")
        provider, target, owner_root = _owner_delivery_identity(self.agent)
        capabilities = self._delivery.registry.capabilities_for(provider)
        if not capabilities.proactive or not target or owner_root is None:
            return _error("当前 owner 没有可用的外部消息通道", "CHANNEL_ADAPTER_UNAVAILABLE")
        attachments = _resolve_attachments(owner_root, attachment_refs)
        if isinstance(attachments, ToolHandlerOutcome):
            return attachments
        envelope = ReplyEnvelope(
            content=message,
            attachments=attachments,
            evidence_refs=tuple(evidence_refs),
        )
        base_context = DeliveryContext(
            channel=provider,
            target=target,
            mode="proactive",
            request_id=str(scope.get("request_id") or ""),
            task_id=str(scope.get("task_id") or scope.get("run_id") or ""),
        )
        receipt_key = self.business_idempotency_key(params) or _receipt_key(
            base_context,
            envelope,
            params,
        )
        delivery_context = DeliveryContext(
            channel=provider,
            target=target,
            mode="proactive",
            request_id=base_context.request_id,
            task_id=base_context.task_id,
            idempotency_key=receipt_key,
        )
        receipt = self._delivery.deliver(delivery_context, envelope)
        if receipt.delivery_status != "sent":
            return _error(
                "消息或附件没有成功送达当前聊天通道",
                receipt.error_code or "CHANNEL_SEND_FAILED",
                effect_outcome=(
                    "not_started"
                    if receipt.delivery_status
                    in {"rejected", "unavailable", "not_applicable", "suppressed"}
                    else "unknown"
                ),
                effect_source_ref=f"delivery_receipt:{receipt.delivery_status or 'unknown'}",
            )
        from ..ingestion.harvester import record_audit_delivery_refs

        reported_refs = record_audit_delivery_refs(
            owner_root,
            evidence_refs,
            receipt_id=receipt_key[:20],
            channel=provider,
        )
        payload = _success_payload(
            provider,
            message,
            attachments,
            receipt_key,
            evidence_refs=evidence_refs,
            reported_refs=reported_refs,
        )
        return _success_result(
            provider,
            message,
            attachments,
            payload,
            evidence_refs=evidence_refs,
        )


# LLM: attachments 是开放世界的 artifact 引用数组；这里只限制资源数量和元素标量类型。
# 函数用途: 清洗并去重模型传入的附件引用。
def _attachment_refs(value: object) -> list[str] | ToolHandlerOutcome:
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


# LLM: 某些后台事件只允许发送与当前事件精确绑定的证据集合；这个约束来自可信 RunScope，
#   不能由模型正文或参数自己声明、放松或替换。
# 函数用途: 在通道副作用发生前验证本轮要求的 evidence_refs 是否完整且无跨事件夹带。
def _scoped_delivery_evidence_error(
    scope: dict[str, object],
    evidence_refs: list[str],
) -> ToolHandlerOutcome | None:
    required = _required_delivery_evidence_refs(scope)
    if not required:
        return None
    if len(evidence_refs) == len(required) and set(evidence_refs) == set(required):
        return None
    return _error(
        "当前后台事件要求消息携带与事件精确一致的 evidence_refs",
        "TOOL_INVALID_ARGUMENTS",
    )


# LLM: 后台事件的证据集合来自可信 RunScope；若模型把其中的同一 typed ref 放进附件字段，
#   统一在副作用前归回 evidence_refs。只搬运可信集合的精确成员，不猜普通字符串或文件用途。
# 函数用途: 让附件解析、证据硬门和业务幂等键共享一份规范化引用分类。
def _canonical_scoped_delivery_refs(
    scope: dict[str, object],
    attachment_refs: list[str],
    evidence_refs: list[str],
) -> tuple[list[str], list[str]]:
    required = set(_required_delivery_evidence_refs(scope))
    if not required:
        return attachment_refs, evidence_refs
    moved = [ref for ref in attachment_refs if ref in required]
    if not moved:
        return attachment_refs, evidence_refs
    attachments = [ref for ref in attachment_refs if ref not in required]
    evidence = list(dict.fromkeys([*evidence_refs, *moved]))
    return attachments, evidence


def _required_delivery_evidence_refs(scope: dict[str, object]) -> list[str]:
    required_raw = scope.get("delivery_evidence_refs")
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in (
                required_raw if isinstance(required_raw, (list, tuple)) else ()
            )
            if str(item).strip()
        )
    )


def _string_refs(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        )
    )


def _evidence_refs(value: object) -> list[str] | ToolHandlerOutcome:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        return _error("evidence_refs 必须是字符串数组", "TOOL_INVALID_ARGUMENTS")
    if any(
        not isinstance(item, str) or not str(item).strip()
        for item in value
    ):
        return _error(
            "evidence_refs 每一项都必须是非空字符串",
            "TOOL_INVALID_ARGUMENTS",
        )
    refs = _string_refs(value)
    if len(refs) > _MAX_EVIDENCE_REFS:
        return _error(
            f"一次最多携带 {_MAX_EVIDENCE_REFS} 个 evidence_refs",
            "TOOL_INVALID_ARGUMENTS",
        )
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
) -> tuple[ChannelAttachment, ...] | ToolHandlerOutcome:
    attachments: list[ChannelAttachment] = []
    for ref in refs:
        record = _resolve_registered_artifact(owner_root, ref)
        if isinstance(record, ToolHandlerOutcome):
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
) -> ArtifactRegistryRecord | ToolHandlerOutcome | None:
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
    *,
    evidence_refs: list[str],
    reported_refs: list[str],
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
        "evidence_refs": list(evidence_refs),
        "reported_source_refs": list(reported_refs),
    }


# LLM: 成功外发必须同时返回一份结构化 delivery evidence，供本轮唯一回复出口判断是否还需自动发送；
#   不能让上层从模型最终正文或日志文本猜测“是不是已经发过”。
# 函数用途: 构造带当前 owner 已送达事实的工具结果，同时保持公开 output 不暴露 owner 路径。
def _success_result(
    provider: str,
    message: str,
    attachments: tuple[ChannelAttachment, ...],
    payload: dict[str, Any],
    *,
    evidence_refs: list[str],
) -> ToolHandlerOutcome:
    output = dict(payload)
    return ToolHandlerOutcome(
        "send_message",
        True,
        json.dumps(output, ensure_ascii=False),
        result_envelope={
            "delivery_evidence": {
                "schema_version": "message_tool_delivery.v1",
                "delivery_status": "sent",
                "source_owner_delivery": True,
                "channel": provider,
                "content": project_host_paths_for_channel(
                    project_user_reply(message).content, provider
                ),
                "receipt_id": str(payload.get("receipt_id") or ""),
                "evidence_refs": list(evidence_refs),
                "deduplicated": False,
                # 路径只在内部结构化运行事实中保留，供同一 owner transcript 复用附件；
                # ToolHandlerOutcome.output 仍只暴露不含路径的 _success_payload。
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
def _error(
    message: str,
    code: str,
    *,
    effect_outcome: str = "not_started",
    effect_source_ref: str = "send_message_preflight",
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "send_message",
        False,
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        error_code=code,
        effect_outcome=effect_outcome,
        effect_source_ref=effect_source_ref,
    )


__all__ = ["SendMessageTool", "build_send_message_model_spec"]
