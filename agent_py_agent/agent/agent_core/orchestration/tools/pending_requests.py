"""直属下级未决能力申请的只读模型工具。"""

# LLM: 这是 pending_inbox 的唯一模型入口：不写任何状态。B1 生命周期：工具常驻注册，availability 每次快照现读
#   当前 owner 的能力开关，关着（或配置读不出/非布尔）就不进快照、模型看不到也调不了；execute 自己再查一次，
#   过期的直接调用同样拒绝。
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

# LLM: 游标越权在错误码登记表里没有专门的码，沿用已登记的权限拒绝码。机器层只消费顶层 error_code
#   （TOOL_PERMISSION_DENIED / TOOL_INVALID_ARGUMENTS，effect_outcome=not_started）；details.error.reason
#   只用于诊断和展示，不参与任何控制判断。
# 常量用途: 把收件箱内部原因映射到已登记的工具错误码。
_ERROR_CODES = {"TOOL_INVALID_ARGUMENTS": "TOOL_INVALID_ARGUMENTS", "CURSOR_SCOPE_MISMATCH": "TOOL_PERMISSION_DENIED"}


# LLM: 管理身份只来自 management_continuity；模型参数只有游标、条数和是否含已处理，不能指定父级或子代理。
# 类用途: 只读列出当前代理（含已证明的定时续查父任务）直属下级的能力申请。
class ListPendingRequestsTool(BaseTool):
    model_spec = build_list_pending_requests_model_spec()
    # B1-R3：开关关着时连名字/类别/原因都不进模型可见的发现面（注册表按这个结构属性过滤）。
    hidden_when_unavailable = True
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

    # LLM: 每次快照现读当前 owner 的能力配置；读不出（None/异常）或值不是 True 都按关闭，不回落默认开启。
    # 函数用途: 告诉注册表收件箱此刻是否开放。
    def availability(self) -> ToolAvailability:
        if _inbox_enabled(self.agent):
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("未决申请收件箱开关未启用")

    # LLM: 参数类型错误与游标越权都在读取任何申请前返回 not_started，失败结果不含申请数据；开关关着时直接或过期的调用
    #   同样在读取前拒绝。
    # 函数用途: 读取一页收件箱并按 JSON 返回。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        name = self.model_spec.name
        if not _inbox_enabled(self.agent):
            details = {"error": {"reason": "PENDING_INBOX_DISABLED", "message": "未决申请收件箱开关未启用。"}}
            return ToolHandlerOutcome(name, False, json.dumps(details, ensure_ascii=False),
                                      result_envelope={"details": details}, error_code="TOOL_PERMISSION_DENIED",
                                      effect_outcome="not_started")
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


# LLM: 唯一的开关读口；B1-R1/R2：只认有可靠来源的配置（选中的用户文件零解析诊断，或随包模板真读成功），坏行、
#   读不到来源时的默认对象都按关闭；值还必须严格为 True。
# 函数用途: 现读当前 owner 的收件箱开关。
def _inbox_enabled(agent: object) -> bool:
    from ....capability import runtime_config_reload
    from ....capability.config import CapabilityConfig

    try:
        config, _refusal = runtime_config_reload.capability_config_with_trusted_source(agent)
    except Exception:  # noqa: BLE001 - 配置读失败按关闭处理
        return False
    return isinstance(config, CapabilityConfig) and config.orchestration_pending_inbox_enabled is True


__all__ = ["ListPendingRequestsTool"]
