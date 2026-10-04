# LLM: 此组件绑定原审批缓存；插件强制确认先读统一判定并禁用缓存/长期授权，不创建第二授权源，联测双请求与等待入口。
# 模块用途: 发布精确审批并等待用户决定，避免旧授权绕过插件要求。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalWaitOptions,
    plugin_gate_required,
)
from .approval_session import ToolApprovalSessionCache
from .permission_bridge import (
    unavailable_gateway_permission_decision,
    wait_for_gateway_permission_decision,
)


# LLM: 与等待入口同样使用固定宿主选项；只包装原参数，不接受任意关键字或新增配置源。
# 类用途: 汇总流式审批的交互能力、自主提供者和取消令牌。
@dataclass(frozen=True)
class StreamApprovalRequestOptions:
    interactive: bool
    mode_decision_provider: Callable | None = None
    cancellation_token: object | None = None


# LLM: 实例只属于一条流，引用宿主缓存而非复制缓存；publish/prepare 在原调用点同步执行，不能延迟或后台化。
# 类用途: 管理一次 Gateway 请求的交互审批，按可信作用域复用授权并等待真实客户端决定。
@dataclass
class StreamApproval:
    chunk_path: Path
    publish: Callable[[dict[str, object]], None]
    before_wait: Callable[[], None]
    cache: ToolApprovalSessionCache | None = field(default=None, repr=False)
    scope_provider: Callable[[], str] | None = field(default=None, repr=False)
    # LLM: 用户选 approved_owner 时把 binding.grant_key 交给宿主持久化（owner 策略文件）；None 表示本流不支持长期授权。
    grant_recorder: Callable[[str], None] | None = field(default=None, repr=False)

    # LLM: Binding happens only after canonical owner/thread/cwd resolution and before the model
    # can call tools. Request payload prose or tool arguments must never choose this scope.
    # 函数用途: 把当前请求接到真实会话级审批缓存，供后续完全相同的调用复用一次授权。
    def configure(
        self,
        cache: ToolApprovalSessionCache,
        scope_provider: Callable[[], str],
        grant_recorder: Callable[[str], None] | None = None,
    ) -> None:
        self.cache = cache
        self.scope_provider = scope_provider
        self.grant_recorder = grant_recorder

    # LLM: The canonical task workspace may be promoted after the first model sample. Resolve
    # it at the approval boundary so first-turn and later-turn keys describe the same real cwd.
    # 函数用途: 在工具真正请求授权时读取审批作用域；解析失败就禁用复用并重新询问。
    def _current_scope(self) -> str:
        provider = self.scope_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "").strip()
        except (OSError, RuntimeError, TypeError, ValueError):
            return ""

    # LLM: 解析请求后守门永远是第一句，必须先于任何缓存/授权/provider；固定选项与两个等待入口同一风格。
    # 函数用途: 发布当次确认；插件请求只能等用户，不让旧缓存或自主选择直接批准。
    def request(
        self,
        request_value: dict[str, object],
        *,
        options: StreamApprovalRequestOptions,
    ) -> dict[str, object]:
        request = ToolApprovalRequest.from_mapping(request_value)
        forced = plugin_gate_required(request)
        interactive, mode_decision_provider = options.interactive, options.mode_decision_provider
        cancellation_token = options.cancellation_token
        if not interactive:
            return unavailable_gateway_permission_decision(request).to_dict()
        session_key = "" if forced else _permission_session_key(request)
        approval_cache = self.cache
        approval_scope = "" if forced else self._current_scope()
        if (
            session_key
            and approval_cache is not None
            and approval_cache.is_approved(approval_scope, session_key)
        ):
            # 会话级已批准：直接放行，不再发布 permission_requested，避免 TUI 闪一下
            # 审批框；最终工具账本仍会记录这次真实执行。
            decision = ToolApprovalDecision(request.permission_id, "approved")
            self.publish(
                {
                    "kind": "permission_resolved",
                    **decision.to_dict(),
                    "session_cached": True,
                },
            )
            return decision.to_dict()
        grant_key = "" if forced else str(request.binding.get("grant_key") or "").strip()
        if grant_key and mode_decision_provider is not None:
            # 长期授权过的操作类别由宿主提供者直接给出批准，不发布审批面板；未授权时提供者返回 None，照常询问。
            granted = mode_decision_provider(request)
            if granted is not None and granted.approved:
                self.publish({"kind": "permission_resolved", **granted.to_dict(), "owner_granted": True})
                return granted.to_dict()
        self.before_wait()
        self.publish(
            {
                "kind": "permission_requested",
                "permission": request.to_dict(),
            },
        )
        decision = wait_for_gateway_permission_decision(
            self.chunk_path,
            request,
            cancellation_token=cancellation_token,
            options=ToolApprovalWaitOptions(mode_decision_provider=mode_decision_provider),
        )
        if session_key and str(decision.decision or "").strip().lower() == "approved_session":
            if approval_cache is not None:
                approval_cache.approve(approval_scope, session_key)
        if grant_key and str(decision.decision or "").strip().lower() == "approved_owner" and self.grant_recorder is not None:
            self.grant_recorder(grant_key)
        self.publish(
            {
                "kind": "permission_resolved",
                **decision.to_dict(),
            },
        )
        return decision.to_dict()


# LLM: 会话级审批键 = 工具名 + 参数哈希（会话运行时 规范化命令 key 的等价物）；同一调用
# 再次出现时不再询问。permission_id 因 request 变化不含在内。
# 函数用途: 生成审批会话缓存的稳定键。
def _permission_session_key(request: ToolApprovalRequest) -> str:
    binding = getattr(request, "binding", None)
    if not isinstance(binding, dict):
        return ""
    tool = str(binding.get("tool_name") or "").strip()
    args_hash = str(binding.get("args_hash") or "").strip()
    if not tool or not args_hash:
        return ""
    return f"{tool}:{args_hash}"
