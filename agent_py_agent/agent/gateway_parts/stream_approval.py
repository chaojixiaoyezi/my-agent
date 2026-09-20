# LLM: 此组件只绑定原 owner 审批缓存及当前请求输出，不创建持久权限或改变执行器裁决；同步审批模式/取消回归。
# 模块用途: 将精确工具审批的缓存、公开请求和等待过程从文本缓冲中分离。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from .approval_session import ToolApprovalSessionCache
from .permission_bridge import (
    unavailable_gateway_permission_decision,
    wait_for_gateway_permission_decision,
)


# LLM: 实例只属于一条流，引用宿主缓存而非复制缓存；publish/prepare 在原调用点同步执行，不能延迟或后台化。
# 类用途: 管理一次 Gateway 请求的交互审批，按可信作用域复用授权并等待真实客户端决定。
@dataclass
class StreamApproval:
    chunk_path: Path
    publish: Callable[[dict[str, object]], None]
    before_wait: Callable[[], None]
    cache: ToolApprovalSessionCache | None = field(default=None, repr=False)
    scope_provider: Callable[[], str] | None = field(default=None, repr=False)

    # LLM: Binding happens only after canonical owner/thread/cwd resolution and before the model
    # can call tools. Request payload prose or tool arguments must never choose this scope.
    # 函数用途: 把当前请求接到真实会话级审批缓存，供后续完全相同的调用复用一次授权。
    def configure(
        self,
        cache: ToolApprovalSessionCache,
        scope_provider: Callable[[], str],
    ) -> None:
        self.cache = cache
        self.scope_provider = scope_provider

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

    # LLM: 精确请求先发布后等待；用户从菜单切自主可原地续跑，模式提供者由 owner 控制面绑定，不接受模型参数。
    # 函数用途: 向客户端发布审批并等待决定或自主模式切换；取消保持优先。
    def request(
        self,
        request_value: dict[str, object],
        *,
        interactive: bool,
        mode_decision_provider: Callable | None = None,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        request = ToolApprovalRequest.from_mapping(request_value)
        if not interactive:
            return unavailable_gateway_permission_decision(request).to_dict()
        session_key = _permission_session_key(request)
        approval_cache = self.cache
        approval_scope = self._current_scope()
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
            mode_decision_provider=mode_decision_provider,
        )
        if session_key and str(decision.decision or "").strip().lower() == "approved_session":
            if approval_cache is not None:
                approval_cache.approve(approval_scope, session_key)
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
