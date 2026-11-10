"""直属下级未决能力申请的只读模型工具。"""

# LLM: 这是 pending_inbox 的唯一模型入口：不写任何状态，开关每轮按能力配置实时复核，关闭时工具不可用。
# 模块用途: 把只读收件箱页交给协调代理，裁决仍由 resolve_capability_requests 完成。

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ..pending_inbox import PendingInboxError, pending_inbox_page
from ..tool_specs import build_list_pending_requests_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent

# LLM: 游标越权在错误码登记表里没有专门的码，沿用已登记的权限拒绝码，具体原因放在结构化 details 里。
# 常量用途: 把收件箱内部原因映射到已登记的工具错误码。
_ERROR_CODES = {"TOOL_INVALID_ARGUMENTS": "TOOL_INVALID_ARGUMENTS", "CURSOR_SCOPE_MISMATCH": "TOOL_PERMISSION_DENIED"}


# LLM: 管理身份只来自 management_continuity；模型参数只有游标、条数和是否含已处理，不能指定父级或子代理。
# 类用途: 只读列出当前代理（含已证明的定时续查父任务）直属下级的能力申请。
class ListPendingRequestsTool(BaseTool):
    model_spec = build_list_pending_requests_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("current_agent_tree",)),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: 只绑定当前代理；owner、管理器和路径都从代理本身取，不接受参数注入。
    # 函数用途: 保存当前代理引用。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent

    # LLM: 开关读失败或关闭都视为不可用，不回落开启。
    # 函数用途: 按能力配置实时判断收件箱是否开放。
    def availability(self) -> ToolAvailability:
        from ....capability.runtime_config_reload import capability_config_for_agent

        config = capability_config_for_agent(self.agent)
        if config is None or not getattr(config, "orchestration_pending_inbox_enabled", False):
            return ToolAvailability.unavailable("未决申请收件箱开关未启用")
        return ToolAvailability.ready()

    # LLM: 参数类型错误与游标越权都在读取任何申请前返回 not_started，失败结果不含申请数据。
    # 函数用途: 读取一页收件箱并按 JSON 返回。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        name = self.model_spec.name
        try:
            include_resolved = params.get("include_resolved", False)
            if not isinstance(include_resolved, bool):
                raise PendingInboxError("TOOL_INVALID_ARGUMENTS", "include_resolved 必须是布尔值。")
            cursor = params.get("cursor") or ""
            if not isinstance(cursor, str):
                raise PendingInboxError("TOOL_INVALID_ARGUMENTS", "cursor 必须是字符串。")
            payload = pending_inbox_page(self.agent, cursor=cursor, limit=params.get("limit") or 0,
                                         include_resolved=include_resolved)
        except PendingInboxError as error:
            details = {"error": {"reason": error.code, "message": str(error)}}
            return ToolHandlerOutcome(name, False, json.dumps(details, ensure_ascii=False),
                                      result_envelope={"details": details},
                                      error_code=_ERROR_CODES.get(error.code, "TOOL_INVALID_ARGUMENTS"),
                                      effect_outcome="not_started")
        return ToolHandlerOutcome(name, True, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


__all__ = ["ListPendingRequestsTool"]
